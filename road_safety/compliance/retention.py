"""Data retention policy — automatic expiry of old events, thumbnails, and feedback.

GDPR Art. 5(1)(e) requires that personal data be kept only as long as
necessary. Dashcam video thumbnails contain faces and plates (even redacted
copies carry metadata risk). This module enforces configurable retention
windows and runs as a periodic background task.

Retention windows (env-configurable):
  ROAD_RETENTION_THUMBNAILS_DAYS  — delete thumbnails older than N days (default 30)
  ROAD_RETENTION_FEEDBACK_DAYS    — trim feedback.jsonl entries older than N days (default 90)
  ROAD_RETENTION_AL_PENDING_DAYS  — delete stale active-learning samples (default 60)
  ROAD_RETENTION_OUTBOUND_DAYS    — trim outbound_queue.jsonl (default 7)
  ROAD_RETENTION_INTERVAL_SEC     — how often the sweep runs (default 3600 = hourly)

Design: never raises, never blocks the main loop, logs what it removes.

Audit trail
-----------
Every deletion performed here is (in higher-level callers) accompanied by
an entry in the audit log — see ``road_safety/compliance/audit.py``.  The
intent is that sweeps leave a **tamper-evident trail**: you can point an
auditor at the audit log and show exactly which files were removed and
when.  Do not add a path that deletes user data without producing an audit
entry.

Key paths
---------
* ``data/thumbnails/``            — redacted thumbnail cache (JPG/PNG).
* ``data/active_learning/pending/`` — samples awaiting manual review.
* ``data/feedback.jsonl``         — operator TP/FP verdicts.
* ``data/outbound_queue.jsonl``   — edge→cloud delivery queue log.

Python idioms used in this file (one-line explanations)
-------------------------------------------------------
* ``from __future__ import annotations`` — defers type-hint evaluation so
  ``list[str]`` syntax works even on older Python versions.
* ``pathlib.Path`` — object-oriented filesystem paths (see ``audit.py``).
* ``.stat().st_mtime`` — the file's last-modification time as a Unix
  timestamp (seconds since 1970-01-01 UTC).
* ``time.time()`` — current wall-clock seconds since epoch.  Subtracting
  two of those gives an age in seconds; divide by 86400 to get days.
* ``datetime.now(timezone.utc) - timedelta(days=N)`` — "N days ago" as a
  timezone-aware datetime.  Used as a cutoff for JSONL filtering.
* ``datetime.fromisoformat`` — parses an ISO 8601 string back to a
  datetime.  We swap ``Z`` → ``+00:00`` because pre-3.11 Pythons don't
  understand the ``Z`` shorthand.
* ``async def`` / ``await`` — coroutine syntax.  ``retention_loop`` is an
  async function that yields control with ``await asyncio.sleep(...)`` so
  the main FastAPI event loop can serve requests while we wait.
* ``logging.getLogger(name)`` — per-module logger.  Preferred over
  ``print`` because it supports levels, structured fields, and can be
  routed to files or aggregators.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
# asyncio  — for the long-running background ``retention_loop`` coroutine.
# json     — parse each line of the *.jsonl files we trim.
# logging  — structured log emission (see module docstring).
# os       — env-var lookup for the configurable windows.
# time     — file-age maths via ``time.time()``.
# datetime — cutoff calculation for JSONL trimming.
# pathlib  — imported for the type annotation on ``_age_days``.
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# One logger per module is the convention — the name shows up in log output
# so operators can filter for "retention" activity specifically.
logger = logging.getLogger("retention")

# ---------------------------------------------------------------------------
# Configuration — retention windows in days, all env-overridable
# ---------------------------------------------------------------------------
# Defaults were picked to balance two pressures:
#   * storage cost / privacy risk (smaller = safer)
#   * investigation utility (larger = more context for late-arriving claims)
#
# The default thumbnail window (30d) matches typical fleet incident
# retention SLAs: most claims surface within a month.  Feedback needs
# longer (90d) because model-quality analysis is quarterly.  Outbound
# queue is short (7d) because it is purely operational delivery data.
THUMBNAILS_DAYS = int(os.getenv("ROAD_RETENTION_THUMBNAILS_DAYS", "30"))
FEEDBACK_DAYS = int(os.getenv("ROAD_RETENTION_FEEDBACK_DAYS", "90"))
AL_PENDING_DAYS = int(os.getenv("ROAD_RETENTION_AL_PENDING_DAYS", "60"))
OUTBOUND_DAYS = int(os.getenv("ROAD_RETENTION_OUTBOUND_DAYS", "7"))
# Hourly sweep is a good default: small enough that retention violations
# are short-lived, large enough that the cost is negligible (a few file
# stats per hour).
INTERVAL_SEC = int(os.getenv("ROAD_RETENTION_INTERVAL_SEC", "3600"))

# DATA_DIR is the single source of truth for where fleet data lives.  Never
# compute ``Path(__file__).parent`` here — project-wide rule.
from road_safety.config import DATA_DIR


# ===========================================================================
# Private helpers
# ===========================================================================
def _age_days(path: Path) -> float:
    """Return how old a file is, in days, based on its mtime.

    Args:
        path: A ``pathlib.Path`` pointing at a file (or anything
              ``stat()`` can reach).

    Returns:
        The age in days as a float (e.g. 3.72).  Returns ``0.0`` on any
        filesystem error — treating the file as "brand new" so the sweep
        will skip it rather than risk deleting something it can't stat.

    Raises:
        Nothing — ``OSError`` is caught and converted to ``0.0``.
    """
    try:
        # st_mtime is "seconds since the epoch" — divide the difference by
        # 86400 (seconds in a day) to get days.
        mtime = path.stat().st_mtime
        return (time.time() - mtime) / 86400.0
    except OSError:
        return 0.0


# ===========================================================================
# Per-data-type sweeps
# ===========================================================================
def sweep_thumbnails(max_age_days: int = THUMBNAILS_DAYS) -> int:
    """Delete thumbnail images older than ``max_age_days``.

    Args:
        max_age_days: Threshold in days.  Files with an mtime older than
            this are unlinked.  Default comes from
            ``ROAD_RETENTION_THUMBNAILS_DAYS``.

    Returns:
        Count of files actually removed.  ``0`` if the directory is
        missing (common on fresh install) or if nothing aged out.

    Raises:
        Nothing — per-file ``OSError`` is caught so one unremovable file
        does not abort the whole sweep.
    """
    thumbs_dir = DATA_DIR / "thumbnails"
    if not thumbs_dir.exists():
        return 0
    removed = 0
    # ``iterdir()`` yields every direct child (no recursion).  That's
    # intentional — thumbnails live in a flat directory.
    for f in thumbs_dir.iterdir():
        # Safety: only touch regular files with image-like suffixes.
        # Protects against accidentally deleting a sidecar metadata dir.
        if not f.is_file() or not f.suffix in (".jpg", ".jpeg", ".png"):
            continue
        if _age_days(f) > max_age_days:
            try:
                # ``unlink()`` is pathlib's ``rm`` — removes a single file.
                f.unlink()
                removed += 1
            except OSError:
                # File could be locked (Windows), on a read-only FS, or
                # already gone.  Skip and continue.
                pass
    return removed


def sweep_al_pending(max_age_days: int = AL_PENDING_DAYS) -> int:
    """Delete active-learning samples that have gone stale.

    Active-learning samples are candidates queued for human labelling.
    If nobody has labelled them within ``max_age_days``, the model has
    most likely moved on and the sample is no longer useful training data.

    Args:
        max_age_days: Age threshold in days.  Default from
            ``ROAD_RETENTION_AL_PENDING_DAYS``.

    Returns:
        Count of files removed.

    Raises:
        Nothing — per-file ``OSError`` is swallowed.
    """
    pending = DATA_DIR / "active_learning" / "pending"
    if not pending.exists():
        return 0
    removed = 0
    for f in pending.iterdir():
        if not f.is_file():
            continue
        if _age_days(f) > max_age_days:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _trim_jsonl(path: Path, max_age_days: int) -> int:
    """Remove lines from a JSONL file that are older than ``max_age_days``.

    JSONL = "JSON Lines" = one JSON document per line.  We trim by reading
    the whole file, keeping lines whose parsed timestamp is newer than the
    cutoff (or is missing/unparseable — we err on the side of retention),
    then rewriting the file.

    Timestamp key lookup order:
      1. ``operator_ts``  (feedback records)
      2. ``wall_time``    (outbound-queue records)
      3. ``sampled_at``   (active-learning records)

    Args:
        path:         The JSONL file to trim.  Safe if it does not exist.
        max_age_days: Records with a timestamp older than *now - N days*
                      are dropped.

    Returns:
        Count of lines removed.  ``0`` if the file is missing, cannot be
        read, cannot be rewritten, or nothing aged out.

    Raises:
        Nothing — all I/O and parse errors are absorbed.
    """
    if not path.exists():
        return 0
    # Cutoff: anything earlier than this is eligible for removal.
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    try:
        # Read-modify-write pattern: load the whole file into memory.
        # Fine for our scale (feedback/outbound stay in the MB range); if
        # they ever grew huge, a streaming rewrite would be needed.
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0

    kept: list[str] = []
    removed = 0
    for line in lines:
        line = line.strip()
        if not line:
            # Blank lines (e.g. from a crashed write) — discard without
            # counting, since they carry no retained data.
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # Corrupt line — keep it.  We prefer leaving unknown data in
            # place over silently destroying it.  A human operator can
            # investigate later.
            kept.append(line)
            continue
        # ``a or b or c`` evaluates to the first truthy value — the Python
        # idiom for "try these fallbacks in order".
        ts_str = rec.get("operator_ts") or rec.get("wall_time") or rec.get("sampled_at")
        if not ts_str:
            # No timestamp → cannot age it → keep.
            kept.append(line)
            continue
        try:
            # ISO 8601 parsing: pre-3.11 Python cannot handle the ``Z``
            # shorthand, so we convert to the equivalent ``+00:00``.
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts < cutoff:
                removed += 1
                # Note: no kept.append — this line is dropped.
                continue
        except (ValueError, TypeError):
            # Unparseable timestamp → treat as retainable (same safety
            # posture as the "no timestamp" branch).
            pass
        kept.append(line)

    if removed > 0:
        # Only rewrite if we actually changed something — avoids needless
        # disk writes and mtime churn.
        try:
            # Trailing newline preserved only when there is content; an
            # empty file stays empty (not "\n").
            path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        except OSError:
            # If we cannot rewrite, report 0 removals — the file on disk
            # is unchanged.
            return 0
    return removed


def sweep_feedback(max_age_days: int = FEEDBACK_DAYS) -> int:
    """Trim operator-feedback entries older than the retention window.

    Args:
        max_age_days: Days to retain.  Default from
            ``ROAD_RETENTION_FEEDBACK_DAYS``.

    Returns:
        Count of feedback entries removed.
    """
    return _trim_jsonl(DATA_DIR / "feedback.jsonl", max_age_days)


def sweep_outbound(max_age_days: int = OUTBOUND_DAYS) -> int:
    """Trim outbound-queue log entries older than the retention window.

    Args:
        max_age_days: Days to retain.  Default from
            ``ROAD_RETENTION_OUTBOUND_DAYS``.

    Returns:
        Count of queue entries removed.
    """
    return _trim_jsonl(DATA_DIR / "outbound_queue.jsonl", max_age_days)


# ===========================================================================
# Orchestration — single sweep + the long-running background loop
# ===========================================================================
def run_sweep() -> dict:
    """Run all retention sweeps once and return a summary.

    This is the synchronous entry point used by both the periodic
    ``retention_loop`` and any manual admin trigger.

    Args:
        None.

    Returns:
        A dict of counts plus a ``swept_at`` ISO 8601 timestamp.  Emits an
        INFO log line when anything was removed.

    Raises:
        Nothing — each sub-sweep absorbs its own errors.
    """
    results = {
        "thumbnails_removed": sweep_thumbnails(),
        "al_pending_removed": sweep_al_pending(),
        "feedback_trimmed": sweep_feedback(),
        "outbound_trimmed": sweep_outbound(),
        "swept_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    # Sum only the integer count fields, not ``swept_at``.  ``isinstance``
    # check keeps this robust if we add more fields later.
    total = sum(v for v in results.values() if isinstance(v, int))
    if total > 0:
        # "%s" lazy-formatting is the logging idiom — the string is only
        # rendered if the log level is enabled, saving CPU when not.
        logger.info("retention sweep: %s", results)
    return results


async def retention_loop(interval_sec: int = INTERVAL_SEC) -> None:
    """Background coroutine — runs :func:`run_sweep` every ``interval_sec``.

    Lifecycle: launched once on server startup, runs forever until the
    server shuts down (which cancels the task — ``asyncio.CancelledError``
    is re-raised so the cancellation propagates cleanly).

    Args:
        interval_sec: Seconds to wait between sweeps.  Default from
            ``ROAD_RETENTION_INTERVAL_SEC`` (3600 = hourly).

    Returns:
        Never — this coroutine is infinite by design.

    Raises:
        ``asyncio.CancelledError`` — re-raised on shutdown (required for
        clean task cancellation).  All other exceptions are logged and
        swallowed so a single bad sweep cannot kill the loop.
    """
    logger.info("retention loop started (interval=%ds, thumbs=%dd, feedback=%dd, al=%dd, outbound=%dd)",
                interval_sec, THUMBNAILS_DAYS, FEEDBACK_DAYS, AL_PENDING_DAYS, OUTBOUND_DAYS)
    while True:
        try:
            # ``await asyncio.sleep`` yields control to the event loop —
            # other requests continue to be served while we wait.  Do not
            # use ``time.sleep`` here; it would block the whole server.
            await asyncio.sleep(interval_sec)
            run_sweep()
        except asyncio.CancelledError:
            # Re-raise so the surrounding ``asyncio.Task`` can finish
            # cleanly on shutdown.  Catching and suppressing this would
            # turn shutdown into a hang.
            raise
        except Exception as exc:
            # Any other failure: log and keep looping.  Retention errors
            # should never bring down the server.
            logger.warning("retention sweep error: %s", exc)
