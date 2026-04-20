"""Drift monitor + active-learning sampler for the road safety dashcam pipeline.

ROLE IN THE SYSTEM
------------------
The hot path emits events. Operators review events and mark them tp (true
positive) or fp (false positive) in the admin UI, which appends JSONL lines
to ``data/feedback.jsonl``. This module sits above that signal and turns it
into two products:

  /api/drift              -> JSON for the Dashboard "Drift" banner
  data/active_learning/   -> directory of events queued for relabeling

It is read-mostly, fault-tolerant, and never runs inside the perception
loop — all work happens behind an HTTP endpoint or a sampler callback.

Two responsibilities, intentionally colocated because they share the same
"what went wrong?" signal:

  1. ``DriftMonitor`` — joins operator feedback (``data/feedback.jsonl``) with
     emitted events (``data/events.json`` or an in-memory getter) and reports
     rolling-window precision. It also slices by ``risk_level`` and
     ``event_type`` so an operator can tell whether degradation is global or
     localized (e.g. one event_type is suddenly noisy because a new camera
     angle landed on the system).

  2. ``ActiveLearningSampler`` — pulls the *ambiguous* and *disputed* events
     off the wire and packages them for re-labeling in Label Studio / CVAT.
     Decision-boundary sampling (confidence in [0.35, 0.50]) gives the model
     the examples it is most uncertain about; disputed sampling (verdict=fp)
     captures the ones the model got *confidently wrong*. Both are the high
     information-per-label buckets.

SLIDING-WINDOW PRECISION INTUITION
----------------------------------
Precision = TP / (TP + FP). We keep the last N labeled events
(``window_size``, default 50) and compute precision over that slice. As new
feedback lands, the oldest drops off the back — a classic sliding window.
If false positives grow faster than true positives, precision drops and
crosses the alert threshold. "Trend" compares this window's precision with
the *previous* non-overlapping window: precision today vs. precision last
window. An increase suggests the model or pipeline is recovering; a drop
suggests drift (new camera angle, new weather class, model decay, etc.)
and fires a Slack/watchdog notification.

Design calls worth calling out:

  * Never raises. Monitors live in production — a missing feedback file, a
    corrupt line, or a missing events.json must degrade to zeros, not crash
    the dashboard endpoint.

  * Export tarballs copy the INTERNAL (unredacted) thumbnail, because
    labeling needs full fidelity and the export is explicitly an
    inside-the-org artifact. Public, face/plate-redacted thumbnails are
    useless for training.

  * Buckets with <3 labels report "insufficient" instead of a noisy
    precision — 1/1 is not 100% precision, it is one data point.

  * Trend compares the current window against the *prior* non-overlapping
    window of the same size, using a +/- 0.05 band around stable. Anything
    tighter is noise at typical operator-feedback volumes.

PYTHON IDIOMS USED IN THIS FILE (first-time reader notes)
---------------------------------------------------------
  * ``@dataclass`` — auto-generates ``__init__`` + ``__repr__`` from typed
    fields. See ``DriftReport`` and ``ActiveLearningSample``.
  * ``pathlib.Path`` — OS-independent path handling. ``p / "x.json"`` joins.
  * ``collections.defaultdict`` — a dict that auto-creates a default value
    on missing-key access. Used below for count aggregation.
  * JSONL reading — one JSON object per line, appended rather than
    rewritten. The helper ``_read_jsonl`` parses each line independently
    and skips corrupt lines so one bad row can't break the whole report.
  * ``try / except / finally`` — Python's exception-handling structure.
    ``try:`` runs the risky code, ``except TYPE:`` handles specific errors,
    ``finally:`` runs cleanup whether or not an exception fired.
  * ``Iterable[tuple[str, str]]`` — a generic "something you can iterate"
    that yields (bucket, verdict) pairs.
  * ``random.Random()`` — a local RNG instance (vs. module-global
    ``random.random()``) so tests can seed it without touching global state.
"""

from __future__ import annotations

import json
import random
import shutil
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Tunable thresholds — every value here has a "why", not just a "what".
# ---------------------------------------------------------------------------

# PRECISION_ALERT_THRESHOLD — if rolling precision drops below 70% the
# Drift banner goes red and a Slack alert fires. 0.70 was picked as the
# level below which operator trust collapses — past that point users start
# dismissing alerts without reading them, which poisons the feedback
# signal that drives this whole module.
PRECISION_ALERT_THRESHOLD = 0.70

