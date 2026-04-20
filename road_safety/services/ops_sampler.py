"""Operational metrics sampler — fps, CPU, memory, LLM spend over time.

Purpose
-------
The Settings Console impact engine compares event-derived metrics (TTC,
confidence, event rate) between a baseline window and an after-change
window. Those are *detection-quality* signals. They do not answer
operational questions that matter when you're tuning TARGET_FPS,
LLM_BUCKET_*, or PERSON_CONF_THRESHOLD:

  * did my change make the pipeline actually run faster?
  * did LLM cost per minute go up or down?
  * did CPU usage move?

This module keeps a small ring buffer of periodic samples so the impact
engine can slice it by window and report ``cpu_p95``, ``actual_fps``,
``llm_cost_usd_per_min`` etc. alongside the detection deltas.

Design
------
* One process-wide :class:`OpsSampler` singleton, driven by a background
  thread (not asyncio) so it keeps sampling even if the event loop is
  busy and so the sample interval is wall-clock deterministic.
* ``snapshot()`` is pure / thread-safe / lock-guarded — callers get a
  shallow copy of the deque.
* Zero dependency on the hot path: the sampler pulls from the LLM
  observer + a caller-supplied ``frames_source`` callable. If psutil is
  missing we still publish fps + LLM metrics — CPU fields come back as
  ``None`` and the UI renders "—".

The sample interval (default 5 s) is coarse on purpose: CPU sampled
more aggressively would double-count the sampler's own work, and the
impact engine aggregates over windows of 5+ minutes anyway.
"""

import logging
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

try:
    import psutil  # type: ignore[import-untyped]
except Exception:  # noqa: BLE001 — optional dependency; tolerate absence.
    psutil = None  # type: ignore[assignment]


# Keep ~1 hour of samples at 5 s cadence = 720 entries. Cheap.
DEFAULT_INTERVAL_SEC = 5.0
DEFAULT_MAX_SAMPLES = 720


@dataclass(frozen=True)
class OpsSample:
    """One point-in-time snapshot of operational metrics.

    Missing / unavailable values are stored as ``None`` (not zero) so the
    aggregator can distinguish "no data yet" from "a true zero".
    """

    ts: float
    fps_actual: float | None
    frames_dropped_ratio: float | None
    cpu_percent: float | None
    memory_percent: float | None
    llm_cost_usd: float
    llm_input_tokens: int
    llm_output_tokens: int
    llm_latency_p95_ms: float
    llm_skip_rate: float
    llm_calls: int


