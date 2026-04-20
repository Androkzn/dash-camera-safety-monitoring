"""Audit logging for compliance (GDPR Art. 30, SOC 2).

Records access to sensitive resources — unredacted thumbnails, feedback
submissions, active-learning exports, chat interactions — so the
organisation can demonstrate who accessed what personal data, when.

Storage: append-only JSONL at ``data/audit.jsonl``. Each line is a
self-contained record with actor, action, resource, timestamp, and
outcome. The file is intended for periodic export to a SIEM / log
aggregator in production; the ``/api/audit`` endpoint provides a
read-only tail for the dashboard.

Thread-safe: writes go through a threading lock because DSAR thumbnail
access can happen on any HTTP worker thread.

Append-only invariant
---------------------
This file is **append-only**. The code never rewrites or edits existing
lines, and no operator tool is provided to do so either. That property is
deliberate and load-bearing:

* **Compliance immutability** — regulators (SOC 2 CC7.2, GDPR Art. 30)
  expect audit logs to be tamper-evident. If past entries could be edited,
  the log would be worthless as evidence in an incident investigation.
* **Simple recovery semantics** — appending one line at a time is atomic
  on POSIX filesystems, so a crash mid-write cannot corrupt earlier
  records.
* **Cheap downstream ingestion** — SIEM tools (Splunk, Datadog, ELK) read
  tail-only; they never need to re-scan prior lines.

If you need to "redact" an audit record to satisfy a DSAR-erasure request,
append a compensating ``redaction`` record pointing at the original — do
not edit history.

Env vars
--------
* ``ROAD_AUDIT_LOG`` — truthy by default. Set to ``0``/``false``/``no`` to
  disable audit writes entirely (useful for local development where the
  noise is unhelpful). **In production this should never be disabled.**

Key paths
---------
* ``data/audit.jsonl`` — the append-only audit log read by the dashboard
  via ``/api/audit``.

Python idioms used in this file (one-line explanations)
-------------------------------------------------------
* ``from __future__ import annotations`` — defers type-hint evaluation so
  forward references and ``|`` union syntax work on older interpreters.
* ``pathlib.Path`` — object-oriented file paths; supports ``/`` operator
  for joining (``DATA_DIR / "audit.jsonl"``) and methods like
  ``.exists()`` / ``.open()``.
* ``threading.Lock`` — mutex that guards the shared file so two HTTP
  workers cannot interleave half-written JSON lines.
* ``datetime.now(timezone.utc)`` — current wall-clock time in UTC. We
  prefer timezone-aware datetimes over the deprecated ``datetime.utcnow()``
  which returns a *naive* datetime (no tz info) and is error-prone.
* ``.isoformat()`` — renders a datetime as an ISO 8601 string, e.g.
  ``2024-05-01T12:34:56.789Z`` — the universal, sortable format auditors
  expect.
* ``open("a")`` (the ``"a"`` mode) — opens a file for *append*; writes go
  to the end and the file is created if missing. Never truncates.
* ``json.dumps(..., default=str)`` — serialises to JSON, falling back to
  ``str(obj)`` for types the encoder does not understand (e.g. ``Path``,
  ``UUID``). This is defensive — we never want an audit write to fail
  because some caller passed an unusual object.
* ``try/except`` — Python's error-handling block. We catch ``OSError``
  (disk full, permission denied, etc.) silently here because a broken
  audit log must not break the surrounding request; the alternative is
  losing user-facing functionality for a logging hiccup.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
# json        — JSONL serialisation (one JSON object per line)
# os          — env-var lookup for the enable/disable switch
# threading   — mutex so concurrent workers do not interleave writes
# datetime    — UTC timestamps in ISO 8601 format
# pathlib     — file paths (imported for the type hint below)
# typing.Any  — used in the optional ``detail`` dict type hint
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# DATA_DIR comes from the single-source-of-truth config module.  Never
# compute ``Path(__file__).parent`` here — see the project's Python rules.
from road_safety.config import DATA_DIR

# ---------------------------------------------------------------------------
# Module-level constants (computed once at import time)
# ---------------------------------------------------------------------------
_DATA_DIR = DATA_DIR
# The actual audit file — append-only JSONL.  One JSON object per line.
_AUDIT_PATH = _DATA_DIR / "audit.jsonl"
# Default cap for ``tail()`` — 200 records is enough for the dashboard view
# without blowing up the response payload on a large log.
_MAX_TAIL = 200
# Safety posture: audit is **enabled by default**.  The opt-out is explicit
# (must set to "0"/"false"/"no") so that forgetting the env var does not
# silently disable compliance logging in production.
_ENABLED = os.getenv("ROAD_AUDIT_LOG", "1").lower() not in ("0", "false", "no")

# Single global lock — cheap because audit writes are rare and fast.
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------
def _write(record: dict) -> None:
    """Append one JSON record to the audit log, atomically and under the lock.

    Args:
        record: A plain dict containing the audit fields.  The caller is
            responsible for including the timestamp, action, actor, etc.
            This helper does not add anything — it just writes bytes.

    Returns:
        None.  Silently returns early if auditing is disabled.

    Raises:
        Nothing.  Any ``OSError`` (disk full, permission denied, file locked
        on Windows, etc.) is swallowed so that audit failures never break the
        caller's request path.  The trade-off: we may silently drop an audit
        entry if the disk is in trouble — but keeping the app responsive is
        more important than failing closed on a logging issue.
    """
    # Feature flag — allows local dev or tests to skip audit writes.
    if not _ENABLED:
        return
    try:
        # Ensure the data directory exists.  ``parents=True`` creates any
        # missing intermediate directories; ``exist_ok=True`` turns the call
        # into a no-op if the dir is already there (instead of raising).
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Nested ``with`` blocks: outer acquires the thread lock, inner opens
        # the file.  Both are released automatically when the block exits,
        # even on exception — this is the "context manager" pattern.
        with _lock:
            # Mode "a" = append.  encoding="utf-8" is explicit to avoid
            # platform-dependent defaults (e.g. cp1252 on Windows).
            with _AUDIT_PATH.open("a", encoding="utf-8") as f:
                # ensure_ascii=False keeps non-ASCII chars readable (e.g.
                # plate text fragments) rather than \uXXXX-escaping them.
                # default=str is a safety net for exotic types — see
                # module docstring.
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        # See docstring: we intentionally swallow I/O errors here.
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def log(
    action: str,
    resource: str,
    *,
    actor: str = "system",
    outcome: str = "success",
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Write one audit record.

    The ``*`` in the signature marks everything after it as **keyword-only**
    — callers must pass ``actor=...`` rather than relying on position.
    That is a deliberate readability choice: at the call site you should
    always see ``action="x", resource="y", outcome="denied"`` clearly.

    Args:
        action:   Verb describing what happened.  Canonical vocabulary:
                  ``access_unredacted_thumbnail``, ``submit_feedback``,
                  ``export_active_learning``, ``chat_query``,
                  ``dsar_request``, ``retention_sweep``, ``drift_alert``.
                  New actions are fine but pick from the canonical set
                  first — dashboard filters expect it.
        resource: The object identifier (event_id, thumbnail filename,
                  endpoint path).  Must be enough to unambiguously locate
                  the subject of the access in subsequent investigations.
        actor:    Operator ID or the literal ``"system"`` for automated
                  actions (retention sweeps, schedulers).
        outcome:  ``"success"``, ``"denied"``, or ``"error"``.  Denied
                  attempts are especially important — they appear in the
                  dashboard's "denied" counter.
        detail:   Optional dict for extra context (e.g. the reason for a
                  denial, the size of an export).  Must be JSON-serialisable.
        ip:       Optional client IP, if known.  Emitted when present.

    Returns:
        None.  The function is fire-and-forget; failures are swallowed.

    Raises:
        Nothing — see ``_write`` for the error-handling posture.
    """
    record = {
        # Timestamp format: ISO 8601 in UTC with millisecond precision.
        # ``.isoformat(...)`` gives ``2024-05-01T12:34:56.789+00:00``; we
        # replace ``+00:00`` with ``Z`` to match the "Zulu time" shorthand
        # that most log aggregators and JS ``Date`` parsers prefer.
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "action": action,
        "resource": resource,
        "actor": actor,
        "outcome": outcome,
    }
    # Optional fields are only included when present, to keep each line as
    # small as reasonable — audit logs can get large over months.
    if ip:
        record["ip"] = ip
    if detail:
        record["detail"] = detail
    _write(record)