# DECISION_BOUNDARY_{LOW,HIGH} — the confidence band [0.35, 0.50] around
# the emission threshold used for active-learning sampling. Events whose
# model confidence lands in this "near boundary" zone are the ones the
# classifier is most uncertain about — labeling them gives the highest
# information gain per annotation. Tighter than this band (e.g. 0.40-0.45)
# yields too few samples; wider (0.25-0.60) dilutes with confident cases.
DECISION_BOUNDARY_LOW = 0.35
DECISION_BOUNDARY_HIGH = 0.50

# DECISION_BOUNDARY_SAMPLE_PROB — probability of actually writing a
# near-boundary event to the pending queue. Set to 0.5 to halve sampling
# volume (avoid drowning human labelers). Acts as a simple rate limiter
# without bookkeeping.
DECISION_BOUNDARY_SAMPLE_PROB = 0.5

# MIN_BUCKET_LABELS — bucket precision below this sample count reports
# "insufficient" instead of a number. 3 is the smallest count where TP/FP
# variance starts being informative; 1/1 = "100% precision" is meaningless
# noise that would mislead the operator.
MIN_BUCKET_LABELS = 3

# TREND_DELTA — the neutral band around the previous window's precision.
# A delta within +/- 0.05 is called "stable"; larger is "improving" or
# "degrading". Any tighter and normal operator-feedback variance (a handful
# of labels per window) would flip the trend every refresh.
TREND_DELTA = 0.05


# ===========================================================================
# SECTION 1 — DriftMonitor
# ===========================================================================


@dataclass
class DriftReport:
    """Serialized output of ``DriftMonitor.compute()``.

    This is the JSON the Drift banner renders on the dashboard. All fields
    are plain types so FastAPI's default encoder handles it without a
    custom ``model_dump``. The ``as_dict`` method exists to keep the exact
    schema pinned even if ``asdict()``-behavior changes.

    Attributes:
        window_size: Number of labeled events used in this computation
            (TP + FP). NOT the configured ``window_size`` — the actual
            count in the current window, which can be smaller early on.
        true_positives: Count of verdict="tp" within the window.
        false_positives: Count of verdict="fp" within the window.
        precision: TP / (TP + FP), 0.0 when no labels yet.
        by_risk_level: Per-risk-level breakdown (high/medium/low) with the
            same TP/FP/precision/status schema used by ``_bucket_stats``.
        by_event_type: Same breakdown keyed by event_type.
        window_start_ts: operator_ts of the first label in the window.
        window_end_ts: operator_ts of the last label in the window.
        alert_triggered: True when precision < threshold AND we have at
            least MIN_BUCKET_LABELS labels (no false alarms on tiny N).
        trend: "improving" | "stable" | "degrading" — see the module
            docstring for the windowing logic.
    """

    window_size: int
    true_positives: int
    false_positives: int
    precision: float
    by_risk_level: dict
    by_event_type: dict
    window_start_ts: str
    window_end_ts: str
    alert_triggered: bool
    trend: str  # "improving" | "stable" | "degrading"
    # Feedback coverage: what fraction of recent events received any operator
    # verdict at all. Low coverage means the precision number above is from a
    # biased sample — operators label the alerts that bothered them and ignore
    # the rest. A precision of 0.9 from 10% coverage is not the same signal as
    # 0.9 from 60% coverage. Consumers should surface both numbers, not just
    # precision.
    feedback_coverage: float = 0.0
    labeled_events: int = 0
    total_events_in_window: int = 0

    def as_dict(self) -> dict:
        """FastAPI-friendly JSON-serialisable representation.

        Returns:
            Plain-dict copy of this dataclass. Kept explicit (rather than
            using ``dataclasses.asdict``) so the schema is auditable and
            stable across refactors.
        """
        return {
            "window_size": self.window_size,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "precision": self.precision,
            "by_risk_level": self.by_risk_level,
            "by_event_type": self.by_event_type,
            "window_start_ts": self.window_start_ts,
            "window_end_ts": self.window_end_ts,
            "alert_triggered": self.alert_triggered,
            "trend": self.trend,
            "feedback_coverage": self.feedback_coverage,
            "labeled_events": self.labeled_events,
            "total_events_in_window": self.total_events_in_window,
        }


