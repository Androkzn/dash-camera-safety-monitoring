"""Settings Console — template service.

Operator-named presets of full settings dicts. Built on
:mod:`road_safety.services.settings_db` for storage and
:mod:`road_safety.settings_spec` for the canonical schema. The single most
load-bearing function here is :func:`apply_template`, which performs the
spec-migration + re-validation dance documented in the plan §S3 — old
templates must not silently bypass new validators.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from road_safety import settings_spec
from road_safety.services import settings_db


# Synthetic "default" template id. It is not stored in SQLite — we render it
# on the fly from `SETTINGS_SPEC.defaults()` so it never drifts.
DEFAULT_TEMPLATE_ID = "tpl_default"


@dataclass
class TemplateApplyPlan:
    """What :func:`prepare_template_apply` returned.

    Attributes:
        cleaned_diff: The diff that should be passed to
            :meth:`SettingsStore.apply_diff`. Already migrated and
            validated against the *current* schema.
        dropped_keys: Keys from the stored payload that no longer exist
            in :data:`SETTINGS_SPEC` (audit-logged at apply time).
        filled_keys: Keys that the stored payload was missing; filled
            from current spec defaults (audit-logged).
        validation_errors: Cross-field or per-key validation errors, if any.
            Non-empty means the apply MUST be rejected with 422.
    """

    cleaned_diff: dict[str, Any]
    dropped_keys: list[str]
    filled_keys: list[str]
    validation_errors: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Hashing helper
# ---------------------------------------------------------------------------
def _hash_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _new_id() -> str:
    return f"tpl_{secrets.token_hex(6)}"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def create_template(
    *,
    name: str,
    description: str,
    payload: dict[str, Any],
    actor_label: str,
) -> dict[str, Any]:
    """Create a new operator template at revision 1.

    The ``payload`` is **not** validated here — operators are allowed to
    save partial templates (e.g. only the LLM-cost knobs). Validation runs
    at apply time, against the current spec, with migration applied.

    Returns the stored template metadata + its initial revision.
    """
    if not name.strip():
        raise ValueError("template name cannot be empty")
    template_id = _new_id()
    return settings_db.insert_template(
        template_id=template_id,
        name=name.strip(),
        description=description.strip(),
        payload=payload,
        schema_version=settings_spec.SCHEMA_VERSION,
        payload_hash=_hash_payload(payload),
        actor_label=actor_label,
        system=False,
    )


def update_template(
    template_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    payload: dict[str, Any] | None = None,
    actor_label: str = "system",
) -> dict[str, Any]:
    """Patch metadata and/or append a new immutable revision."""
    if template_id == DEFAULT_TEMPLATE_ID:
        raise PermissionError("default template is read-only")
    return settings_db.update_template(
        template_id,
        name=name,
        description=description,
        payload=payload,
        schema_version=settings_spec.SCHEMA_VERSION if payload is not None else None,
        payload_hash=_hash_payload(payload) if payload is not None else None,
        actor_label=actor_label,
    )


def soft_delete_template(template_id: str) -> bool:
    """Soft-delete; raises ``PermissionError`` for system templates."""
    if template_id == DEFAULT_TEMPLATE_ID:
        raise PermissionError("default template cannot be deleted")
    return settings_db.soft_delete_template(template_id)


def get_template(template_id: str) -> dict[str, Any] | None:
    """Return one template + its latest revision payload, or ``None``.

    Synthesizes :data:`DEFAULT_TEMPLATE_ID` from current spec defaults.
    """
    if template_id == DEFAULT_TEMPLATE_ID:
        return _synthetic_default_template()
    return settings_db.get_template(template_id)


def list_templates() -> list[dict[str, Any]]:
    """Return all live templates with the synthetic default at the head."""
    out: list[dict[str, Any]] = [_synthetic_default_template()]
    out.extend(settings_db.list_templates())
    return out


def list_revisions(template_id: str) -> list[dict[str, Any]]:
    """Full revision history for a template; default template has none."""
    if template_id == DEFAULT_TEMPLATE_ID:
        return []
    return settings_db.list_revisions(template_id)


def _synthetic_default_template() -> dict[str, Any]:
    payload = settings_spec.defaults()
    return {
        "id": DEFAULT_TEMPLATE_ID,
        "name": "Default",
        "description": "Built-in defaults from the spec registry. Always available, never editable.",
        "system": True,
        "soft_deleted_at": None,
        "created_at": 0.0,
        "updated_at": 0.0,
        "payload": payload,
        "latest_revision_no": 0,
        "payload_hash": _hash_payload(payload),
        "schema_version": settings_spec.SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# Apply preparation (migration + re-validation)
# ---------------------------------------------------------------------------
def prepare_template_apply(
    template_id: str,
    *,
    current_snapshot: dict[str, Any],
) -> TemplateApplyPlan:
    """Compute the cleaned diff to feed into the store for a template apply.

    Steps (per plan §S3):

    1. Drop keys not in the *current* :data:`SETTINGS_SPEC`.
    2. Fill keys present in the spec but missing from the template with
       current defaults.
    3. Coerce values to the spec's declared type (so a stored string for a
       float-typed key is repaired without a 422).
    4. Validate the prospective merged snapshot.

    Returns a :class:`TemplateApplyPlan` describing the outcome. The caller
    audit-logs the dropped/filled key lists and 422s on non-empty
    ``validation_errors``.
    """
    if template_id == DEFAULT_TEMPLATE_ID:
        stored_payload = settings_spec.defaults()
    else:
        latest = settings_db.latest_revision(template_id)
        if latest is None:
            raise KeyError(template_id)
        stored_payload = latest["payload"]

    valid_keys = set(settings_spec.all_keys())
    stored_keys = set(stored_payload.keys())

    dropped = sorted(stored_keys - valid_keys)
    filled = sorted(valid_keys - stored_keys)

    cleaned: dict[str, Any] = {}
    for key in valid_keys:
        if key in stored_payload:
            try:
                cleaned[key] = settings_spec.coerce(key, stored_payload[key])
            except (TypeError, ValueError) as exc:
                cleaned[key] = settings_spec.spec_for(key).default
                dropped.append(f"{key} (coercion failed: {exc})")
        else:
            cleaned[key] = settings_spec.spec_for(key).default

    # Build the prospective merged snapshot for validation.
    merged = dict(current_snapshot)
    merged.update(cleaned)
    errors = settings_spec.validate(merged)

    # The diff we hand to the store is only the keys that *change* —
    # otherwise we churn subscribers for no-ops.
    diff: dict[str, Any] = {
        k: v for k, v in cleaned.items() if current_snapshot.get(k) != v
    }
    return TemplateApplyPlan(
        cleaned_diff=diff,
        dropped_keys=dropped,
        filled_keys=filled,
        validation_errors=errors,
    )
