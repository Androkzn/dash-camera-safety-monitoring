"""Operator feedback API — thumbs-up / thumbs-down verdicts per event.

Responsibility
--------------
Let a human operator tell the system "this event was a real near-miss"
(true-positive, ``tp``) or "this event was a false alarm"
(false-positive, ``fp``). Over time this feedback stream drives:
    - drift monitoring (precision slipping over time?)
    - active-learning sample selection (which events to hand-label next?)
    - per-driver coaching queues (which medium-risk events need a human
      review before going into a driver debrief?)

Mounted from ``server.py`` via ``feedback.mount(app)``. Writes are
append-only JSON-lines to ``data/feedback.jsonl`` — downstream
drift-monitoring jobs can tail that file (just like following a log).

Endpoints registered by ``mount(app)``:
    POST /api/feedback          — record a verdict
    GET  /api/feedback          — last 100 feedback lines, parsed
    GET  /api/coaching_queue    — pending medium-risk events for review

Files read / written
--------------------
- ``data/feedback.jsonl`` (append + tail read) — operator verdicts.
- ``data/events.json`` (read only, fallback) — used when the in-memory
  medium buffer is empty (e.g. just after a server restart) so the
  coaching queue still has something to show.

Environment variables
---------------------
None directly. Indirectly: ``DATA_DIR`` resolves from ``road_safety.config``
which reads ``ROAD_DATA_DIR``.
"""

# ``from __future__ import annotations`` — evaluate type hints lazily so
# ``str | None`` works and forward references are cheap.
from __future__ import annotations

# Standard library first, then third-party, then local — blank line
# between groups per project convention.
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

# FastAPI — the async web framework this service is built on. We import
# only the tiny surface we use: the app class and the standard HTTPException.
from fastapi import FastAPI, HTTPException
# pydantic is a runtime validation / parsing library. Subclassing
# ``BaseModel`` gives us an object whose fields are type-checked at
# request time — FastAPI automatically hooks it up for JSON body parsing.
from pydantic import BaseModel, Field

# Local: single source of truth for filesystem paths. Never compute
# ``Path(__file__).parent`` in modules — always import from config.
from road_safety.config import DATA_DIR
from road_safety.integrations import slack as slack_notify

# Type alias for the optional post-write hook. Reads as "a callable that
# takes (record: dict, matched: Optional[dict]) and returns an Awaitable
# producing None". Naming this once keeps the function signatures below
# readable.
FeedbackHook = Callable[[dict, Optional[dict]], Awaitable[None]]

# Module-private (leading underscore) file paths. All resolved via the
# config module so relocating the data dir in one place updates every
# consumer.
_DATA_DIR = DATA_DIR
_FEEDBACK_PATH = _DATA_DIR / "feedback.jsonl"
_EVENTS_PATH = _DATA_DIR / "events.json"

# Why only "tp" / "fp"?
#   * Binary is the only shape that yields useful precision/recall math
#     downstream. Tri-state ("unsure") muddies every derived metric.
#   * Matches what the React feedback component emits.
#   * Pydantic's ``Literal["tp", "fp"]`` enforces this at request time,
#     so this set is a belt-and-braces second check (see post_feedback).
_VALID_VERDICTS = {"tp", "fp"}


class FeedbackBody(BaseModel):
    """Pydantic model for the POST /api/feedback request body.

    Pydantic validates incoming JSON against these field declarations and
    raises a 422 response automatically for any that fail. Using
    ``Field(...)`` provides extra constraints (min/max length) on top of
    the type annotation.
    """

    # ``Field(..., min_length=..., max_length=...)``: the ``...`` literal
    # is Python's ``Ellipsis`` and here means "this field is REQUIRED,
    # no default". Length bounds catch empty strings and prevent a caller
    # from pushing a megabyte of "event_id" into the log.
    event_id: str = Field(..., min_length=1, max_length=128)
    # ``Literal["tp", "fp"]`` restricts the value to exactly those two
    # strings — pydantic rejects anything else at parse time.
    verdict: Literal["tp", "fp"]
    # Optional note: 2000-char cap is a sanity bound — operator comments
    # are usually one line, never paragraphs.
    note: str | None = Field(default=None, max_length=2000)


async def _safe_hook(hook: FeedbackHook, record: dict, matched: dict | None) -> None:
    """Run the optional post-write hook without letting it crash the request.

    Hooks are dispatched via ``asyncio.create_task`` from the HTTP
    handler, so any exception raised here would otherwise end up as an
    unhandled-task warning and potentially a leaked traceback. We catch
    broadly and log at WARNING — this is glue code, not application logic.
    """
    try:
        await hook(record, matched)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("feedback hook failed: %s", exc)