def _empty_report() -> DriftReport:
    """Safe default used when feedback is missing or computation fails.

    Returns:
        A ``DriftReport`` of all zeros / empty strings with
        ``trend="stable"`` and ``alert_triggered=False`` — the "nothing
        to see here" shape the dashboard renders when there's no data.
    """
    return DriftReport(
        window_size=0,
        true_positives=0,
        false_positives=0,
        precision=0.0,
        by_risk_level={},
        by_event_type={},
        window_start_ts="",
        window_end_ts="",
        alert_triggered=False,
        trend="stable",
    )


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSON Lines file into a list of dicts, tolerating corruption.

    JSONL (one JSON object per line) is the append-only format we use for
    feedback, audit log, and watchdog. Reading it here goes line-by-line
    rather than ``json.loads(whole_file)`` because a single malformed
    line must not lose the preceding good lines.

    Args:
        path: Target JSONL file.

    Returns:
        List of parsed dicts in file order. Empty list when the file is
        missing or unreadable.
    """
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        # ``with`` — context manager, guarantees the file handle closes
        # even if the enclosed code raises.
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # Corrupt line — skip rather than fail the whole report.
                    continue
    except OSError:
        return []
    return out


def _read_events_json(path: Path) -> list[dict]:
    """Load the on-disk events snapshot, handling both supported shapes.

    Historically ``events.json`` was a bare list of events; newer
    snapshots are wrapped as ``{"events": [...]}`` alongside metadata.
    This helper normalizes both to a plain list and filters out any
    non-dict entries defensively.

    Args:
        path: Path to ``events.json`` (or equivalent).

    Returns:
        A list of event dicts, possibly empty. Never raises.
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    # ``isinstance(x, T)`` — Python's runtime type check. Safer than
    # ``type(x) is T`` because it honors subclassing.
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        return [e for e in data["events"] if isinstance(e, dict)]
    return []


def _precision(tp: int, fp: int) -> float:
    """Compute TP / (TP + FP), rounded to 4 decimal places.

    Args:
        tp: True-positive count.
        fp: False-positive count.

    Returns:
        A value in [0.0, 1.0]. Returns 0.0 when both counts are zero
        (avoids ZeroDivisionError — consumers must interpret "0.0 with
        0 labels" as "no signal yet" via ``window_size``).
    """
    total = tp + fp
    if total == 0:
        return 0.0
    return round(tp / total, 4)


def _bucket_stats(
    labels: Iterable[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    """Compute per-bucket TP/FP/precision from (bucket_key, verdict) pairs.

    Used to slice the overall precision number by ``risk_level`` and by
    ``event_type``. The dashboard renders these slices so an operator can
    see whether a drop is global ("precision tanked everywhere") or
    localized ("precision tanked only on ``near_miss`` events").

    Args:
        labels: Iterable of (bucket_name, verdict_string) tuples. Verdict
            is one of "tp" or "fp"; other values are ignored. Empty
            bucket_name values are skipped.

    Returns:
        Dict keyed by bucket_name. Each entry carries TP/FP counts plus
        either ``{"precision": float, "status": "ok"}`` or
        ``{"precision": None, "status": "insufficient"}`` when the bucket
        has fewer than MIN_BUCKET_LABELS rows. The "insufficient" state
        suppresses noisy "1/1 = 100%" precision reads on tiny buckets.
    """
    # defaultdict(lambda: {...}) — a dict whose missing-key access
    # auto-creates the default instead of raising KeyError. Lets us write
    # ``counts[key]["tp"] += 1`` without bootstrapping each bucket first.
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0})
    for key, verdict in labels:
        if not key:
            continue
        if verdict == "tp":
            counts[key]["tp"] += 1
        elif verdict == "fp":
            counts[key]["fp"] += 1
    out: dict[str, dict[str, Any]] = {}
    for key, c in counts.items():
        total = c["tp"] + c["fp"]
        if total < MIN_BUCKET_LABELS:
            # Not enough data — explicitly say so rather than return a
            # misleading precision number.
            out[key] = {"tp": c["tp"], "fp": c["fp"], "precision": None, "status": "insufficient"}
        else:
            out[key] = {
                "tp": c["tp"],
                "fp": c["fp"],
                "precision": _precision(c["tp"], c["fp"]),
                "status": "ok",
            }
    return out


