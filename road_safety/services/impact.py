"""Settings Console — baseline + impact engine.

Captures a `WindowStats` snapshot before any settings change, then keeps
sampling the live event ring after the change to compute deterministic
deltas with explicit comparability gates.

Two metric classes are kept distinct (per plan §S4):

* **immediate** — counts and distributions readable from the live event
  buffer the moment they arrive (event-rate, severity ratios, scene mix,
  quality mix, confidence percentiles).
* **lagging** — anything that needs operator feedback (drift precision,
  FP rate from feedback). Surfaced separately and labeled
  ``"awaiting feedback"`` until enough verdicts accrue.

Comparability between baseline and after-window is gated by the algorithm
spelled out in plan §S4: minimum sample size, scene-mix Jensen-Shannon
divergence, and quality-state similarity. The output ``confidence_tier``
is one of ``"high" | "medium" | "low" | "insufficient"``.

Persistence: every tick upserts the session into
:mod:`road_safety.services.settings_db` so a server restart inside the
monitoring window does not lose the operator's experiment.
"""

from __future__ import annotations

import math
import secrets
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

from road_safety.services import settings_db


# ---------------------------------------------------------------------------
# Tunables for the engine itself (intentionally NOT in SETTINGS_SPEC)
# ---------------------------------------------------------------------------
MIN_BASELINE_EVENTS = 20
MIN_AFTER_EVENTS = 20
MIN_FEEDBACK = 5
SCENE_JSD_THRESHOLD = 0.20         # above => "scene_mix_drift" reason
QUALITY_SIMILARITY_FLOOR = 0.6     # below => "quality_drift" reason
WINDOW_LOOKBACKS_SEC = (300.0, 600.0, 1200.0, 1800.0)
COALESCE_WINDOW_SEC = 30.0
IMPACT_TICK_SEC = 15.0
IMPACT_SESSION_MAX_AGE_SEC = 3600.0
UNATTENDED_BANNER_LEAD_SEC = 600.0


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------
@dataclass
class WindowStats:
    """Deterministic stats over one observation window (baseline or after)."""

    window_start_ts: float
    window_end_ts: float
    duration_sec: float
    sample_size: int
    event_rate_per_min: float
    severity_counts: dict[str, int] = field(default_factory=dict)
    severity_ratios: dict[str, float] = field(default_factory=dict)
    confidence_p50: float | None = None
    confidence_p95: float | None = None
    ttc_p50: float | None = None
    ttc_p95: float | None = None
    distance_p50_m: float | None = None
    distance_p95_m: float | None = None
    scene_distribution: dict[str, float] = field(default_factory=dict)
    quality_distribution: dict[str, float] = field(default_factory=dict)
    fp_rate: float | None = None
    fp_rate_source: str = "insufficient"  # "feedback" | "proxy" | "insufficient"
    # --- operational metrics (from ops_sampler; None when unavailable) ----
    # These describe how the pipeline is running — actual fps, CPU, LLM
    # spend — as opposed to the event-derived fields above. They let an
    # operator see whether a setting change made things cheaper / faster /
    # heavier, not just whether it shifted the risk tier distribution.
    actual_fps_p50: float | None = None
    actual_fps_p95: float | None = None
    frames_dropped_ratio_p95: float | None = None
    cpu_p50: float | None = None
    cpu_p95: float | None = None
    memory_p95: float | None = None
    llm_cost_usd_per_min: float | None = None
    llm_tokens_per_min: float | None = None
    llm_latency_p95_ms: float | None = None
    llm_skip_rate: float | None = None
    llm_calls: int = 0
    ops_samples: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImpactReport:
    """One ``GET /api/settings/impact`` payload."""

    audit_id: str
    change_ts: float
    actor_label: str
    before: dict[str, Any]
    after: dict[str, Any]
    changed_keys: list[str]
    baseline: WindowStats | None
    after_window: WindowStats | None
    deltas: dict[str, float] = field(default_factory=dict)
    confidence_tier: str = "insufficient"  # high | medium | low | insufficient
    confidence_reasons: list[str] = field(default_factory=list)
    immediate_metrics: list[str] = field(default_factory=list)
    lagging_metrics: list[str] = field(default_factory=list)
    state: str = "monitoring"
    warnings: list[str] = field(default_factory=list)
    last_good: dict[str, Any] = field(default_factory=dict)
    narrative: str | None = None
    recommendation: str | None = None  # keep | revert | monitor

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if self.baseline is not None:
            out["baseline"] = self.baseline.to_dict()
        if self.after_window is not None:
            out["after_window"] = self.after_window.to_dict()
        return out


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------
def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return float(s[idx])


