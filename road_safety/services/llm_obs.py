"""LLM observability - token spend, latency, and quality tracking.

Role in the system
------------------
Every LLM call (narration, enrichment, chat, completion) flows through
``observer.record()`` in [services.llm]. Nothing in this file initiates
LLM calls - it only *records* them. That separation matters: the
observer can never cause an LLM failure.

Operator questions this layer answers
-------------------------------------
  * How much are we spending?      (token budget burn rate - ``cost_usd``)
  * Which call types cost most?    (narration vs enrichment vs chat)
  * Are we hitting rate limits?    (``skip_rate``)
  * Is something failing?          (``error_rate`` + ``top_errors``)
  * Are we meeting latency SLA?    (``latency_p50_ms`` / ``latency_p95_ms``)

Exposed via
-----------
``/api/llm/stats``   - aggregated (optional ``window_sec`` filter)
``/api/llm/recent``  - last N raw records for debugging

Storage
-------
Append-only in-memory ring buffer capped at ``MAX_RECORDS``. No persistence
across restarts - operators use the /stats endpoint to scrape into their
own telemetry stack if they want history.

Thread-safety
-------------
The ring buffer is guarded by a ``threading.Lock`` (not an asyncio Lock)
because ``server.py``'s frame callback runs in a background thread and
may trigger ``narrate_event`` concurrently with ``chat`` calls running
on the asyncio event loop. Both paths land here.

Python idioms in this file (explained once)
-------------------------------------------
- ``@dataclass`` : auto-generates ``__init__`` / ``__repr__`` / ``__eq__``
  from type-annotated class attributes. ``field(default_factory=...)``
  supplies per-instance defaults (needed for mutable defaults like
  ``time.time()``, which you cannot write as ``timestamp: float = time.time()``
  because that would evaluate once at class-definition time).
- ``@property`` : makes a method callable without ``()`` - lets
  ``rec.estimated_cost_usd`` look like an attribute.
- ``defaultdict(lambda: {...})`` : a dict that materializes a fresh
  default value on first access of a missing key - saves the
  ``if key not in d: d[key] = {...}`` boilerplate.
- ``statistics.median`` : 50th-percentile helper from the stdlib.
- ``str | None`` : PEP 604 union type hint.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# 2000 records ~= a few hours of operation at typical event rates. Large
# enough to spot trends in a /stats call, small enough that the buffer
# itself is a trivial memory footprint (<1MB). When full we drop the oldest
# records on the floor (FIFO via slicing in ``record``).
MAX_RECORDS = 2000

# USD per 1K tokens. These are POLICY not truth - actual billing comes from
# the provider. We use these as a rough cost estimate only; the "default"
# keys exist so a model we don't know about (future deployment) still gets
# a non-zero estimate instead of silently appearing free.
COST_PER_1K_INPUT = {
    "claude-haiku-4-5-20251001": 0.001,
    "claude-sonnet-4-6": 0.003,
    "gpt-4o": 0.005,
    "default": 0.002,
}
COST_PER_1K_OUTPUT = {
    "claude-haiku-4-5-20251001": 0.005,
    "claude-sonnet-4-6": 0.015,
    "gpt-4o": 0.015,
    "default": 0.010,
}


@dataclass
class LLMRecord:
    """One recorded LLM interaction. Immutable after construction.

    Three flavors of record coexist in the buffer:
      * SUCCESS       - ``success=True`` and ``skip_reason is None``.
                        Contributes to latency / cost / token aggregates.
      * FAILURE       - ``success=False``; ``error`` is populated.
                        Contributes to ``error_rate`` and ``top_errors``.
      * SKIP          - ``success=True`` with ``skip_reason`` set (e.g.
                        "rate_budget_exhausted"). NOT counted as latency
                        (no call was made) but contributes to ``skip_rate``.

    The ``timestamp`` uses ``time.time`` (wall clock) - not ``monotonic``
    - because it's displayed to humans in the UI and filtered by windows
    ("last 60 seconds").
    """

    call_type: str       # "narration" | "enrichment" | "chat" | "vision"
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    success: bool
    error: str | None = None
    # ``field(default_factory=time.time)`` defers calling ``time.time`` until
    # each ``LLMRecord()`` is instantiated. A bare ``= time.time()`` would
    # evaluate once at class-definition time, stamping every record with
    # the import time of this module.
    timestamp: float = field(default_factory=time.time)
    event_id: str | None = None
    skip_reason: str | None = None

    @property
    def estimated_cost_usd(self) -> float:
        """Rough $ estimate from ``COST_PER_1K_*`` tables.

        Exposed as a property so call sites read ``rec.estimated_cost_usd``
        like a plain attribute. Unknown models fall back to the ``default``
        rate (see module docstring).
        """
        in_rate = COST_PER_1K_INPUT.get(self.model, COST_PER_1K_INPUT["default"])
        out_rate = COST_PER_1K_OUTPUT.get(self.model, COST_PER_1K_OUTPUT["default"])
        return (self.input_tokens * in_rate + self.output_tokens * out_rate) / 1000.0


class LLMObserver:
    """Ring-buffer collector of ``LLMRecord`` entries.

    State
    -----
    - ``self._records``   : list used as a FIFO ring (trimmed in ``record``).
    - ``self._lock``      : threading lock protecting ``_records`` and
                            the all-time counters.
    - ``self._total_*``   : monotonic all-time counters. Not affected by
                            ring-buffer trimming, so operators always see
                            lifetime totals even when recent detail has
                            rolled off.

    Lifecycle
    ---------
    Instantiated exactly once at module import (``observer = LLMObserver()``
    at the bottom). The singleton is imported by ``services.llm`` and by
    the API routes in ``server.py``.
    """

    def __init__(self, max_records: int = MAX_RECORDS):
        # ``threading.Lock`` (not ``asyncio.Lock``) because records arrive
        # from both asyncio coroutines and worker threads; only a
        # thread-safe primitive covers both.
        self._lock = threading.Lock()
        self._records: list[LLMRecord] = []
        self._max = max_records
        self._total_calls = 0
        self._total_errors = 0
        self._total_skips = 0

    def record(
        self,
        call_type: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        success: bool = True,
        error: str | None = None,
        event_id: str | None = None,
        skip_reason: str | None = None,
    ) -> LLMRecord:
        """Append one record to the ring buffer. Thread-safe. Never raises.

        Called from every branch of ``services.llm``. The function
        intentionally has a broad signature so both success and failure
        paths share one entrypoint.

        Args
        ----
        call_type : str
            One of "narration", "enrichment", "chat", "completion", "vision".
        model : str
            Provider model identifier - drives cost lookup.
        input_tokens, output_tokens : int
            Token counts from the provider's usage response. Default 0
            when we couldn't parse usage (some SDKs don't expose it).
        latency_ms : float
            Wall-clock duration of the call in milliseconds.
        success : bool
            True for successes AND skips; False for actual failures.
        error : str | None
            First line / first 160 chars of the exception (``top_errors``
            groups by this string).
        event_id : str | None
            Event id for traceability in ``/api/llm/recent``.
        skip_reason : str | None
            Non-empty when the call was intentionally not made (e.g.
            "rate_budget_exhausted"). When set, the record counts toward
            ``skip_rate`` and is EXCLUDED from latency aggregates.

        Returns
        -------
        LLMRecord
            The stored record (callers usually ignore the return).
        """
        rec = LLMRecord(
            call_type=call_type,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            success=success,
            error=error,
            event_id=event_id,
            skip_reason=skip_reason,
        )
        # ``with self._lock`` : block until acquired, release on exit.
        # Critical section is minimal (append + trim + counter bumps).
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._max:
                # Slice-assignment is how we cheaply drop the oldest
                # records once the cap is hit.
                self._records = self._records[-self._max:]
            self._total_calls += 1
            if not success:
                self._total_errors += 1
            if skip_reason:
                self._total_skips += 1
        return rec

    def record_skip(
        self,
        call_type: str,
        model: str,
        reason: str,
        event_id: str | None = None,
    ) -> LLMRecord:
        """Convenience wrapper to log a refused call.

        Used by ``services.llm`` when a call is short-circuited client-side
        (rate bucket empty, circuit breaker open, etc.). Modeled as
        ``success=True`` with a ``skip_reason`` so it doesn't inflate the
        error rate but DOES show up in skip rate.
        """
        return self.record(
            call_type=call_type,
            model=model,
            success=True,
            skip_reason=reason,
            event_id=event_id,
        )

    def stats(self, window_sec: float | None = None) -> dict[str, Any]:
        """Aggregate recorded calls into an operator-facing summary.

        Args
        ----
        window_sec : float | None
            If set, only include records whose ``timestamp`` is within the
            last ``window_sec`` seconds. If ``None``, use everything in
            the ring buffer.

        Returns
        -------
        dict[str, Any]
            JSON-ready dict. Keys include ``by_type`` (per call_type:
            calls / errors / skips / tokens / cost / p50 / p95),
            ``by_model``, ``cost_usd``, ``latency_p50_ms``,
            ``latency_p95_ms``, ``error_rate``, ``skip_rate``,
            ``top_errors`` (top 5 by count).

        Edge cases
        ----------
        Empty windows return a zero-valued dict (no division by zero).
        P95 falls back to the single latency when only one sample exists.
        """
        # Snapshot under the lock so the aggregation below runs against
        # a stable copy and doesn't block new records. The snapshot is a
        # shallow copy of the list; ``LLMRecord`` instances are never
        # mutated, so no deep copy is needed.
        with self._lock:
            records = list(self._records)
            total_calls = self._total_calls
            total_errors = self._total_errors
            total_skips = self._total_skips

        now = time.time()
        if window_sec is not None:
            cutoff = now - window_sec
            records = [r for r in records if r.timestamp >= cutoff]

        if not records:
            # Short-circuit: no data in window. Returning zeros rather
            # than None keeps the schema stable for the React client.
            return {
                "window_sec": window_sec,
                "total_calls_all_time": total_calls,
                "total_errors_all_time": total_errors,
                "total_skips_all_time": total_skips,
                "window_calls": 0,
                "by_type": {},
                "by_model": {},
                "cost_usd": 0.0,
                "latency_p50_ms": 0.0,
                "latency_p95_ms": 0.0,
                "error_rate": 0.0,
                "skip_rate": 0.0,
                "top_errors": [],
            }

        # Latency is measured only over records that actually made a call
        # (exclude skips) and that reported a positive latency.
        successful = [r for r in records if r.success and not r.skip_reason]
        latencies = [r.latency_ms for r in successful if r.latency_ms > 0]

        # ``defaultdict(lambda: {...})`` gives each new key a fresh dict
        # without an ``if key not in d`` guard around every write.
        by_type: dict[str, dict] = defaultdict(lambda: {
            "calls": 0, "errors": 0, "skips": 0,
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            "latencies_ms": [],
        })
        by_model: dict[str, dict] = defaultdict(lambda: {
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
        })
        error_counts: dict[str, int] = defaultdict(int)

        total_cost = 0.0
        for r in records:
            # Per-call_type aggregation (narration / enrichment / chat / ...).
            ct = by_type[r.call_type]
            ct["calls"] += 1
            if not r.success:
                ct["errors"] += 1
                if r.error:
                    # Group errors by their first line, capped at 160
                    # chars. Tracebacks differ per-call in line numbers
                    # but share a first line - we use that as a cheap
                    # fingerprint for ``top_errors``.
                    key = str(r.error).strip().splitlines()[0][:160]
                    if key:
                        error_counts[key] += 1
            if r.skip_reason:
                ct["skips"] += 1
            ct["input_tokens"] += r.input_tokens
            ct["output_tokens"] += r.output_tokens
            cost = r.estimated_cost_usd
            ct["cost_usd"] += cost
            if r.success and not r.skip_reason and r.latency_ms > 0:
                ct["latencies_ms"].append(r.latency_ms)

            # Per-model aggregation (used to see the Haiku/Sonnet split).
            bm = by_model[r.model]
            bm["calls"] += 1
            bm["input_tokens"] += r.input_tokens
            bm["output_tokens"] += r.output_tokens
            bm["cost_usd"] += cost
            total_cost += cost

        # Second pass: finalize per-type dicts (pop the temp latency list,
        # replace with p50/p95, round cost to 6 decimals for display).
        by_type_out = {}
        for k, v in by_type.items():
            lats = v.pop("latencies_ms")
            v["cost_usd"] = round(v["cost_usd"], 6)
            if lats:
                v["latency_p50_ms"] = round(statistics.median(lats), 1)
                # Simple nearest-rank P95: sort, index ``int(len*0.95)``.
                # Good enough for an operator dashboard; not a statistically
                # rigorous percentile but cheap and monotonic.
                v["latency_p95_ms"] = round(
                    sorted(lats)[int(len(lats) * 0.95)], 1
                ) if len(lats) >= 2 else round(lats[0], 1)
            by_type_out[k] = v

        for v in by_model.values():
            v["cost_usd"] = round(v["cost_usd"], 6)

        errors_in_window = sum(1 for r in records if not r.success)
        skips_in_window = sum(1 for r in records if r.skip_reason)

        return {
            "window_sec": window_sec,
            "total_calls_all_time": total_calls,
            "total_errors_all_time": total_errors,
            "total_skips_all_time": total_skips,
            "window_calls": len(records),
            "by_type": by_type_out,
            "by_model": dict(by_model),
            "cost_usd": round(total_cost, 6),
            "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
            "latency_p95_ms": (
                round(sorted(latencies)[int(len(latencies) * 0.95)], 1)
                if len(latencies) >= 2
                else round(latencies[0], 1) if latencies else 0.0
            ),
            "error_rate": round(errors_in_window / len(records), 4) if records else 0.0,
            "skip_rate": round(skips_in_window / len(records), 4) if records else 0.0,
            "top_errors": [
                {"error": error, "count": count}
                for error, count in sorted(error_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
            ],
        }

    def recent(self, n: int = 50) -> list[dict]:
        """Return the last ``n`` records as JSON-ready dicts.

        Used by ``/api/llm/recent`` for the debug panel in the admin UI.
        Snapshots the tail under the lock then builds the dicts outside
        the lock to keep the critical section short.
        """
        with self._lock:
            tail = self._records[-n:]
        return [
            {
                "call_type": r.call_type,
                "model": r.model,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "latency_ms": round(r.latency_ms, 1),
                "success": r.success,
                "error": r.error,
                "event_id": r.event_id,
                "skip_reason": r.skip_reason,
                "cost_usd": round(r.estimated_cost_usd, 6),
                "timestamp": r.timestamp,
            }
            for r in tail
        ]


# Module-level singleton. Imported by ``services.llm`` as ``llm_observer``
# and by ``server.py`` to serve the /api/llm/* endpoints.
observer = LLMObserver()