class DriftMonitor:
    """Compute rolling-window precision and trend from operator feedback.

    STATE
    -----
      * ``feedback_path`` — JSONL file of operator verdicts.
      * ``events_path`` — on-disk events snapshot (fallback when no
        in-memory source is set).
      * ``window_size`` — how many of the most recent labels to use.
      * ``alert_threshold`` — precision at or below this triggers
        ``alert_triggered=True``.
      * ``_event_source`` — optional closure returning live events from
        the server's in-memory deque (preferred over the on-disk file
        because it's fresher and cheaper).

    LIFECYCLE
    ---------
    Constructed in ``server.py`` lifespan startup; ``set_event_source`` is
    called immediately after with the live-events closure. ``compute()``
    is invoked from the ``/api/drift`` HTTP handler on every request (no
    background thread, so staleness is bounded by request frequency).

    THREAD SAFETY
    -------------
    ``compute()`` only reads files and calls the event-source closure, and
    both underlying stores are append-only or snapshot-style, so
    concurrent reads are safe.
    """

    def __init__(
        self,
        feedback_path: Path = Path("data/feedback.jsonl"),
        events_path: Path = Path("data/events.json"),
        window_size: int = 50,
        alert_threshold: float = PRECISION_ALERT_THRESHOLD,
    ):
        """Configure paths and window size.

        Args:
            feedback_path: JSONL feedback file written by the admin UI.
            events_path: Snapshot of recent events. Used only when no
                in-memory source has been registered.
            window_size: Number of most recent labels to include in the
                sliding window. Default 50 balances sensitivity (enough
                samples to estimate precision) against stickiness (too
                many and a current regression takes too long to surface).
            alert_threshold: Precision at or below which alerts trigger.
                Defaults to the module-level ``PRECISION_ALERT_THRESHOLD``.
        """
        # ``Path(...)`` wrapping normalizes str or Path input into Path.
        self.feedback_path = Path(feedback_path)
        self.events_path = Path(events_path)
        self.window_size = int(window_size)
        self.alert_threshold = float(alert_threshold)
        # Start with no live source; caller registers one via
        # set_event_source(). Annotated as Callable | None so the type
        # checker is happy when we check for None later.
        self._event_source: Callable[[], list[dict]] | None = None

    def set_event_source(self, events_getter: Callable[[], list[dict]]) -> None:
        """Register a callable returning recent in-memory events.

        Preferred over re-reading events.json on every compute() call — the
        server keeps a deque of recent events that is cheaper and fresher
        than the on-disk copy.

        Args:
            events_getter: Zero-arg closure returning the current live
                events list. Called once per ``compute()``; should be
                cheap. Server-side this is a trivial read of a
                ``collections.deque``.
        """
        self._event_source = events_getter

    # -- internal ----------------------------------------------------------

    def _load_events_index(self) -> dict[str, dict]:
        """Merge in-memory + on-disk events into a single {event_id: event} map.

        In-memory takes precedence — it's the fresher copy.

        Returns:
            ``{event_id: event_dict}`` — the index consumers use to look
            up event attributes (risk_level, event_type) while joining
            with feedback verdicts.
        """
        index: dict[str, dict] = {}
        # Load on-disk first so in-memory overwrites with its fresher copy.
        for evt in _read_events_json(self.events_path):
            eid = evt.get("event_id")
            if eid:
                index[eid] = evt
        if self._event_source is not None:
            try:
                live = self._event_source() or []
            except Exception:
                # Closure broke — fall back to whatever we loaded from
                # disk. Never let a bad live-source break the endpoint.
                live = []
            for evt in live:
                if isinstance(evt, dict):
                    eid = evt.get("event_id")
                    if eid:
                        index[eid] = evt
        return index

    def _window(self, feedback: list[dict], offset: int = 0) -> list[dict]:
        """Return ``window_size`` labels ending ``offset`` positions from the end.

        Slicing an append-ordered list this way gives us the sliding
        window. ``offset=0`` returns the current window; ``offset=window_size``
        returns the previous non-overlapping window used for trend comparison.

        Args:
            feedback: Full feedback list, oldest first.
            offset: How many entries from the tail to step back before
                taking the window.

        Returns:
            Slice of up to ``window_size`` entries. Empty list when the
            requested window is past the start of the feedback log.
        """
        if not feedback:
            return []
        end = len(feedback) - offset
        # ``max(0, ...)`` clamps start to zero — otherwise negative slices
        # would wrap around and include data from the wrong end.
        start = max(0, end - self.window_size)
        if end <= 0 or start >= end:
            return []
        return feedback[start:end]

    def _precision_of(self, feedback_window: list[dict]) -> float:
        """Compute precision over a pre-sliced feedback window.

        Args:
            feedback_window: List of feedback dicts with ``verdict`` keys.

        Returns:
            TP / (TP + FP), or 0.0 on an empty window.
        """
        # ``sum(1 for f in ... if cond)`` — a generator expression
        # counting matches without building an intermediate list.
        tp = sum(1 for f in feedback_window if f.get("verdict") == "tp")
        fp = sum(1 for f in feedback_window if f.get("verdict") == "fp")
        return _precision(tp, fp)

    def _trend(self, current_precision: float, feedback: list[dict]) -> str:
        """Classify movement between the current and previous windows.

        Logic: take the window immediately preceding the current one,
        compute its precision, and compare. Delta > +TREND_DELTA means
        we've improved by at least 5 percentage points; delta < -TREND_DELTA
        means we've degraded by at least 5; anything in between is noise.

        Args:
            current_precision: Precision of the current window (already
                computed in ``compute()``).
            feedback: Full feedback list — we slice the prior window from it.

        Returns:
            One of "improving", "stable", "degrading". Returns "stable"
            when the prior window has too few labels to trust.
        """
        prior = self._window(feedback, offset=self.window_size)
        if len(prior) < MIN_BUCKET_LABELS:
            return "stable"
        prior_precision = self._precision_of(prior)
        delta = current_precision - prior_precision
        if delta >= TREND_DELTA:
            return "improving"
        if delta <= -TREND_DELTA:
            return "degrading"
        return "stable"

    # -- public ------------------------------------------------------------

    def compute(self) -> DriftReport:
        """Produce the full drift report.

        Called on every ``/api/drift`` request. Always returns a valid
        ``DriftReport`` — never raises. On any error path it returns
        ``_empty_report()`` so the dashboard stays renderable.

        Returns:
            A ``DriftReport`` with window stats, per-bucket breakdown,
            trend, and feedback coverage. Empty-zero report when there
            is no feedback yet, no active window, or any I/O/parse error.
        """
        try:
            feedback = _read_jsonl(self.feedback_path)
            if not feedback:
                return _empty_report()

            # Current window: last N labeled events.
            window = self._window(feedback)
            if not window:
                return _empty_report()

            events_index = self._load_events_index()

            tp = 0
            fp = 0
            # Two parallel lists of (bucket, verdict) tuples. We build
            # them once here instead of re-iterating the window twice in
            # _bucket_stats for each slice.
            risk_labels: list[tuple[str, str]] = []
            type_labels: list[tuple[str, str]] = []

            # Walk the window, joining each feedback row with the matching
            # event's risk_level/event_type. Missing events degrade to
            # "unknown" so the feedback row still counts toward precision.
            for fb in window:
                verdict = fb.get("verdict")
                if verdict not in ("tp", "fp"):
                    continue
                if verdict == "tp":
                    tp += 1
                else:
                    fp += 1
                # ``dict.get(key, {})`` returns an empty dict on miss —
                # lets us chain another ``.get(...)`` safely below.
                evt = events_index.get(fb.get("event_id"), {})
                risk = evt.get("risk_level") or "unknown"
                etype = evt.get("event_type") or "unknown"
                risk_labels.append((risk, verdict))
                type_labels.append((etype, verdict))

            precision = _precision(tp, fp)
            by_risk = _bucket_stats(risk_labels)
            by_type = _bucket_stats(type_labels)

            # Window timestamps — first/last operator_ts we actually saw.
            timestamps = [fb.get("operator_ts") for fb in window if fb.get("operator_ts")]
            window_start = timestamps[0] if timestamps else ""
            window_end = timestamps[-1] if timestamps else ""

            # Alert fires ONLY when we have enough labels AND precision
            # is below the threshold. The MIN_BUCKET_LABELS guard stops
            # us alerting on the first fp when there are only 2 labels.
            alert = (tp + fp) >= MIN_BUCKET_LABELS and precision < self.alert_threshold
            trend = self._trend(precision, feedback)

            # Feedback coverage: compare labeled events against total events
            # in the same window. Guards against the "high precision from
            # biased sample" trap — if operators only label 5% of events,
            # the 95% they ignore could be silently drifting and precision
            # wouldn't move.
            #
            # ``labeled_in_window`` counts distinct labeled event_ids in
            # the window directly. We deliberately do NOT require the
            # event to still be present in ``events_index`` — the live
            # event source is a bounded ring buffer, so older labeled
            # events scroll out and would otherwise be silently dropped
            # from the numerator while feedback.jsonl still has the label.
            labeled_ids = {fb.get("event_id") for fb in window if fb.get("event_id")}
            labeled_in_window = len(labeled_ids)
            # ``total_in_window`` is the size of the observable event
            # universe (events currently in the index). Floored at the
            # label count so coverage is never < 1 label / 0 events.
            total_in_window = max(len(events_index), labeled_in_window)
            if total_in_window > 0:
                coverage = round(labeled_in_window / total_in_window, 4)
            else:
                coverage = 0.0

            return DriftReport(
                window_size=tp + fp,
                true_positives=tp,
                false_positives=fp,
                precision=precision,
                by_risk_level=by_risk,
                by_event_type=by_type,
                window_start_ts=window_start,
                window_end_ts=window_end,
                alert_triggered=alert,
                trend=trend,
                feedback_coverage=coverage,
                labeled_events=labeled_in_window,
                total_events_in_window=total_in_window,
            )
        except Exception:
            # Hard belt-and-braces: this runs behind a dashboard endpoint.
            # Any unexpected failure produces zeros instead of a 500.
            return _empty_report()