def _normalize_dist(counts: Mapping[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in counts.items()}


def jensen_shannon_distance(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    """Symmetrized KL divergence in [0, 1].

    Returns 0.0 when both distributions are identical (or both empty),
    1.0 when fully disjoint. Uses base-2 log so the value is bounded by 1.
    """
    keys = set(p) | set(q)
    if not keys:
        return 0.0

    def _kl(a: Mapping[str, float], b: Mapping[str, float]) -> float:
        total = 0.0
        for k in keys:
            ak = a.get(k, 0.0)
            bk = b.get(k, 0.0)
            if ak > 0 and bk > 0:
                total += ak * math.log2(ak / bk)
        return total

    m = {k: 0.5 * (p.get(k, 0.0) + q.get(k, 0.0)) for k in keys}
    jsd = 0.5 * _kl(p, m) + 0.5 * _kl(q, m)
    # Numerical floor: tiny negative values from rounding can sneak in.
    return max(0.0, min(1.0, jsd))


# ---------------------------------------------------------------------------
# Window computation
# ---------------------------------------------------------------------------
def compute_window(
    events: Iterable[dict[str, Any]],
    *,
    start_ts: float,
    end_ts: float,
    ops_stats: Mapping[str, Any] | None = None,
) -> WindowStats:
    """Roll a list of event dicts into a :class:`WindowStats`.

    Each event is expected to expose (best-effort) the fields:
    ``risk`` (low/medium/high), ``timestamp_sec`` or ``ts``,
    ``confidence``, ``ttc_sec``, ``distance_m``, ``scene_label`` /
    ``scene``, ``quality_state`` / ``perception_state``. Missing fields
    are tolerated — the corresponding stat is just ``None``.

    ``ops_stats`` is the dict returned by
    :meth:`OpsSampler.window_stats` (or ``None`` when the sampler is not
    wired). Its fields are copied into the operational-metric slots on
    the returned :class:`WindowStats`.
    """
    duration = max(0.001, end_ts - start_ts)
    severity_counts: Counter[str] = Counter()
    confidences: list[float] = []
    ttcs: list[float] = []
    dists: list[float] = []
    scenes: Counter[str] = Counter()
    quality: Counter[str] = Counter()

    sample = 0
    for ev in events:
        ts = float(ev.get("timestamp_sec") or ev.get("ts") or 0.0)
        if ts and (ts < start_ts or ts > end_ts):
            continue
        sample += 1
        severity_counts[str(ev.get("risk") or ev.get("severity") or "unknown")] += 1
        c = ev.get("confidence")
        if isinstance(c, (int, float)):
            confidences.append(float(c))
        t = ev.get("ttc_sec")
        if isinstance(t, (int, float)):
            ttcs.append(float(t))
        d = ev.get("distance_m")
        if isinstance(d, (int, float)):
            dists.append(float(d))
        scene = ev.get("scene_label") or ev.get("scene")
        if scene:
            scenes[str(scene)] += 1
        q = ev.get("quality_state") or ev.get("perception_state")
        if q:
            quality[str(q)] += 1

    rate_per_min = (sample / duration) * 60.0 if duration > 0 else 0.0
    ops = ops_stats or {}
    return WindowStats(
        window_start_ts=start_ts,
        window_end_ts=end_ts,
        duration_sec=duration,
        sample_size=sample,
        event_rate_per_min=rate_per_min,
        severity_counts=dict(severity_counts),
        severity_ratios=_normalize_dist(severity_counts),
        confidence_p50=_percentile(confidences, 50),
        confidence_p95=_percentile(confidences, 95),
        ttc_p50=_percentile(ttcs, 50),
        ttc_p95=_percentile(ttcs, 95),
        distance_p50_m=_percentile(dists, 50),
        distance_p95_m=_percentile(dists, 95),
        scene_distribution=_normalize_dist(scenes),
        quality_distribution=_normalize_dist(quality),
        actual_fps_p50=ops.get("actual_fps_p50"),
        actual_fps_p95=ops.get("actual_fps_p95"),
        frames_dropped_ratio_p95=ops.get("frames_dropped_ratio_p95"),
        cpu_p50=ops.get("cpu_p50"),
        cpu_p95=ops.get("cpu_p95"),
        memory_p95=ops.get("memory_p95"),
        llm_cost_usd_per_min=ops.get("llm_cost_usd_per_min"),
        llm_tokens_per_min=ops.get("llm_tokens_per_min"),
        llm_latency_p95_ms=ops.get("llm_latency_p95_ms"),
        llm_skip_rate=ops.get("llm_skip_rate"),
        llm_calls=int(ops.get("llm_calls", 0) or 0),
        ops_samples=int(ops.get("samples", 0) or 0),
    )


# ---------------------------------------------------------------------------
# Comparability gates
# ---------------------------------------------------------------------------
def evaluate_confidence(
    baseline: WindowStats | None,
    after: WindowStats | None,
) -> tuple[str, list[str]]:
    """Apply the gate algorithm; returns ``(tier, reasons)``."""
    reasons: list[str] = []
    if baseline is None or after is None:
        return "insufficient", ["no_baseline_or_after"]
    tier = "high"
    if baseline.sample_size < MIN_BASELINE_EVENTS or after.sample_size < MIN_AFTER_EVENTS:
        reasons.append("insufficient_events")
        tier = "low"
    if baseline.scene_distribution and after.scene_distribution:
        jsd = jensen_shannon_distance(baseline.scene_distribution, after.scene_distribution)
        if jsd > SCENE_JSD_THRESHOLD:
            reasons.append("scene_mix_drift")
            tier = _cap_tier(tier, "medium")
    if baseline.quality_distribution and after.quality_distribution:
        same = sum(
            min(baseline.quality_distribution.get(k, 0.0), after.quality_distribution.get(k, 0.0))
            for k in set(baseline.quality_distribution) | set(after.quality_distribution)
        )
        if same < QUALITY_SIMILARITY_FLOOR:
            reasons.append("quality_drift")
            tier = _cap_tier(tier, "low")
    return tier, reasons


def _cap_tier(current: str, cap: str) -> str:
    rank = {"high": 3, "medium": 2, "low": 1, "insufficient": 0}
    return current if rank[current] <= rank[cap] else cap


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------
_DELTA_FIELDS = (
    "event_rate_per_min",
    "confidence_p50",
    "confidence_p95",
    "ttc_p50",
    "ttc_p95",
    "distance_p50_m",
    "distance_p95_m",
    # Operational metrics (from ops_sampler). Same percentage-delta logic
    # as the event-derived fields; ``compute_deltas`` skips any field
    # where either baseline or after is ``None``, so an unwired sampler
    # simply produces no ops deltas.
    "actual_fps_p50",
    "actual_fps_p95",
    "frames_dropped_ratio_p95",
    "cpu_p50",
    "cpu_p95",
    "memory_p95",
    "llm_cost_usd_per_min",
    "llm_tokens_per_min",
    "llm_latency_p95_ms",
    "llm_skip_rate",
)


def compute_deltas(baseline: WindowStats, after: WindowStats) -> dict[str, float]:
    """Percentage delta when baseline is non-zero, absolute otherwise."""
    deltas: dict[str, float] = {}
    for f in _DELTA_FIELDS:
        b = getattr(baseline, f)
        a = getattr(after, f)
        if a is None or b is None:
            continue
        if b != 0:
            deltas[f] = (a - b) / abs(b) * 100.0
        else:
            deltas[f] = float(a)
    return deltas


# ---------------------------------------------------------------------------
# ImpactMonitor
# ---------------------------------------------------------------------------
class ImpactMonitor:
    """Single active session tracker.

    The engine is intentionally simple — one active session at a time. A new
    apply within :data:`COALESCE_WINDOW_SEC` of the prior change folds into
    the same session (the original ``before`` is preserved so revert lands
    on the *first* pre-change snapshot, not the latest).
    """

    def __init__(
        self,
        events_source: Callable[[], list[dict[str, Any]]],
        *,
        ops_stats_fn: Callable[[float, float], Mapping[str, Any]] | None = None,
    ):
        """Construct the monitor.

        Args:
            events_source: Callable returning the live event ring buffer.
                Called once per window aggregation; must be cheap.
            ops_stats_fn: Optional callable ``(start_ts, end_ts) -> ops_stats_dict``
                as returned by :meth:`OpsSampler.window_stats`. When
                ``None``, windows carry only event-derived fields —
                operational metrics stay ``None`` and the UI renders "—".
        """
        self._events_source = events_source
        self._ops_stats_fn = ops_stats_fn
        self._session: dict[str, Any] | None = None

    def _ops_for(self, start_ts: float, end_ts: float) -> Mapping[str, Any] | None:
        if self._ops_stats_fn is None:
            return None
        try:
            return self._ops_stats_fn(start_ts, end_ts)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def on_settings_change(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        actor_label: str,
        changed_keys: list[str],
    ) -> str:
        """Capture (or refresh) a baseline; returns the session ``audit_id``."""
        now = time.time()
        if self._session and (now - self._session["change_ts"]) < COALESCE_WINDOW_SEC:
            self._session["after"] = after
            self._session["change_ts"] = now
            self._session["changed_keys"] = sorted(set(self._session["changed_keys"]) | set(changed_keys))
        else:
            audit_id = f"impact_{secrets.token_hex(6)}"
            baseline_window = self._capture_baseline(now)
            baseline_id = f"bl_{secrets.token_hex(6)}"
            settings_db.insert_baseline(
                baseline_id=baseline_id,
                audit_id=audit_id,
                settings_hash="",  # filled by API layer if needed
                captured_start=baseline_window.window_start_ts,
                captured_end=baseline_window.window_end_ts,
                sample_count=baseline_window.sample_size,
                payload=baseline_window.to_dict(),
            )
            self._session = {
                "audit_id": audit_id,
                "change_ts": now,
                "actor_label": actor_label,
                "before": before,
                "after": after,
                "changed_keys": changed_keys,
                "baseline_id": baseline_id,
                "baseline": baseline_window,
                "last_good": before,
                "state": "monitoring",
                "warnings": [],
            }
        self._persist_session()
        return self._session["audit_id"]

    def _capture_baseline(self, end_ts: float) -> WindowStats:
        events = self._events_source()
        for lookback in WINDOW_LOOKBACKS_SEC:
            start = end_ts - lookback
            ws = compute_window(
                events,
                start_ts=start,
                end_ts=end_ts,
                ops_stats=self._ops_for(start, end_ts),
            )
            if ws.sample_size >= MIN_BASELINE_EVENTS:
                return ws
        # Last resort: widest lookback window even if under threshold.
        start = end_ts - WINDOW_LOOKBACKS_SEC[-1]
        return compute_window(
            events,
            start_ts=start,
            end_ts=end_ts,
            ops_stats=self._ops_for(start, end_ts),
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def current_report(self) -> ImpactReport | None:
        """Return the live impact for the active session, if any."""
        if self._session is None:
            return self._restore_from_db()
        return self._build_report(self._session)

    def report_for(self, audit_id: str) -> ImpactReport | None:
        """Return a report for an arbitrary session id (active or archived)."""
        sess = settings_db.get_impact_session(audit_id)
        if sess is None:
            return None
        baseline_payload = settings_db.baseline_for_audit(audit_id)
        baseline = (
            WindowStats(**baseline_payload["payload"]) if baseline_payload else None
        )
        events = self._events_source()
        now = time.time()
        after = compute_window(
            events,
            start_ts=sess["change_ts"],
            end_ts=now,
            ops_stats=self._ops_for(sess["change_ts"], now),
        )
        return _assemble_report(sess, baseline, after)

    def _restore_from_db(self) -> ImpactReport | None:
        sess = settings_db.get_active_impact_session()
        if sess is None:
            return None
        # Hydrate the in-memory session for subsequent ticks.
        baseline_payload = settings_db.baseline_for_audit(sess["audit_id"])
        baseline = (
            WindowStats(**baseline_payload["payload"]) if baseline_payload else None
        )
        self._session = {
            **sess,
            "baseline": baseline,
            "last_good": sess["before"],
            "warnings": [],
        }
        return self._build_report(self._session)

    def _build_report(self, sess: dict[str, Any]) -> ImpactReport:
        events = self._events_source()
        now = time.time()
        # If we crossed the unattended threshold, mark the state.
        if (
            sess["state"] == "monitoring"
            and (now - sess["change_ts"]) >= (IMPACT_SESSION_MAX_AGE_SEC - UNATTENDED_BANNER_LEAD_SEC)
        ):
            sess["state"] = "monitoring_unattended"
            self._persist_session()
        if (now - sess["change_ts"]) >= IMPACT_SESSION_MAX_AGE_SEC and sess["state"] != "archived":
            sess["state"] = "archived"
            sess["archived_at"] = now
            self._persist_session()
            self._session = None
        after = compute_window(
            events,
            start_ts=sess["change_ts"],
            end_ts=now,
            ops_stats=self._ops_for(sess["change_ts"], now),
        )
        baseline = sess.get("baseline")
        report = _assemble_report(sess, baseline, after)
        sess["last_payload"] = report.to_dict()
        self._persist_session()
        return report

    def _persist_session(self) -> None:
        if self._session is None:
            return
        sess = self._session
        settings_db.upsert_impact_session(
            session_id=sess["audit_id"],
            audit_id=sess["audit_id"],
            change_ts=sess["change_ts"],
            actor_label=sess["actor_label"],
            before=sess["before"],
            after=sess["after"],
            baseline_id=sess.get("baseline_id"),
            last_payload=sess.get("last_payload"),
            state=sess["state"],
            archived_at=sess.get("archived_at"),
        )

    # ------------------------------------------------------------------
    # Operator-facing controls
    # ------------------------------------------------------------------
    def revert_target(self) -> dict[str, Any] | None:
        """Return the snapshot to revert to (or ``None`` if nothing eligible)."""
        if self._session is None:
            self._restore_from_db()
        if self._session is None:
            return None
        return dict(self._session["last_good"])

    def archive_active(self) -> None:
        """Force-archive the currently active session (e.g. after rollback)."""
        if self._session is None:
            return
        self._session["state"] = "archived"
        self._session["archived_at"] = time.time()
        self._persist_session()
        self._session = None


def _assemble_report(
    sess: dict[str, Any],
    baseline: WindowStats | None,
    after: WindowStats | None,
) -> ImpactReport:
    tier, reasons = evaluate_confidence(baseline, after)
    deltas = compute_deltas(baseline, after) if baseline and after else {}
    immediate = [
        "event_rate_per_min",
        "severity_counts",
        "confidence_p50",
        "confidence_p95",
        "ttc_p50",
        "ttc_p95",
        "scene_distribution",
        "quality_distribution",
        # Operational — populated whenever the ops_sampler is wired. The
        # comparability gate treats these like any other immediate metric
        # (it only checks sample size + scene/quality distributions).
        "actual_fps_p95",
        "cpu_p95",
        "llm_cost_usd_per_min",
        "llm_latency_p95_ms",
    ]
    lagging = ["fp_rate", "drift_precision", "feedback_coverage"]
    return ImpactReport(
        audit_id=sess["audit_id"],
        change_ts=sess["change_ts"],
        actor_label=sess["actor_label"],
        before=sess["before"],
        after=sess["after"],
        changed_keys=sess.get("changed_keys", []),
        baseline=baseline,
        after_window=after,
        deltas=deltas,
        confidence_tier=tier,
        confidence_reasons=reasons,
        immediate_metrics=immediate,
        lagging_metrics=lagging,
        state=sess["state"],
        warnings=sess.get("warnings", []),
        last_good=sess.get("last_good", sess["before"]),
    )