def _append_feedback(record: dict) -> None:
    """Append a single feedback record as one JSON line.

    Synchronous on purpose — feedback writes are rare (human-driven), the
    file is tiny, and using the async executor here would add complexity
    without meaningful throughput gain.

    ``ensure_ascii=False`` keeps non-ASCII characters (e.g. accented
    driver names in operator notes) as their real UTF-8 bytes rather than
    ``\\uXXXX`` escapes.
    """
    # ``parents=True, exist_ok=True`` — create the data dir if missing
    # without raising on a second call.
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Context manager (``with ... as f``) guarantees the file handle is
    # closed even if ``f.write`` raises. ``"a"`` opens for append.
    with _FEEDBACK_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_tail(path: Path, n: int) -> list[dict]:
    """Return the last ``n`` parsed JSON lines from ``path``.

    Malformed lines and missing files are treated as empty — the tail
    endpoint must never fail the whole request because of one bad line
    somewhere in history. Operator tooling reads this; hardness matters.
    """
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    # ``lines[-n:]`` = last n lines; slicing tolerates n > len(lines).
    for raw in lines[-n:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            # Skip malformed lines silently. The drift-monitor job that
            # primarily consumes this file does its own stricter parsing.
            continue
    return out


def _medium_events_from_disk(limit: int) -> list[dict]:
    """Fallback when the in-memory buffer is empty: replay events.json.

    After a server restart the in-memory Slack medium buffer is empty,
    but operators still expect the coaching queue to show recent
    medium-risk events. We replay the persisted events snapshot and
    filter by ``risk_level == "medium"`` to approximate what the buffer
    would have held.

    Defensive against both the legacy shape (``{"events": [...]}``) and
    the newer flat-list shape — isinstance checks cover both.
    """
    if not _EVENTS_PATH.exists():
        return []
    try:
        with _EVENTS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("events") or []
    if not isinstance(data, list):
        return []
    mediums = [e for e in data if isinstance(e, dict) and e.get("risk_level") == "medium"]
    return mediums[-limit:]


def mount(
    app: FastAPI,
    on_feedback: FeedbackHook | None = None,
    event_lookup: Callable[[str], dict | None] | None = None,
) -> None:
    """Register feedback + coaching-queue routes on the given FastAPI app.

    This function is the public surface of the module. Its body defines
    nested async functions decorated with ``@app.post`` / ``@app.get``,
    which FastAPI registers as HTTP routes during this call. Closing
    over the ``on_feedback`` and ``event_lookup`` arguments is a clean
    way to inject dependencies without needing a DI framework.

    Args:
        app: the shared ``FastAPI`` instance from ``server.py``.
        on_feedback: optional async callback fired after each verdict
            write, for drift recompute / active-learning sampling. Never
            blocks the HTTP response — fired via ``asyncio.create_task``.
            Signature: ``(record: dict, matched: dict | None) -> None``.
        event_lookup: optional sync resolver from ``event_id`` to the full
            event dict (e.g. a closure over ``state.recent_events``).
            Called synchronously just before firing the hook so the hook
            can know which vehicle the feedback applies to, for reporting.
    """
    # Local import: only needed inside the nested route; keeping it out of
    # module top-level reduces import-time cost for consumers that never
    # call ``mount``.
    import asyncio

    # ``@app.post("/api/feedback")`` is a decorator that attaches the
    # decorated async function as the handler for HTTP POST requests to
    # that path. FastAPI inspects the function's parameter annotations
    # (here: ``body: FeedbackBody``) to know how to parse the request —
    # in this case it pulls JSON from the request body and validates it
    # against the pydantic model before our code ever runs.
    @app.post("/api/feedback")
    async def post_feedback(body: FeedbackBody):
        """Record a single operator verdict.

        Returns:
            ``{"ok": True}`` on success.

        Raises:
            HTTPException(400): verdict not in the allowed set. Normally
                pydantic catches this first; the check below is
                defence-in-depth.
            HTTPException(500): disk write failed — surfaced loudly so
                the operator knows their click did not persist.
        """
        if body.verdict not in _VALID_VERDICTS:
            # pydantic Literal normally catches this; belt-and-braces.
            raise HTTPException(status_code=400, detail="verdict must be 'tp' or 'fp'")
        record = {
            "event_id": body.event_id,
            "verdict": body.verdict,
            "note": body.note,
            # ``datetime.now(timezone.utc).isoformat()`` = RFC 3339 UTC
            # timestamp, e.g. ``"2026-04-18T12:34:56.789+00:00"``. UTC is
            # non-negotiable for cross-fleet analytics.
            "operator_ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _append_feedback(record)
        except OSError as exc:
            # ``raise ... from exc`` preserves the original traceback so
            # operators see the underlying FS error in the server log.
            raise HTTPException(status_code=500, detail=f"write failed: {exc}") from exc
        if on_feedback is not None:
            # Match the event_id against the recent in-memory event buffer
            # (via the caller-supplied lookup closure) so the hook can
            # pull vehicle_id / driver_id for reporting without re-reading
            # events.json from disk.
            matched = event_lookup(body.event_id) if event_lookup else None
            # ``asyncio.create_task`` schedules the coroutine to run on the
            # event loop WITHOUT awaiting it. The HTTP response returns
            # immediately; the hook runs in the background. ``_safe_hook``
            # ensures any exception inside the hook gets logged, not lost.
            asyncio.create_task(_safe_hook(on_feedback, record, matched))
        return {"ok": True}

    @app.get("/api/feedback")
    async def get_feedback():
        """Return the last 100 feedback records, most recent last.

        100 is a UX choice: enough to show a few days of operator
        activity, small enough to render in a single request.
        """
        return {"items": _read_tail(_FEEDBACK_PATH, 100)}

    @app.get("/api/coaching_queue")
    async def coaching_queue(limit: int = 50):
        """Return pending medium-risk events for operator review.

        Query params:
            limit: max items to return. Must be positive; capped at 500
                to prevent DoS-via-massive-response.

        Fallback behaviour: when the in-memory Slack medium buffer is
        empty (e.g. immediately after a server restart), we replay
        ``events.json`` and filter by ``risk_level == "medium"`` so the
        operator UI always has something to show.
        """
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be positive")
        # ``min(limit, 500)`` caps the max. This is a common idiom for
        # "accept user input but clamp it to a safe bound".
        limit = min(limit, 500)
        # Prefer the live in-memory buffer (what Slack would have sent);
        # fall back to disk for cold starts / restarts.
        buf = slack_notify.get_medium_buffer()
        if buf:
            items = buf[-limit:]
        else:
            items = _medium_events_from_disk(limit)
        return {"items": items, "count": len(items)}