def drift_warning_message(report: DriftReport) -> str | None:
    """Slack-ready warning string, or None if no alert.

    Consumed by ``services/watchdog.py`` — when an alert is live, the
    watchdog creates a fingerprinted incident with this message as the
    human-readable description. Identifies the worst event_type bucket
    (lowest precision with enough labels) so the on-call engineer knows
    where to look first.

    Args:
        report: The most recent ``DriftReport``.

    Returns:
        A single-line warning string, or None when
        ``report.alert_triggered`` is False (nothing to warn about).
    """
    if not report.alert_triggered:
        return None

    # Scan the per-event-type buckets to find the worst offender. We
    # iterate manually rather than using ``min()`` so we can filter out
    # buckets with ``precision=None`` (insufficient data).
    worst_type = None
    worst_precision = None
    for etype, stats in (report.by_event_type or {}).items():
        p = stats.get("precision")
        if p is None:
            continue
        if worst_precision is None or p < worst_precision:
            worst_precision = p
            worst_type = etype

    # f-string with ``:.2f`` — format spec: "float with 2 decimal places".
    base = (
        f"Precision dropped to {report.precision:.2f} over last "
        f"{report.window_size} labels (threshold "
        f"{PRECISION_ALERT_THRESHOLD:.2f})."
    )
    if worst_type is not None and worst_precision is not None:
        base += (
            f" Event type '{worst_type}' driving the degradation "
            f"({worst_precision:.2f})."
        )
    return base


