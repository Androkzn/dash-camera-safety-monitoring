"""Background schedulers for tiered Slack alerting.

Two long-lived asyncio tasks:
  * digest_scheduler  — hourly (default), flushes the medium buffer
  * daily_scheduler   — every 24h (default), flushes the low buffer

Intervals are configurable via environment:
    DIGEST_INTERVAL_SEC  (default 3600)  — medium-tier flush cadence
    DAILY_INTERVAL_SEC   (default 86400) — low-tier flush cadence

Intentionally minimal: no cron, no sqlite, no config file. If a flush raises,
we log and keep looping — transient Slack/network errors must not kill the
scheduler.

Role in the wider system
------------------------
The Slack integration uses a three-tier alerting model:

  * **High-risk** events post to Slack immediately (not this module).
  * **Medium-risk** events buffer in memory and are flushed here on an
    hourly "digest" cadence.
  * **Low-risk** events buffer too and flush on a daily roll-up.

Buffering avoids alert fatigue for the operator — a storm of low-severity
detections would otherwise drown out the genuinely urgent ones.

Python idioms used in this file
-------------------------------
* ``async def`` / ``await`` — coroutine functions.  They "pause" at each
  ``await`` so the event loop can run other coroutines meanwhile.
* ``asyncio.Task`` — an object wrapping a running coroutine.
* ``asyncio.AbstractEventLoop`` — the scheduler that juggles coroutines.
* ``asyncio.sleep(n)`` — non-blocking sleep; unlike ``time.sleep`` it
  yields control back to the loop so the server stays responsive.
* ``asyncio.CancelledError`` — special exception raised inside a coroutine
  when its task is cancelled.  Must be re-raised to allow clean shutdown.
* ``tuple[X, Y]`` — a type hint meaning "a two-element tuple of these
  types".  Tuples are immutable sequences.
* ``traceback.print_exc()`` — prints the current exception's full stack,
  useful in background loops where the error otherwise disappears.
* ``f"..."`` — f-string, Python's inline string interpolation syntax.
"""

from __future__ import annotations

import asyncio
import os
import traceback

from road_safety.integrations import slack as slack_notify

# ---------------------------------------------------------------------------
# Interval configuration (env-overridable)
# ---------------------------------------------------------------------------
# Hourly default matches typical ops rhythms: one digest per meeting cadence,
# small enough to be actionable, large enough not to be noisy.
DIGEST_INTERVAL_SEC: int = int(os.getenv("DIGEST_INTERVAL_SEC", "3600"))
# Daily roll-up (86400 s = 24 h) — low-risk items are for trend review,
# not real-time response.
DAILY_INTERVAL_SEC: int = int(os.getenv("DAILY_INTERVAL_SEC", "86400"))

# Idempotency: start_schedulers() may be called more than once (e.g. reload);
# we hand back the existing tasks instead of spawning duplicates.
#
# Module-level mutable state — acceptable here because the server's event
# loop is single-threaded.  Two workers trying to start schedulers would
# still be safe; the second call just returns the first call's handles.
_started: bool = False
_tasks: tuple[asyncio.Task, asyncio.Task] | None = None


async def digest_scheduler(interval_sec: int = DIGEST_INTERVAL_SEC) -> None:
    """Periodically flush the medium-risk buffer as one Slack digest.

    Args:
        interval_sec: Seconds to wait between flushes.  Default from
            ``DIGEST_INTERVAL_SEC``.

    Returns:
        Never — infinite loop by design, meant to be cancelled on shutdown.

    Raises:
        ``asyncio.CancelledError`` — re-raised when the task is cancelled.
        All other exceptions are logged and the loop continues.
    """
    print(f"[digest] medium-risk scheduler online — interval {interval_sec}s")
    while True:
        try:
            # Non-blocking sleep — the event loop can serve HTTP requests
            # and other tasks while we wait.  ``time.sleep`` here would
            # freeze the whole server.
            await asyncio.sleep(interval_sec)
            await slack_notify.flush_medium_digest()
        except asyncio.CancelledError:
            # Re-raise so the surrounding task can exit cleanly.  If we
            # swallowed this, shutdown would hang waiting for us.
            raise
        except Exception as exc:
            # Broad ``except Exception`` is deliberate here: this is a
            # background daemon and the priority is survival, not
            # correctness of a single flush.
            print(f"[digest] medium flush error: {exc}")
            traceback.print_exc()
            # keep going — one bad flush must not tear down the loop


async def daily_scheduler(interval_sec: int = DAILY_INTERVAL_SEC) -> None:
    """Periodically flush the low-risk buffer as a daily Slack summary.

    Mirrors ``digest_scheduler`` but on a 24-hour cadence and drains the
    low-risk buffer instead of the medium one.

    Args:
        interval_sec: Seconds between flushes (default 86400 = 1 day).

    Returns:
        Never — infinite loop.

    Raises:
        ``asyncio.CancelledError`` — re-raised on shutdown.
    """
    print(f"[digest] low-risk daily scheduler online — interval {interval_sec}s")
    while True:
        try:
            await asyncio.sleep(interval_sec)
            await slack_notify.flush_low_daily()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[digest] daily flush error: {exc}")
            traceback.print_exc()


def start_schedulers(
    loop: asyncio.AbstractEventLoop,
) -> tuple[asyncio.Task, asyncio.Task]:
    """Create both scheduler tasks on ``loop``.

    Idempotent — safe to call more than once (e.g. on hot reload);
    subsequent calls return the already-running task handles rather than
    spawning duplicates.

    Args:
        loop: The asyncio event loop to attach the tasks to.  Usually the
              loop FastAPI/uvicorn is running on.

    Returns:
        A tuple ``(medium_task, daily_task)`` — callers can keep these
        around for diagnostics or to await cancellation on shutdown.

    Raises:
        Nothing.
    """
    # ``global`` = "these names refer to module-level variables, not new
    # locals".  Needed because we assign to them inside the function.
    global _started, _tasks
    if _started and _tasks is not None:
        return _tasks
    # ``loop.create_task`` schedules the coroutine to run concurrently and
    # returns a Task handle.  Naming tasks helps in debugging and in the
    # output of ``asyncio.all_tasks()``.
    medium_task = loop.create_task(
        digest_scheduler(DIGEST_INTERVAL_SEC), name="digest_scheduler_medium"
    )
    daily_task = loop.create_task(
        daily_scheduler(DAILY_INTERVAL_SEC), name="digest_scheduler_daily"
    )
    _tasks = (medium_task, daily_task)
    _started = True
    print(
        f"[digest] schedulers started "
        f"(medium={DIGEST_INTERVAL_SEC}s, daily={DAILY_INTERVAL_SEC}s)"
    )
    return _tasks