class OpsSampler:
    """Ring-buffer sampler of fps / CPU / LLM spend.

    The sampler is started once at FastAPI lifespan. It owns one worker
    thread that wakes every ``interval_sec`` seconds, builds an
    :class:`OpsSample`, appends it to the deque under the lock, and
    sleeps again. The worker is marked ``daemon=True`` so it never
    blocks process exit.
    """

    def __init__(
        self,
        frames_source: Callable[[], tuple[int, int]],
        llm_stats_fn: Callable[[float], dict[str, Any]],
        *,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        max_samples: int = DEFAULT_MAX_SAMPLES,
    ) -> None:
        """Construct the sampler. Does NOT start the worker; call :meth:`start`.

        Args:
            frames_source: Callable returning ``(frames_read, frames_processed)``
                totals from the active reader(s). If there are multiple
                readers the implementation should sum across them.
            llm_stats_fn: Callable accepting ``window_sec`` and returning
                the dict produced by :class:`LLMObserver.stats`. The
                sampler calls it with a short window (``interval_sec``)
                so per-tick cost is an instantaneous rate, not the
                cumulative since boot.
            interval_sec: Wake interval for the worker thread. Larger =
                coarser data, less sampling overhead. Default 5 s is
                ~0.1% CPU on an M-series laptop.
            max_samples: Ring buffer cap. At ``interval_sec=5`` the
                default holds one hour of history.
        """
        self._frames_source = frames_source
        self._llm_stats_fn = llm_stats_fn
        self._interval = interval_sec
        self._samples: deque[OpsSample] = deque(maxlen=max_samples)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_frames_read: int | None = None
        self._last_frames_processed: int | None = None
        self._last_ts: float | None = None

    def start(self) -> None:
        """Spawn the background sampler thread. Idempotent."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ops_sampler", daemon=True
        )
        self._thread.start()
        log.info(
            "ops_sampler started (interval=%.1fs, max_samples=%d, psutil=%s)",
            self._interval,
            self._samples.maxlen,
            "yes" if psutil is not None else "no",
        )

    def stop(self) -> None:
        """Signal the worker to exit; join up to one interval."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self._interval + 1.0)

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        """Worker body. Sleeps in ``interval`` chunks; wakes on stop."""
        while not self._stop.is_set():
            try:
                self._take_one()
            except Exception as exc:  # noqa: BLE001 — sampler must never die.
                log.warning("ops_sampler tick failed: %s", exc)
            # ``Event.wait`` returns True if ``set()`` was called. Using
            # wait() rather than ``sleep()`` makes ``stop()`` promptly
            # wake the thread instead of waiting out the interval.
            if self._stop.wait(self._interval):
                return

    def _take_one(self) -> None:
        """Build and store one :class:`OpsSample`."""
        now = time.time()
        try:
            frames_read, frames_processed = self._frames_source()
        except Exception as exc:  # noqa: BLE001 — frames source is best-effort.
            log.warning("ops_sampler frames_source failed: %s", exc)
            frames_read = frames_processed = 0

        fps_actual: float | None
        dropped_ratio: float | None
        if self._last_ts is None or self._last_frames_processed is None:
            fps_actual = None
            dropped_ratio = None
        else:
            dt = max(0.001, now - self._last_ts)
            proc_delta = max(0, frames_processed - self._last_frames_processed)
            fps_actual = proc_delta / dt
            if self._last_frames_read is not None:
                read_delta = max(0, frames_read - self._last_frames_read)
                # "frames_dropped_ratio" = of the frames the source produced in
                # this interval, what fraction did the pipeline NOT process.
                # Always non-negative because processed ≤ read by construction.
                if read_delta > 0:
                    dropped_ratio = max(0.0, 1.0 - (proc_delta / read_delta))
                else:
                    dropped_ratio = None
            else:
                dropped_ratio = None
        self._last_ts = now
        self._last_frames_read = frames_read
        self._last_frames_processed = frames_processed

        cpu_pct: float | None = None
        mem_pct: float | None = None
        if psutil is not None:
            try:
                # ``interval=None`` returns the percent since the previous
                # call — non-blocking, and since we sample every interval
                # the reading covers exactly that period.
                cpu_pct = float(psutil.cpu_percent(interval=None))
                mem_pct = float(psutil.virtual_memory().percent)
            except Exception as exc:  # noqa: BLE001
                log.debug("psutil sample failed: %s", exc)

        try:
            # Query the LLM observer over the same interval we sampled fps.
            # Using the full interval (not a longer window) gives a true
            # per-tick rate rather than a trailing moving average.
            llm = self._llm_stats_fn(self._interval)
        except Exception as exc:  # noqa: BLE001
            log.warning("ops_sampler llm stats failed: %s", exc)
            llm = {}

        sample = OpsSample(
            ts=now,
            fps_actual=fps_actual,
            frames_dropped_ratio=dropped_ratio,
            cpu_percent=cpu_pct,
            memory_percent=mem_pct,
            llm_cost_usd=float(llm.get("cost_usd", 0.0) or 0.0),
            llm_input_tokens=int(
                sum(
                    int(t.get("input_tokens", 0) or 0)
                    for t in (llm.get("by_type") or {}).values()
                )
            ),
            llm_output_tokens=int(
                sum(
                    int(t.get("output_tokens", 0) or 0)
                    for t in (llm.get("by_type") or {}).values()
                )
            ),
            llm_latency_p95_ms=float(llm.get("latency_p95_ms", 0.0) or 0.0),
            llm_skip_rate=float(llm.get("skip_rate", 0.0) or 0.0),
            llm_calls=int(llm.get("window_calls", 0) or 0),
        )
        with self._lock:
            self._samples.append(sample)

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------
    def latest(self) -> OpsSample | None:
        """Most recent sample, or ``None`` if the buffer is empty."""
        with self._lock:
            return self._samples[-1] if self._samples else None

    def window_stats(
        self, start_ts: float, end_ts: float
    ) -> dict[str, float | int | None]:
        """Aggregate samples whose timestamp falls in ``[start_ts, end_ts]``.

        Returns a small flat dict suitable for embedding in a :class:`WindowStats`.
        Fields are explicit ``None`` when there is no data — the UI uses
        that to render "—" rather than an unhelpful zero.

        Rate fields (``llm_cost_usd_per_min``, ``llm_tokens_per_min``) are
        reconstructed from the per-interval sample deltas so they are
        correct even if the window contains only one or two samples.
        """
        with self._lock:
            records = [s for s in self._samples if start_ts <= s.ts <= end_ts]

        if not records:
            return {
                "actual_fps_p50": None,
                "actual_fps_p95": None,
                "frames_dropped_ratio_p95": None,
                "cpu_p50": None,
                "cpu_p95": None,
                "memory_p95": None,
                "llm_cost_usd_per_min": None,
                "llm_tokens_per_min": None,
                "llm_latency_p95_ms": None,
                "llm_skip_rate": None,
                "llm_calls": 0,
                "samples": 0,
            }

        fps_vals = [s.fps_actual for s in records if s.fps_actual is not None]
        drop_vals = [
            s.frames_dropped_ratio for s in records if s.frames_dropped_ratio is not None
        ]
        cpu_vals = [s.cpu_percent for s in records if s.cpu_percent is not None]
        mem_vals = [s.memory_percent for s in records if s.memory_percent is not None]
        lat_vals = [s.llm_latency_p95_ms for s in records if s.llm_latency_p95_ms > 0]
        skip_vals = [s.llm_skip_rate for s in records]

        total_cost = sum(s.llm_cost_usd for s in records)
        total_tokens = sum(s.llm_input_tokens + s.llm_output_tokens for s in records)
        total_calls = sum(s.llm_calls for s in records)
        span_sec = max(1.0, records[-1].ts - records[0].ts)
        per_min = 60.0 / span_sec

        return {
            "actual_fps_p50": _p50(fps_vals),
            "actual_fps_p95": _p95(fps_vals),
            "frames_dropped_ratio_p95": _p95(drop_vals),
            "cpu_p50": _p50(cpu_vals),
            "cpu_p95": _p95(cpu_vals),
            "memory_p95": _p95(mem_vals),
            "llm_cost_usd_per_min": round(total_cost * per_min, 6),
            "llm_tokens_per_min": round(total_tokens * per_min, 1),
            "llm_latency_p95_ms": _p95(lat_vals),
            "llm_skip_rate": round(statistics.mean(skip_vals), 4) if skip_vals else None,
            "llm_calls": total_calls,
            "samples": len(records),
        }


# ---------------------------------------------------------------------------
# Percentile helpers — kept local so callers can import only the public
# sampler symbol. ``statistics.quantiles`` needs ≥2 data points, so we
# fall back to the single value when shorter.
# ---------------------------------------------------------------------------
def _p50(xs: list[float]) -> float | None:
    if not xs:
        return None
    return round(float(statistics.median(xs)), 3)


def _p95(xs: list[float]) -> float | None:
    if not xs:
        return None
    if len(xs) == 1:
        return round(float(xs[0]), 3)
    s = sorted(xs)
    idx = int(round(0.95 * (len(s) - 1)))
    return round(float(s[idx]), 3)