# ===========================================================================
# SECTION 2 — ActiveLearningSampler
# ===========================================================================


@dataclass
class ActiveLearningSample:
    """One pending row in the active-learning queue.

    Attributes:
        event_id: The event to re-label.
        reason: Why we sampled it: "decision_boundary" (model uncertain)
            or "disputed" (operator marked verdict=fp).
        confidence: Model confidence at emission time. Informs labelers
            how the classifier saw it.
        risk_level: Copy of the event's risk_level at emission time.
        thumbnail_internal: Path to the UNREDACTED thumbnail — labelers
            need full fidelity and this export never leaves the org.
        event_json: Snapshot of the full event dict. Includes detections,
            tracks, and any prior enrichment.
    """

    event_id: str
    reason: str  # "decision_boundary" | "disputed"
    confidence: float
    risk_level: str
    thumbnail_internal: str
    event_json: dict


class ActiveLearningSampler:
    """Collects ambiguous + disputed events for re-labeling.

    Samples are held on disk in ``out_dir/pending/<event_id>.json`` plus a
    copy of the internal thumbnail. ``export_batch()`` zips the pending dir
    into a single artifact ready for upload to Label Studio / CVAT and
    clears the pending queue.

    LIFECYCLE
    ---------
    Instantiated at server startup. Two hooks feed it:
      * ``maybe_sample`` — called once per emitted event; probabilistically
        writes near-boundary events to the pending directory.
      * ``sample_disputed`` — called from the feedback handler when an
        operator marks verdict=fp; always writes.

    Periodically, ops runs ``export_batch()`` (from an admin endpoint) to
    zip the queue, clear it, and hand the zip off to a labeling tool.
    """

    def __init__(self, out_dir: Path = Path("data/active_learning")):
        """Set up directories and the RNG.

        Args:
            out_dir: Root of the active-learning queue. ``pending/`` is
                created under it if missing. Export zips also land here.
        """
        self.out_dir = Path(out_dir)
        self.pending_dir = self.out_dir / "pending"
        self.thumbs_dir = Path("data/thumbnails")
        # Local RNG instance so tests can reseed without perturbing
        # global ``random`` state used elsewhere.
        self._rng = random.Random()
        try:
            # ``parents=True`` — create intermediate directories too.
            # ``exist_ok=True`` — don't raise if already there.
            self.pending_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Non-fatal — maybe_sample / sample_disputed will retry.
            pass

    # -- internal ----------------------------------------------------------

    def _internal_thumb_path(self, event_id: str) -> Path:
        """Return the path to the internal (unredacted) thumbnail file.

        Args:
            event_id: Event id used as the JPEG filename stem.

        Returns:
            Path to the internal thumbnail. May or may not exist on disk —
            the caller handles missing files gracefully.
        """
        return self.thumbs_dir / f"{event_id}.jpg"

    def _persist(self, sample: ActiveLearningSample) -> None:
        """Write the sample to ``pending_dir/<event_id>.json``.

        Args:
            sample: The sample record to persist. One JSON file per sample
                keeps export simple (glob + zip) and lets concurrent
                writers avoid contention on a shared file.

        Returns:
            None. Silently swallows ``OSError`` because this is an
            auxiliary channel — losing an active-learning sample is not
            worth crashing the event emission path.
        """
        try:
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "event_id": sample.event_id,
                "reason": sample.reason,
                "confidence": sample.confidence,
                "risk_level": sample.risk_level,
                "thumbnail_internal": sample.thumbnail_internal,
                "event_json": sample.event_json,
                # ISO-8601 with "Z" suffix — pedestrian canonical UTC
                # representation. ``timespec="seconds"`` drops microseconds.
                "sampled_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
            (self.pending_dir / f"{sample.event_id}.json").write_text(
                json.dumps(record, indent=2), encoding="utf-8"
            )
        except OSError:
            # Disk full / permissions — skip silently, this is a side channel.
            pass

    def _build_sample(
        self, event: dict, reason: str, note: str | None = None
    ) -> ActiveLearningSample:
        """Construct an ``ActiveLearningSample`` from an event dict.

        Args:
            event: The full event dict coming off the emission path (or
                the feedback handler, in the disputed case).
            reason: Why we're sampling — "decision_boundary" or "disputed".
            note: Optional operator note (e.g. reason for fp). Stored on
                the copied event_json under ``_feedback_note`` to keep
                labeler context.

        Returns:
            A fully populated ``ActiveLearningSample``. The caller is
            expected to persist it with ``_persist``.
        """
        event_id = event.get("event_id", "")
        # ``float(x or 0.0)`` — coerces None/missing to 0.0 before float().
        confidence = float(event.get("confidence", 0.0) or 0.0)
        risk_level = event.get("risk_level", "unknown") or "unknown"
        thumb_path = self._internal_thumb_path(event_id)
        # ``dict(event)`` — shallow copy so ``_feedback_note`` injection
        # doesn't mutate the caller's event.
        event_copy = dict(event)
        if note:
            event_copy["_feedback_note"] = note
        return ActiveLearningSample(
            event_id=event_id,
            reason=reason,
            confidence=confidence,
            risk_level=risk_level,
            thumbnail_internal=str(thumb_path),
            event_json=event_copy,
        )

    # -- public ------------------------------------------------------------

    def maybe_sample(self, event: dict) -> ActiveLearningSample | None:
        """Decision-boundary sampling at event emission time.

        Called once per emitted event. Filters to the confidence band
        [DECISION_BOUNDARY_LOW, DECISION_BOUNDARY_HIGH] — events the
        model was uncertain about — and then probabilistically keeps
        half of them (``DECISION_BOUNDARY_SAMPLE_PROB``) to avoid flooding
        the human labelers.

        Args:
            event: The emitted event dict.

        Returns:
            The created ``ActiveLearningSample`` (also persisted to disk)
            or None when the event is outside the boundary band or the
            random coin flip rejects it.
        """
        try:
            conf = float(event.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            # Non-numeric confidence — treat as "not samplable".
            return None
        # Outside the near-boundary band: either clearly high or clearly
        # low confidence. Those cases give less per-label information
        # than events the model is actively uncertain about.
        if not (DECISION_BOUNDARY_LOW <= conf <= DECISION_BOUNDARY_HIGH):
            return None
        # Coin flip — keep roughly half of qualifying events.
        if self._rng.random() >= DECISION_BOUNDARY_SAMPLE_PROB:
            return None
        sample = self._build_sample(event, reason="decision_boundary")
        self._persist(sample)
        return sample

    def sample_disputed(
        self, event: dict, note: str | None = None
    ) -> ActiveLearningSample:
        """Always-sample path, called when an operator marks verdict=fp.

        Disputed events are the highest-signal examples for the model —
        cases where the pipeline produced a confident event that the human
        rejected. We never filter them out; every disputed verdict goes
        straight to the pending queue.

        Args:
            event: The event the operator just marked false-positive.
            note: Optional free-text reason from the operator. Persisted
                into the event JSON under ``_feedback_note``.

        Returns:
            The created ``ActiveLearningSample``, already persisted.
        """
        sample = self._build_sample(event, reason="disputed", note=note)
        self._persist(sample)
        return sample

    def export_batch(self) -> Path | None:
        """Zip the pending directory for handoff to a labeling tool.

        The zip contains:
          * ``manifest.json`` — summary of every sample in the batch.
          * ``thumbnails/<event_id>.jpg`` — one per sample (internal, UNREDACTED
            because labelers need full fidelity).

        The pending directory is cleared on successful export so the next
        batch starts fresh.

        Returns:
            Path to the created zip, or None if there is nothing to
            export (empty pending directory) or anything failed. Never
            raises — the caller should surface None as "no export".
        """
        try:
            if not self.pending_dir.exists():
                return None
            # ``sorted(...glob(...))`` — deterministic ordering so the
            # manifest is reproducible across runs.
            records = sorted(self.pending_dir.glob("*.json"))
            if not records:
                return None

            # Timestamp format: e.g. "20260418T143022Z" — sorts
            # lexicographically as well as chronologically.
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_path = self.out_dir / f"active_learning_{ts}.zip"
            out_path.parent.mkdir(parents=True, exist_ok=True)

            manifest: list[dict] = []
            # ``tempfile.TemporaryDirectory()`` — creates an auto-cleaned
            # staging dir. The ``with`` block ensures it's removed even on
            # error paths. We stage thumbnails here so the zip is built
            # atomically and partial failures don't leave garbage in place.
            with tempfile.TemporaryDirectory() as staging:
                staging_path = Path(staging)
                for rec_path in records:
                    try:
                        rec = json.loads(rec_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        # Skip corrupt record — do NOT abort the export.
                        continue
                    event_id = rec.get("event_id", rec_path.stem)
                    thumb_src = Path(rec.get("thumbnail_internal", ""))
                    thumb_arcname = f"thumbnails/{event_id}.jpg"
                    thumb_ok = False
                    if thumb_src.exists() and thumb_src.is_file():
                        try:
                            # copy2 preserves file metadata (mtime, perms).
                            shutil.copy2(thumb_src, staging_path / f"{event_id}.jpg")
                            thumb_ok = True
                        except OSError:
                            thumb_ok = False
                    manifest.append({
                        "event_id": event_id,
                        "reason": rec.get("reason"),
                        "confidence": rec.get("confidence"),
                        "risk_level": rec.get("risk_level"),
                        "sampled_at": rec.get("sampled_at"),
                        "thumbnail": thumb_arcname if thumb_ok else None,
                        "event_json": rec.get("event_json", {}),
                    })

                manifest_path = staging_path / "manifest.json"
                manifest_path.write_text(
                    json.dumps({"count": len(manifest), "items": manifest}, indent=2),
                    encoding="utf-8",
                )

                # zipfile.ZIP_DEFLATED — standard gzip-like compression.
                # Manifest first so labelers can stream-read it without
                # unpacking thumbnails.
                with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(manifest_path, "manifest.json")
                    for item in manifest:
                        if item["thumbnail"] is None:
                            continue
                        src = staging_path / f"{item['event_id']}.jpg"
                        if src.exists():
                            zf.write(src, item["thumbnail"])

            # Clear pending on success. Per-file unlink with a broad catch
            # so one stuck file (e.g. Windows file lock) doesn't prevent
            # the rest from clearing.
            for rec_path in records:
                try:
                    rec_path.unlink()
                except OSError:
                    pass
            return out_path
        except Exception:
            # Final safety net: export failure never propagates to the
            # HTTP layer as a 500. Caller treats None as "nothing to show".
            return None