def tail(n: int = _MAX_TAIL) -> list[dict]:
    """Return the most recent ``n`` audit records, newest last.

    Used by the ``/api/audit`` endpoint to populate the dashboard's audit
    view.  Never edits the file — strictly read-only.

    Args:
        n: Maximum number of records to return.  Defaults to ``_MAX_TAIL``
           (200).  If the file has fewer lines, all are returned.

    Returns:
        A list of decoded JSON objects (dicts).  Empty list if the file
        does not exist yet (common on a fresh install) or cannot be read.

    Raises:
        Nothing.  All I/O and JSON errors degrade to an empty list or skip
        the offending line.
    """
    if not _AUDIT_PATH.exists():
        return []
    try:
        # ``read_text`` reads the whole file in one go.  That is fine for
        # the dashboard's tail view (up to a few MB); if the file grows
        # huge, swap this for a streaming tail reader.
        lines = _AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    # ``lines[-n:]`` is Python's negative-slice idiom — "the last n items".
    # Safe even when ``len(lines) < n`` (returns all of them).
    for raw in lines[-n:]:
        raw = raw.strip()
        if not raw:
            # Blank lines can appear if the process was killed mid-write.
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            # Malformed line — skip silently.  We do not want the dashboard
            # to crash because one historical line got truncated.
            continue
    return out


def stats() -> dict:
    """Summary counts by action type for the dashboard.

    Reads up to the last 1000 records and buckets them for the UI's
    compliance tile.

    Args:
        None.

    Returns:
        A dict with:
          * ``total_records`` — how many records were considered (≤1000).
          * ``by_action``     — dict mapping action name → count.
          * ``denied_count``  — how many of those had outcome="denied".
          * ``audit_enabled`` — whether ``ROAD_AUDIT_LOG`` is on; useful
            for surfacing a warning in the UI when audit is off.

    Raises:
        Nothing — relies on ``tail()`` which degrades silently.
    """
    # 1000-record window is an intentional cap: enough to see trends over
    # a day or two of activity without loading the entire history.
    records = tail(1000)
    by_action: dict[str, int] = {}
    for r in records:
        a = r.get("action", "unknown")
        by_action[a] = by_action.get(a, 0) + 1
    # Generator expression inside ``sum`` — counts records where the
    # outcome is "denied".  A fast way to compute a conditional count.
    denied = sum(1 for r in records if r.get("outcome") == "denied")
    return {
        "total_records": len(records),
        "by_action": by_action,
        "denied_count": denied,
        "audit_enabled": _ENABLED,
    }
