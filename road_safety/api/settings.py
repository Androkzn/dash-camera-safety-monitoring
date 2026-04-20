"""Settings Console — FastAPI router.

Routes registered by :func:`mount`:

* Reads (admin-bearer):
    - ``GET  /api/settings/effective``
    - ``GET  /api/settings/schema``
    - ``GET  /api/settings/templates``
    - ``GET  /api/settings/templates/{template_id}/revisions``
    - ``GET  /api/settings/baseline?audit_id=…``
    - ``GET  /api/settings/impact?audit_id=…``
    - ``GET  /api/settings/impact/history``
    - ``GET  /api/settings/apply_log``
    - ``GET  /api/settings/observability``
* Writes (admin-bearer):
    - ``POST /api/settings/validate``
    - ``POST /api/settings/apply``
    - ``POST /api/settings/rollback``
    - ``POST /api/settings/templates``
    - ``PATCH /api/settings/templates/{template_id}``
    - ``DELETE /api/settings/templates/{template_id}``
    - ``POST /api/settings/templates/{template_id}/apply``
    - ``POST /api/settings/baseline/capture``
    - ``POST /api/settings/stream_ticket``
* SSE (ticket-gated):
    - ``GET  /api/settings/impact/stream?ticket=…``
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from road_safety import settings_spec
from road_safety.compliance import audit
from road_safety.config import ADMIN_TOKEN
from road_safety.security import require_bearer_token
from road_safety.services import settings_db
from road_safety.services import templates as template_svc
from road_safety.services.impact import ImpactMonitor
from road_safety.settings_store import (
    STORE,
    AppliedResult,
    PrivacyConfirmRequired,
    RevisionConflict,
    SettingsValidationError,
)


# ---------------------------------------------------------------------------
# Apply-rate cooldown (per-token / per-IP best-effort)
# ---------------------------------------------------------------------------
MIN_CHANGE_INTERVAL_SEC = 5.0
_last_apply_at: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Ticket exchange (single-use, 30 s TTL)
# ---------------------------------------------------------------------------
_TICKET_TTL_SEC = 30.0
_tickets: dict[str, tuple[str, float]] = {}  # ticket -> (actor, exp)
_ticket_lock = asyncio.Lock()


async def _issue_ticket(actor: str) -> tuple[str, float]:
    """Mint and store a fresh ticket. Sweeps expired entries opportunistically."""
    async with _ticket_lock:
        now = time.monotonic()
        # Janitor sweep — cheap because the dict is tiny.
        for k in [t for t, (_a, exp) in _tickets.items() if exp <= now]:
            _tickets.pop(k, None)
        ticket = secrets.token_hex(16)
        _tickets[ticket] = (actor, now + _TICKET_TTL_SEC)
        return ticket, _TICKET_TTL_SEC


async def _consume_ticket(ticket: str) -> str | None:
    """Pop a ticket if valid. Returns the actor label or ``None``."""
    async with _ticket_lock:
        item = _tickets.pop(ticket, None)
        if item is None:
            return None
        actor, exp = item
        if time.monotonic() > exp:
            return None
        return actor


# ---------------------------------------------------------------------------
# Pydantic bodies
# ---------------------------------------------------------------------------
class ValidateBody(BaseModel):
    diff: dict[str, Any] = Field(default_factory=dict)


class ApplyBody(BaseModel):
    diff: dict[str, Any] = Field(default_factory=dict)
    expected_revision_hash: str | None = None
    confirm_privacy_change: bool = False
    operator_label: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)


class TemplateCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    payload: dict[str, Any] = Field(default_factory=dict)


class TemplateUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    payload: dict[str, Any] | None = None


class TemplateApplyBody(BaseModel):
    expected_revision_hash: str | None = None
    confirm_privacy_change: bool = False
    operator_label: str | None = Field(default=None, max_length=120)


class StreamTicketBody(BaseModel):
    operator_label: str | None = Field(default=None, max_length=120)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bearer(request: Request) -> None:
    require_bearer_token(
        request,
        ADMIN_TOKEN,
        realm="settings",
        env_var="ROAD_ADMIN_TOKEN",
    )


def _actor(request: Request, label: str | None = None) -> str:
    """Best-effort operator id under the single shared admin token."""
    if label:
        return label.strip()[:120]
    fwd = request.headers.get("x-operator-label")
    if fwd:
        return fwd.strip()[:120]
    return "admin"


def _validation_response(errors: list[dict]) -> JSONResponse:
    return JSONResponse(status_code=422, content={"errors": errors})


def _conflict_response(expected: str, actual: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"error": "revision_conflict", "expected": expected, "actual": actual},
    )


def _result_payload(result: AppliedResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "applied_now": result.applied_now,
        "pending_restart": result.pending_restart,
        "warnings": result.warnings,
        "revision_hash_before": result.revision_hash_before,
        "revision_hash_after": result.revision_hash_after,
        "revision_no": result.revision_no,
    }


def _check_apply_cooldown(actor: str) -> None:
    """Reject if this actor's last *successful* apply was within the cooldown.

    The cooldown clock is stamped by ``_record_apply_attempt`` only when an
    apply actually mutates state. Failed attempts (validation errors,
    revision conflicts, privacy-confirm-required, no-op diffs) deliberately
    do **not** burn the budget — punishing a typo with a 5 s lockout is a
    hostile UX and offers no protection (no state changed, no subscriber
    storm). DoS protection against rapid invalid attempts is left to the
    upstream proxy / WAF, where it belongs.
    """
    now = time.monotonic()
    last = _last_apply_at.get(actor, 0.0)
    if now - last < MIN_CHANGE_INTERVAL_SEC:
        wait = MIN_CHANGE_INTERVAL_SEC - (now - last)
        raise HTTPException(
            status_code=429,
            detail=f"apply rate limited; retry after {wait:.1f}s",
            headers={"Retry-After": str(int(wait) + 1)},
        )


def _record_apply_attempt(actor: str) -> None:
    """Stamp the cooldown clock — call only after a state-changing apply."""
    _last_apply_at[actor] = time.monotonic()


# Back-compat alias so external callers that imported the old eager helper
# still see the previous "check + stamp on attempt" semantics. Internal
# callers use the split pair above instead.
def _enforce_apply_cooldown(actor: str) -> None:
    _check_apply_cooldown(actor)
    _record_apply_attempt(actor)


# ---------------------------------------------------------------------------
# Mount
# ---------------------------------------------------------------------------
def mount(
    app: FastAPI,
    *,
    impact_monitor: ImpactMonitor,
    impact_subscribers: list[asyncio.Queue],
) -> None:
    """Register every ``/api/settings/*`` route on ``app``.

    ``impact_subscribers`` is a list of asyncio Queues filled by ``server.py``
    when an apply happens; the SSE handler drains its own queue and closes
    on disconnect. Keeping the list owned by the server keeps the lifecycle
    consistent with the existing ``/stream/events`` SSE pattern.
    """

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    @app.get("/api/settings/schema")
    async def get_schema(request: Request):
        _bearer(request)
        return settings_spec.schema_payload()

    @app.get("/api/settings/effective")
    async def get_effective(request: Request):
        _bearer(request)
        snap = dict(STORE.snapshot())
        return {
            "schema_version": settings_spec.SCHEMA_VERSION,
            "values": snap,
            "revision_hash": STORE.revision_hash(),
            "revision_no": STORE.revision_no(),
        }

    @app.get("/api/settings/templates")
    async def get_templates(request: Request):
        _bearer(request)
        return {"templates": template_svc.list_templates()}

    @app.get("/api/settings/templates/{template_id}/revisions")
    async def get_template_revisions(template_id: str, request: Request):
        _bearer(request)
        if template_svc.get_template(template_id) is None:
            raise HTTPException(status_code=404, detail=f"template {template_id} not found")
        return {"revisions": template_svc.list_revisions(template_id)}

    @app.get("/api/settings/baseline")
    async def get_baseline(request: Request, audit_id: str = Query(...)):
        _bearer(request)
        bl = settings_db.baseline_for_audit(audit_id)
        if bl is None:
            raise HTTPException(status_code=404, detail="no baseline for that audit_id")
        return bl

    @app.get("/api/settings/impact")
    async def get_impact(request: Request, audit_id: str | None = None):
        _bearer(request)
        report = (
            impact_monitor.report_for(audit_id)
            if audit_id
            else impact_monitor.current_report()
        )
        if report is None:
            return {"report": None}
        return {"report": report.to_dict()}

    @app.get("/api/settings/impact/history")
    async def get_impact_history(request: Request, limit: int = 20):
        _bearer(request)
        limit = max(1, min(limit, 200))
        return {"items": settings_db.list_archived_sessions(limit=limit)}

    @app.get("/api/settings/apply_log")
    async def get_apply_log(request: Request, limit: int = 50):
        _bearer(request)
        limit = max(1, min(limit, 200))
        return {"items": settings_db.list_apply_log(limit=limit)}

    @app.get("/api/settings/observability")
    async def get_observability(request: Request):
        _bearer(request)
        return {"counters": dict(STORE.counters), "revision_no": STORE.revision_no()}

    # ------------------------------------------------------------------
    # Writes — settings
    # ------------------------------------------------------------------
    @app.post("/api/settings/validate")
    async def validate(body: ValidateBody, request: Request):
        _bearer(request)
        snap = dict(STORE.snapshot())
        merged = dict(snap)
        cleaned: dict[str, Any] = {}
        for k, v in body.diff.items():
            if settings_spec.spec_for(k) is None:
                continue
            try:
                cleaned[k] = settings_spec.coerce(k, v)
            except (TypeError, ValueError) as exc:
                return _validation_response([{"key": k, "reason": f"coercion failed: {exc}"}])
        merged.update(cleaned)
        errors = settings_spec.validate(merged)
        if errors:
            return _validation_response(errors)
        buckets = settings_spec.changed_mutability(cleaned)
        return {
            "ok": True,
            "resolved_diff": cleaned,
            "would_apply_now": sorted(buckets.get("hot_apply", []) + buckets.get("warm_reload", [])),
            "would_pending_restart": sorted(buckets.get("restart_required", [])),
        }

    @app.post("/api/settings/apply")
    async def apply(body: ApplyBody, request: Request):
        _bearer(request)
        actor = _actor(request, body.operator_label)
        # Check-only: failed attempts below do not stamp the cooldown clock.
        _check_apply_cooldown(actor)
        before_snap = dict(STORE.snapshot())
        try:
            result = STORE.apply_diff(
                body.diff,
                actor=actor,
                expected_revision_hash=body.expected_revision_hash,
                confirm_privacy_change=body.confirm_privacy_change,
            )
        except RevisionConflict as exc:
            settings_db.insert_apply_log(
                actor_label=actor,
                revision_hash_before=exc.actual,
                revision_hash_after=exc.actual,
                result="conflict",
                warnings=[],
                payload={"diff": body.diff, "expected": exc.expected},
            )
            return _conflict_response(exc.expected, exc.actual)
        except PrivacyConfirmRequired as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "privacy_confirm_required",
                    "key": exc.key,
                    "hint": "set confirm_privacy_change=true to acknowledge",
                },
            )
        except SettingsValidationError as exc:
            return _validation_response(exc.errors)

        after_snap = dict(STORE.snapshot())
        changed_keys = sorted(body.diff.keys())
        audit_id: str | None = None
        if changed_keys and result.applied_now:
            audit_id = impact_monitor.on_settings_change(
                before_snap, after_snap, actor_label=actor, changed_keys=changed_keys
            )
        # Stamp the cooldown clock only when state actually moved. A no-op
        # apply (empty diff, or diff that resolves to the current values)
        # produces no subscriber storm and shouldn't lock the operator out.
        if result.applied_now or result.pending_restart:
            _record_apply_attempt(actor)
        log_id = settings_db.insert_apply_log(
            actor_label=actor,
            revision_hash_before=result.revision_hash_before,
            revision_hash_after=result.revision_hash_after,
            result="success",
            warnings=result.warnings,
            payload={"diff": body.diff, "applied_now": result.applied_now},
            audit_id=audit_id,
        )
        audit.log(
            "settings.apply",
            f"settings:{result.revision_hash_after}",
            actor=actor,
            outcome="success",
            detail={
                "log_id": log_id,
                "applied_now": result.applied_now,
                "pending_restart": result.pending_restart,
                "warnings": result.warnings,
                "audit_id": audit_id,
                "note": body.note,
            },
        )
        # Best-effort SSE notification.
        payload = _result_payload(result)
        payload["audit_id"] = audit_id
        await _broadcast(impact_subscribers, {"event": "apply", "data": payload})
        return payload

    @app.post("/api/settings/rollback")
    async def rollback(request: Request):
        _bearer(request)
        actor = _actor(request)
        # Check-only; stamp the cooldown only if the rollback actually
        # mutated state (a no-op rollback against an unchanged store
        # shouldn't count toward the budget).
        _check_apply_cooldown(actor)
        before = dict(STORE.snapshot())
        result = STORE.rollback_to_last_good(actor=actor)
        after = dict(STORE.snapshot())
        if result.applied_now or result.pending_restart:
            impact_monitor.archive_active()
            audit_id = impact_monitor.on_settings_change(
                before, after, actor_label=actor, changed_keys=sorted(after.keys())
            )
            _record_apply_attempt(actor)
        else:
            audit_id = None
        settings_db.insert_apply_log(
            actor_label=actor,
            revision_hash_before=result.revision_hash_before,
            revision_hash_after=result.revision_hash_after,
            result="rollback",
            warnings=result.warnings,
            payload={"applied_now": result.applied_now},
            audit_id=audit_id,
        )
        audit.log(
            "settings.rollback",
            f"settings:{result.revision_hash_after}",
            actor=actor,
            outcome="success",
            detail={"warnings": result.warnings, "audit_id": audit_id},
        )
        payload = _result_payload(result)
        payload["audit_id"] = audit_id
        return payload

    # ------------------------------------------------------------------
    # Writes — templates
    # ------------------------------------------------------------------
    @app.post("/api/settings/templates")
    async def create_template(body: TemplateCreateBody, request: Request):
        _bearer(request)
        actor = _actor(request)
        try:
            tmpl = template_svc.create_template(
                name=body.name,
                description=body.description,
                payload=body.payload,
                actor_label=actor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        audit.log(
            "settings.template.create",
            f"template:{tmpl['id']}",
            actor=actor,
            outcome="success",
            detail={"name": tmpl["name"]},
        )
        return tmpl

    @app.patch("/api/settings/templates/{template_id}")
    async def update_template(template_id: str, body: TemplateUpdateBody, request: Request):
        _bearer(request)
        actor = _actor(request)
        try:
            tmpl = template_svc.update_template(
                template_id,
                name=body.name,
                description=body.description,
                payload=body.payload,
                actor_label=actor,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"template {template_id} not found")
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        audit.log(
            "settings.template.update",
            f"template:{template_id}",
            actor=actor,
            outcome="success",
            detail={"changed_payload": body.payload is not None},
        )
        return tmpl

    @app.delete("/api/settings/templates/{template_id}")
    async def delete_template(template_id: str, request: Request):
        _bearer(request)
        actor = _actor(request)
        try:
            modified = template_svc.soft_delete_template(template_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"template {template_id} not found")
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        audit.log(
            "settings.template.delete",
            f"template:{template_id}",
            actor=actor,
            outcome="success",
            detail={"already_deleted": not modified},
        )
        return {"ok": True, "deleted": modified}

    @app.post("/api/settings/templates/{template_id}/apply")
    async def apply_template(template_id: str, body: TemplateApplyBody, request: Request):
        _bearer(request)
        actor = _actor(request, body.operator_label)
        # Check-only; the cooldown clock is stamped only after a real
        # state change below (so a missing template, validation failure,
        # privacy-confirm-required, or revision conflict don't lock the
        # operator out of fixing the request).
        _check_apply_cooldown(actor)
        try:
            plan = template_svc.prepare_template_apply(
                template_id, current_snapshot=dict(STORE.snapshot())
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"template {template_id} not found")
        if plan.dropped_keys:
            audit.log(
                "settings.template.key_dropped",
                f"template:{template_id}",
                actor=actor,
                outcome="success",
                detail={"keys": plan.dropped_keys},
            )
        if plan.filled_keys:
            audit.log(
                "settings.template.key_filled",
                f"template:{template_id}",
                actor=actor,
                outcome="success",
                detail={"keys": plan.filled_keys},
            )
        if plan.validation_errors:
            return _validation_response(plan.validation_errors)
        before_snap = dict(STORE.snapshot())
        try:
            result = STORE.apply_diff(
                plan.cleaned_diff,
                actor=actor,
                expected_revision_hash=body.expected_revision_hash,
                confirm_privacy_change=body.confirm_privacy_change,
            )
        except RevisionConflict as exc:
            return _conflict_response(exc.expected, exc.actual)
        except PrivacyConfirmRequired as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "privacy_confirm_required", "key": exc.key},
            )
        except SettingsValidationError as exc:
            return _validation_response(exc.errors)
        after_snap = dict(STORE.snapshot())
        changed_keys = sorted(plan.cleaned_diff.keys())
        audit_id: str | None = None
        if changed_keys:
            audit_id = impact_monitor.on_settings_change(
                before_snap, after_snap, actor_label=actor, changed_keys=changed_keys
            )
        if result.applied_now or result.pending_restart:
            _record_apply_attempt(actor)
        settings_db.insert_apply_log(
            actor_label=actor,
            revision_hash_before=result.revision_hash_before,
            revision_hash_after=result.revision_hash_after,
            result="template_apply",
            warnings=result.warnings,
            payload={"template_id": template_id, "diff": plan.cleaned_diff},
            audit_id=audit_id,
        )
        audit.log(
            "settings.template.apply",
            f"template:{template_id}",
            actor=actor,
            outcome="success",
            detail={"audit_id": audit_id, "applied_now": result.applied_now},
        )
        payload = _result_payload(result)
        payload["audit_id"] = audit_id
        payload["template_id"] = template_id
        return payload

    # ------------------------------------------------------------------
    # Baseline capture
    # ------------------------------------------------------------------
    @app.post("/api/settings/baseline/capture")
    async def capture_baseline(request: Request):
        _bearer(request)
        actor = _actor(request)
        snap = dict(STORE.snapshot())
        audit_id = impact_monitor.on_settings_change(
            snap, snap, actor_label=actor, changed_keys=[]
        )
        return {"ok": True, "audit_id": audit_id}

    # ------------------------------------------------------------------
    # Ticket exchange + SSE
    # ------------------------------------------------------------------
    @app.post("/api/settings/stream_ticket")
    async def stream_ticket(body: StreamTicketBody, request: Request):
        _bearer(request)
        actor = _actor(request, body.operator_label)
        ticket, ttl = await _issue_ticket(actor)
        audit.log(
            "settings.stream_ticket.issued",
            "ticket",
            actor=actor,
            outcome="success",
            detail={"ttl_sec": ttl},
        )
        return {"ticket": ticket, "expires_in": ttl}

    @app.get("/api/settings/impact/stream")
    async def impact_stream(ticket: str = Query(...)):
        actor = await _consume_ticket(ticket)
        if actor is None:
            audit.log(
                "settings.stream_ticket.rejected",
                "ticket",
                outcome="denied",
                detail={"reason": "missing_or_expired"},
            )
            raise HTTPException(status_code=401, detail="invalid or expired ticket")

        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        impact_subscribers.append(queue)

        async def _gen():
            try:
                # Initial snapshot.
                report = impact_monitor.current_report()
                if report is not None:
                    yield _sse_pack({"event": "snapshot", "data": report.to_dict()})
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # Heartbeat keeps the connection alive through proxies.
                        yield ": ping\n\n"
                        continue
                    yield _sse_pack(msg)
            finally:
                if queue in impact_subscribers:
                    impact_subscribers.remove(queue)

        return StreamingResponse(_gen(), media_type="text/event-stream")


def _sse_pack(msg: dict[str, Any]) -> str:
    """Serialise one SSE message in the standard wire format."""
    return f"event: {msg.get('event', 'message')}\ndata: {json.dumps(msg.get('data', {}), default=str)}\n\n"


async def _broadcast(subscribers: list[asyncio.Queue], msg: dict[str, Any]) -> None:
    """Best-effort fan-out to every active SSE subscriber."""
    for q in list(subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            # Drop silently; the SSE channel is best-effort.
            pass
