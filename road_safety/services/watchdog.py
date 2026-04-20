"""AI-powered background watchdog — the operational incident queue.

Responsibility
--------------
This module is the single home for the fleet-safety system's "incident
queue". It is NOT a logger, it is NOT a log-tail of red text, and it is
deliberately NOT a stream of every error that ever happened. Instead it
groups repeated operational symptoms into fingerprinted *incidents*, each
of which carries enough context for an on-call engineer to act:

- ``severity``          error | warning | info
- ``category``          perception | drift | llm | stream | scene | system
- ``impact``            what this means for fleet operations
- ``likely_cause``      the most probable root cause (observed or inferred)
- ``owner``             the team that normally debugs this class of issue
- ``evidence``          small structured facts (label/value/threshold/status)
- ``investigation_steps`` ordered human-readable troubleshooting steps
- ``debug_commands``    ready-to-paste shell / curl commands
- ``runbook``           one-liner linking the finding to standing guidance
- ``priority_score``    unified sort key across severity + evidence density
- ``fingerprint``       dedupe key so repeats do NOT create new rows
- ``source``            ``rule`` (deterministic) or ``ai`` (hypothesis)
- ``cause_confidence``  ``observed`` for rule-based, ``inferred`` for AI

Runtime model
-------------
A background :class:`Watchdog` instance runs a periodic loop (default 60s)
that calls a caller-supplied ``collect_fn()`` to harvest a health snapshot
from every subsystem (perception, drift, LLM, stream, scene). That snapshot
is fed through two stacked analyzers:

1. Rule-based detectors (deterministic, always available, no LLM required).
2. An AI hypothesis layer that routes the same snapshot through Claude when
   the LLM stack is configured. Its findings are always labeled
   ``cause_confidence="inferred"`` and are skipped when their fingerprint
   or title already matches a rule-based finding (rules win).

Findings are appended to ``data/watchdog.jsonl`` (append-only JSONL, same
pattern as the audit log). The file plus an in-process grouping step is the
source of truth for the public read APIs.

Consumers
---------
The frontend ``MonitoringPage`` is the primary consumer. It reads through
these endpoints in ``server.py``:

- ``/api/watchdog``              summary stats + top incidents
- ``/api/watchdog/recent``       most recent raw findings
- ``/api/watchdog/findings*``    list / delete endpoints used by the UI

Design invariant
----------------
Monitoring must never depend on LLM availability. If the provider is down,
rule-based findings still fire, still dedupe, still persist, and still
render. The AI layer is strictly additive.
"""

# A `from __future__ import` statement enables forward-compatible Python
# features. ``annotations`` makes all type hints lazy (evaluated as strings),
# which lets us reference class names like ``WatchdogFinding`` from inside
# their own module and use the ``X | Y`` union syntax on older runtimes.
from __future__ import annotations

# ----- imports: standard library -----
import asyncio          # cooperative concurrency primitives: sleep/create_task
import json             # JSON (de)serialization for snapshots and jsonl rows
import re               # regular expressions used only by _slugify
import threading        # used for a simple process-wide lock guarding the jsonl writer
import time             # time.time() for wall-clock timestamps in ``status()``
import uuid             # uuid4 for short unique snapshot identifiers
from dataclasses import asdict, dataclass, field   # zero-boilerplate record types
from datetime import datetime, timezone            # ISO-8601 timestamp formatting
from pathlib import Path                           # typed filesystem paths
from typing import Any, Callable                   # generic-ish type hints

# ----- imports: local -----
# Paths MUST come from ``road_safety.config`` — the project's single source of
# truth for filesystem layout. Modules never compute ``Path(__file__).parent``
# on their own.
from road_safety.config import DATA_DIR

# ----- module-level constants and shared state -----

# Append-only JSON-Lines file where every finding is persisted.
# JSON Lines == one JSON object per line, easy to tail/grep and crash-safe
# (a partially written line is simply skipped by the reader).
_WATCHDOG_PATH = DATA_DIR / "watchdog.jsonl"

# Default slice size used by ``tail()`` when reading recent findings.
# 200 is chosen to be "enough to see a shift's worth of incidents" without
# loading the entire historical file into memory for every API hit.
_MAX_TAIL = 200

# A threading Lock is a mutex: only one thread can hold it at a time.
# We acquire it around any read-then-write of the jsonl file so concurrent
# writers don't interleave partial lines. Using ``threading`` (not asyncio)
# is fine because the writer runs briefly and never awaits.
_lock = threading.Lock()

# Numeric ordering for severity. Used to pick the worst severity when
# multiple observations are grouped into one incident, and to sort the
# "top incidents" list in ``stats()``. Unknown severities rank 0.
_SEVERITY_ORDER = {"error": 3, "warning": 2, "info": 1}
# Every incident needs an *owner* — the team that normally debugs that
# class of issue. These defaults are applied by ``_defaults_for()`` when a
# finding is constructed without an explicit owner. They drive the
# "Owner" chip displayed on the incident queue.
_OWNER_BY_CATEGORY = {
    "perception": "Edge camera ops",
    "drift": "ML quality",
    "llm": "AI platform",
    "stream": "Video ingest",
    "scene": "Scene understanding",
    "system": "Platform",
    "validator": "ML quality",
}

# One-liner runbook hint per category, linking the finding to standing
# operational guidance. Shown under "Runbook" on the incident card.
_RUNBOOK_BY_CATEGORY = {
    "perception": "Verify live camera quality, lens obstruction, lighting, and mount stability.",
    "drift": "Review recent false positives, validate labeling coverage, and plan threshold or model updates.",
    "llm": "Inspect provider health, rate limiting, fallback behavior, and token instrumentation.",
    "stream": "Check stream source health, frame throughput, and local resource pressure.",
    "scene": "Validate scene classifier inputs and thresholds against current roadway conditions.",
    "system": "Inspect recent deploys, runtime logs, and subsystem health endpoints.",
    "validator": "Compare primary YOLO output against the heavier shadow model, review evidence on both sides, and queue disagreements for labeling.",
}

# Fallback "impact" copy per category. ``impact`` describes the operator-
# level consequence (why anyone should care) rather than the raw symptom.
# Specific detectors may override this with narrower wording.
_DEFAULT_IMPACT_BY_CATEGORY = {
    "perception": "Perception quality is degraded, so real conflicts may be missed and noisy alerts may increase.",
    "drift": "Operators may lose trust because alert precision is degrading or becoming unmeasurable.",
    "llm": "Narration, enrichment, and investigation workflows will become slower, noisier, or unavailable.",
    "stream": "Live detection coverage is reduced because frames are not moving through the pipeline reliably.",
    "scene": "Context-aware thresholds may become unreliable, increasing false positives or missed conflicts.",
    "system": "The dashboard may report misleading health information until the underlying issue is fixed.",
    "validator": "The primary detector disagrees with the shadow model, suggesting false alerts or missed events that warrant labeling review.",
}


# ----- small utilities: slugs, ranks, scores, evidence chips -----

def _slugify(text: str) -> str:
    """Turn a free-form title into a URL-safe, fingerprint-friendly slug.

    The regex collapses any run of non-alphanumeric characters into a
    single dash, then strips leading/trailing dashes. Falls back to
    ``"finding"`` when the input reduces to the empty string so fingerprints
    always contain a usable second segment.

    Args:
        text: An arbitrary human-readable string (title or detail).

    Returns:
        A lowercase dash-separated slug, never empty.
    """
    # ``(text or "")`` guards against ``None`` input by substituting "".
    text = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return text or "finding"


def _severity_rank(severity: str) -> int:
    """Return a numeric rank for a severity string (higher == more severe).

    Args:
        severity: Severity label, case-insensitive. Expected values are
            ``"error"``, ``"warning"``, ``"info"``; anything else maps to 0.

    Returns:
        An integer in the range 0..3. Used for max/sort operations.
    """
    return _SEVERITY_ORDER.get((severity or "").lower(), 0)


def _priority_score(severity: str, source: str, evidence_count: int) -> int:
    """Compute the cross-incident sort key used by the UI.

    The score is a simple additive blend of three ingredients so the
    incident queue surfaces high-severity, well-evidenced, rule-based
    items first:

    - Base weight driven by severity (error=90, warning=60, info=30).
    - A small +5 bonus when the finding is rule-based, because rule
      findings are deterministic and therefore more trustworthy than
      AI hypotheses.
    - +2 per piece of evidence, capped at 5 chips (so we don't reward
      pathologically long evidence lists).

    Args:
        severity: Severity label (see :func:`_severity_rank`).
        source: ``"rule"`` or ``"ai"``.
        evidence_count: Number of evidence chips attached to the finding.

    Returns:
        A non-negative integer. Higher == higher priority.
    """
    base = {"error": 90, "warning": 60, "info": 30}.get((severity or "").lower(), 10)
    if source == "rule":
        base += 5
    # ``min(max(x, 0), 5)`` clamps the count into the inclusive range [0, 5].
    return base + min(max(evidence_count, 0), 5) * 2


def _evidence(label: str, value: Any, *, threshold: str | None = None, status: str = "observed") -> dict[str, str]:
    """Build a single evidence chip dict for attachment to a finding.

    Evidence chips are small structured facts that let the incident card
    convey *why* we believe something is wrong (e.g. "Drop rate = 35%,
    threshold <= 10%, status breach"). The UI renders them as compact
    pills next to the finding title.

    The ``*`` in the signature marks everything after it as keyword-only,
    so callers must write ``_evidence("x", 1, threshold=">0")`` rather
    than passing threshold positionally. This keeps call sites readable.

    Args:
        label: Short human-readable field name (e.g. "Actual FPS").
        value: The observed value. Coerced to ``str`` for JSON storage.
        threshold: Optional expected range string ("<= 10%", ">= 0.75").
        status: ``"observed"`` (the default, suppressed from output),
            ``"breach"`` for threshold violations, ``"trend"`` for change
            signals, or ``"context"`` for supporting facts.

    Returns:
        A dict with keys ``label``, ``value`` and optionally
        ``threshold`` / ``status``.
    """
    item = {"label": label, "value": str(value)}
    if threshold:
        item["threshold"] = threshold
    # "observed" is the default and is elided to keep the JSON small.
    if status != "observed":
        item["status"] = status
    return item


def _top_bucket(buckets: dict[str, Any], *, prefer_low_precision: bool = False) -> tuple[str, dict[str, Any]] | None:
    """Pick the most interesting bucket from a drift breakdown.

    Drift reports carry per-event-type or per-risk-level sub-buckets like
    ``{"tailgate": {"precision": 0.42, ...}, ...}``. For findings we want
    to point at either the best or worst slice to make the finding
    actionable ("Worst event type is `tailgate` at 0.42 precision").

    Args:
        buckets: Mapping from bucket key to stats dict. ``None`` and
            non-dict values are tolerated.
        prefer_low_precision: When True, flip the sign on ``precision``
            so the "worst" slice wins. When False, the highest-precision
            slice wins.

    Returns:
        ``(key, stats)`` for the winning bucket, or ``None`` when the
        input had no usable entries. The tuple return uses Python's
        ``X | None`` union syntax (i.e. Optional[tuple[...]]).
    """
    best_key = ""
    best_stats: dict[str, Any] | None = None
    best_score: float | None = None
    # ``buckets or {}`` guards against ``None`` so ``.items()`` never throws.
    for key, stats in (buckets or {}).items():
        if not isinstance(stats, dict):
            continue
        precision = stats.get("precision")
        # Only numeric precisions feed the score; missing values keep the
        # bucket in the running but cannot beat a scored competitor.
        score = precision if isinstance(precision, (int, float)) else None
        if prefer_low_precision and score is not None:
            score = -score
        if best_stats is None:
            best_key = key
            best_stats = stats
            best_score = score
            continue
        if score is None:
            continue
        if best_score is None or score > best_score:
            best_key = key
            best_stats = stats
            best_score = score
    if best_stats is None:
        return None
    return best_key, best_stats


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a timezone-aware datetime.

    Our persisted rows use a trailing ``Z`` to denote UTC; Python's
    ``datetime.fromisoformat`` only accepts the ``+00:00`` form, so we
    rewrite ``Z`` before parsing.

    Args:
        ts: ISO-8601 timestamp (``"2026-04-18T12:34:56.000Z"``) or None.

    Returns:
        A ``datetime`` when parsing succeeds, otherwise ``None``. We
        deliberately swallow ``ValueError`` so one malformed row cannot
        break aggregation for the whole jsonl file.
    """
    if not ts:
        return None
    # try/except lets us attempt a risky operation and recover without
    # crashing. Here we only catch ``ValueError`` — the narrow exception
    # that ``fromisoformat`` raises on bad input — per the project's
    # "prefer narrow except clauses" rule.
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ----- fingerprinting -----
#
# A *fingerprint* is the dedupe key that turns "the same thing happened
# again" into one incident row with an incremented count, instead of N
# unrelated rows in the queue. The rules below map the well-known
# titles produced by the rule-based detectors to a stable short string.
# Unknown titles fall back to ``{category}/{slugified-title}`` so even
# ad-hoc findings stay groupable across repeats.

def _fingerprint_for(category: str, title: str) -> str:
    """Compute the stable fingerprint (dedupe key) for a finding.

    The fingerprint is what makes the watchdog an *incident queue*
    rather than a log tail: two findings with the same fingerprint are
    grouped into one incident row with ``count`` incremented, instead of
    rendering as two separate incidents.

    Args:
        category: Finding category (``perception``/``drift``/...).
        title: Human-readable title of the finding.

    Returns:
        A short slash-separated string such as ``"drift/precision-alert"``.
        Stable across process restarts because it depends only on
        category + title text.
    """
    cat = (category or "system").lower()
    ttl = (title or "").lower()
    # Each branch below maps a well-known detector title to the canonical
    # fingerprint for its incident class. Order matters only to the extent
    # that earlier matches win when titles overlap.
    if cat == "llm" and "error rate" in ttl:
        return "llm/error-rate"
    if cat == "llm" and "latency" in ttl:
        return "llm/latency"
    if cat == "llm" and "zero tokens" in ttl:
        return "llm/token-instrumentation"
    if cat == "drift" and "feedback coverage" in ttl:
        return "drift/feedback-coverage"
    if cat == "drift" and "alert" in ttl:
        return "drift/precision-alert"
    if cat == "drift" and ("trending down" in ttl or "degrading" in ttl):
        return "drift/precision-degrading"
    if cat == "drift" and ("window start equals end" in ttl or "zero-duration" in ttl):
        return "drift/window-collapsed"
    if cat == "drift" and "false positive" in ttl:
        return "drift/false-positive-spike"
    if cat == "drift" and "unknown" in ttl:
        return "drift/taxonomy-unknown"
    if cat == "stream" and "frame drop rate" in ttl:
        return "stream/frame-drop"
    if cat == "stream" and "fps" in ttl:
        return "stream/fps-low"
    if cat == "stream" and ("not running" in ttl or "stalled" in ttl):
        return "stream/stalled"
    if cat == "perception" and "failed" in ttl:
        return "perception/failed"
    if cat == "perception" and "degraded" in ttl:
        return "perception/degraded"
    # ``f"..."`` is an f-string: a templated string literal where ``{expr}``
    # is replaced with the value of ``expr``. Fallback fingerprint uses the
    # category and a slugified title so unknown-titled findings still dedupe.
    return f"{cat}/{_slugify(title)}"


# ----- defaults table: copy-paste ready troubleshooting per category -----

def _defaults_for(category: str, title: str, severity: str) -> dict[str, Any]:
    """Derive default fields (cause, impact, steps, commands) for a finding.

    Every finding needs owner/runbook/impact/cause/steps/commands. Rather
    than forcing each detector to repeat boilerplate, this helper looks up
    the right defaults based on category and title keywords, then returns
    a dict that :func:`_normalize_finding_payload` and :func:`_make_finding`
    merge into the final record.

    Args:
        category: Finding category (``perception``/``drift``/...).
        title: Human-readable title of the finding.
        severity: Severity label; used only for the computed
            ``priority_score``.

    Returns:
        A dict with ``owner``, ``runbook``, ``impact``, ``likely_cause``,
        ``investigation_steps``, ``debug_commands``, ``fingerprint`` and
        ``priority_score`` keys, all guaranteed present and non-empty.
    """
    cat = (category or "system").lower()
    ttl = (title or "").lower()
    # Fall back to the "system" defaults when the category is unknown —
    # findings should always render, even if the classification is off.
    owner = _OWNER_BY_CATEGORY.get(cat, "Platform")
    runbook = _RUNBOOK_BY_CATEGORY.get(cat, _RUNBOOK_BY_CATEGORY["system"])
    impact = _DEFAULT_IMPACT_BY_CATEGORY.get(cat, _DEFAULT_IMPACT_BY_CATEGORY["system"])
    likely_cause = ""
    # Type hints like ``list[str]`` (Python 3.9+) declare the element type so
    # tooling can catch mistakes; they have no runtime effect.
    steps: list[str] = []
    commands: list[str] = []

    if cat == "perception":
        likely_cause = "Camera image quality has dropped below the confidence floor, often due to low light, blur, vibration, or obstruction."
        steps = [
            "Open `/api/live/perception` and confirm whether luminance, sharpness, or confidence is driving the degraded state.",
            "Compare the live feed with the physical camera install: lens cleanliness, mount vibration, and scene lighting.",
            "If the issue persists after the environment stabilizes, recalibrate perception thresholds for this camera position.",
        ]
        commands = [
            "curl http://localhost:8000/api/live/perception",
            "tail -n 120 logs/app.log | rg \"perception|quality|camera\"",
        ]
    elif cat == "drift" and "feedback coverage" in ttl:
        likely_cause = "The feedback pipeline or operator labeling loop is not producing enough verdicts to measure precision credibly."
        impact = "Drift monitoring is effectively blind, so false positives can grow without a trustworthy signal."
        steps = [
            "Open `/api/drift` and confirm `feedback_coverage`, `labeled_events`, and `total_events_in_window`.",
            "Check whether operators are receiving and submitting verdicts for recent events.",
            "Until coverage improves, treat precision numbers as weak evidence and prioritize restoring the labeling path.",
        ]
        commands = [
            "curl http://localhost:8000/api/drift",
            "tail -n 40 data/feedback.jsonl",
        ]
    elif cat == "drift" and ("alert" in ttl or "trending down" in ttl):
        likely_cause = "Recent event behavior has shifted or current thresholds are overfiring, and the feedback window now shows a material precision drop."
        steps = [
            "Inspect `/api/drift` and identify the worst-performing event type or risk slice.",
            "Review the most recent false positives from `/api/live/events` to see whether this is a threshold issue or a taxonomy problem.",
            "Queue disputed and boundary samples for relabeling before changing model thresholds.",
        ]
        commands = [
            "curl http://localhost:8000/api/drift",
            "curl http://localhost:8000/api/live/events?limit=20",
        ]
    elif cat == "drift" and ("window start equals end" in ttl or "zero-duration" in ttl):
        likely_cause = "The drift window timestamps are not advancing, which points to a bug in timestamp assignment or window initialization."
        impact = "Trend and precision calculations may be computed on a degenerate window, making the monitor misleading."
        steps = [
            "Inspect the latest drift payload and verify the start/end timestamps are advancing between checks.",
            "Trace the feedback records being used to compute the current window.",
            "Patch timestamp assignment before trusting drift trend output.",
        ]
        commands = [
            "curl http://localhost:8000/api/drift",
            "tail -n 20 data/feedback.jsonl",
        ]
    elif cat == "drift" and ("false positive" in ttl or "unknown" in ttl):
        likely_cause = "The detector is emitting events that the current taxonomy cannot classify cleanly, or the threshold is too loose for this scene."
        steps = [
            "Check recent events for `event_type=unknown` or `risk_level=unknown`.",
            "Validate the classifier path before tuning thresholds so you do not hide a labeling bug.",
            "Review disputed events and capture them for retraining.",
        ]
        commands = [
            "curl http://localhost:8000/api/live/events?limit=20",
            "curl http://localhost:8000/api/drift",
        ]
    elif cat == "llm" and "error rate" in ttl:
        likely_cause = "The LLM path is failing due to provider errors, rate limiting, auth drift, or unhealthy fallback behavior."
        steps = [
            "Open `/api/llm/stats` to confirm which call types are failing and whether errors cluster around one provider or mode.",
            "Inspect `/api/llm/recent` for the latest error strings and skip reasons.",
            "If errors are rate-limit related, reduce load or force the cheaper/faster fallback path until the provider recovers.",
        ]
        commands = [
            "curl -H \"Authorization: Bearer $ROAD_ADMIN_TOKEN\" http://localhost:8000/api/llm/stats",
            "curl -H \"Authorization: Bearer $ROAD_ADMIN_TOKEN\" http://localhost:8000/api/llm/recent",
            "tail -n 160 logs/app.log | rg \"429|anthropic|openai|llm\"",
        ]
    elif cat == "llm" and "latency" in ttl:
        likely_cause = "The provider is slow or prompt payloads have grown enough to violate the real-time budget."
        impact = "Operator-facing narration and investigation lag behind live events, which weakens real-time coaching value."
        steps = [
            "Inspect `/api/llm/stats` and compare overall latency with the slowest call type.",
            "Check whether latency jumped after a deploy or during provider backpressure.",
            "Reduce prompt size or route slow paths to a faster model while keeping detection fully local.",
        ]
        commands = [
            "curl -H \"Authorization: Bearer $ROAD_ADMIN_TOKEN\" http://localhost:8000/api/llm/stats",
            "tail -n 160 logs/app.log | rg \"429|Retrying request|HTTP Request\"",
        ]
    elif cat == "llm" and "zero tokens" in ttl:
        likely_cause = "Instrumentation for a call type is broken or responses are being dropped before token accounting runs."
        impact = "Cost and usage metrics become misleading, so tuning and incident response rely on bad data."
        steps = [
            "Inspect `/api/llm/stats` and verify the affected call type reports latency but zero tokens.",
            "Check recent LLM records to confirm whether outputs are empty or only the accounting path is broken.",
            "Fix instrumentation before using cost or throughput data for policy decisions.",
        ]
        commands = [
            "curl -H \"Authorization: Bearer $ROAD_ADMIN_TOKEN\" http://localhost:8000/api/llm/stats",
            "curl -H \"Authorization: Bearer $ROAD_ADMIN_TOKEN\" http://localhost:8000/api/llm/recent",
        ]
    elif cat == "stream":
        likely_cause = "Frames are not flowing through the reader or processing loop at the expected rate, often because the source is stalled or the host is overloaded."
        steps = [
            "Check `/api/admin/health` for `frames_read`, `frames_processed`, and `target_fps`.",
            "Confirm the stream source is still reachable and that the reader thread is alive.",
            "If the source is healthy, inspect CPU and memory pressure before reducing FPS or model load.",
        ]
        commands = [
            "curl http://localhost:8000/api/admin/health",
            "tail -n 160 logs/app.log | rg \"stream|reader|fps|buffer\"",
        ]
    else:
        likely_cause = "A subsystem health signal crossed its expected operating range and needs investigation."
        steps = [
            "Review the latest evidence attached to this finding.",
            "Open the related health endpoint and compare it with the previous healthy state.",
            "Use the runtime logs to determine whether this is a regression, resource issue, or bad input.",
        ]
        commands = ["tail -n 160 logs/app.log"]

    return {
        "owner": owner,
        "runbook": runbook,
        "impact": impact,
        "likely_cause": likely_cause,
        "investigation_steps": steps,
        "debug_commands": commands,
        "fingerprint": _fingerprint_for(cat, title),
        "priority_score": _priority_score(severity, "rule", evidence_count=0),
    }


def _normalize_finding_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw finding dict into the canonical shape used by the UI.

    Used in two places: when a fresh finding is persisted, and when an
    existing jsonl row is read back (so historical rows with missing
    fields still render correctly).

    Args:
        payload: A possibly-incomplete finding dict, as produced by this
            module, loaded from jsonl, or returned by the AI layer.

    Returns:
        A fully-populated dict with every expected key present. Missing
        fields are filled from category/title defaults; ``priority_score``
        is recomputed when not a valid integer; ``cause_confidence`` is
        inferred from ``source`` when absent.
    """
    severity = (payload.get("severity") or "info").lower()
    category = payload.get("category") or "system"
    title = payload.get("title") or "Watchdog finding"
    detail = payload.get("detail") or ""
    defaults = _defaults_for(category, title, severity)

    # This is a list comprehension: a compact ``[expr for x in iterable if cond]``
    # form that builds a list in one expression. The inner ``{k: str(v) ...}``
    # is a dict comprehension. Together they:
    #   - skip anything that isn't a dict with both label and value,
    #   - stringify every value so JSON serialization is safe.
    evidence = [
        {k: str(v) for k, v in item.items()}
        for item in (payload.get("evidence") or [])
        if isinstance(item, dict) and item.get("label") and item.get("value") is not None
    ]
    source = payload.get("source") or "rule"
    priority = payload.get("priority_score")
    # Re-derive priority if missing or non-integer so stored rows stay
    # comparable to freshly emitted ones.
    if not isinstance(priority, int):
        priority = _priority_score(severity, source, len(evidence))

    # The ``payload.get("x") or defaults["x"]`` pattern is "use x if truthy,
    # otherwise fall back to the default". It handles missing keys AND
    # empty strings, which is exactly what we want for human-readable copy.
    return {
        "severity": severity,
        "category": category,
        "title": title,
        "detail": detail,
        "suggestion": payload.get("suggestion") or "",
        "impact": payload.get("impact") or defaults["impact"],
        "likely_cause": payload.get("likely_cause") or defaults["likely_cause"],
        "owner": payload.get("owner") or defaults["owner"],
        "runbook": payload.get("runbook") or defaults["runbook"],
        "fingerprint": payload.get("fingerprint") or defaults["fingerprint"],
        "source": source,
        # AI-sourced findings MUST be labeled as inferred hypotheses so the
        # UI never presents them with the authority of a rule match.
        "cause_confidence": payload.get("cause_confidence") or ("inferred" if source == "ai" else "observed"),
        "priority_score": priority,
        "evidence": evidence,
        "investigation_steps": payload.get("investigation_steps") or defaults["investigation_steps"],
        "debug_commands": payload.get("debug_commands") or defaults["debug_commands"],
        # Timestamps are ISO-8601 UTC with millisecond precision and a
        # trailing ``Z`` for consistency with audit.jsonl and the frontend.
        "ts": payload.get("ts") or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        # ``snapshot_id`` groups every finding from the same watchdog tick,
        # so operators can reason about "what else fired at this moment".
        "snapshot_id": payload.get("snapshot_id") or uuid.uuid4().hex[:12],
    }


def _normalize_finding(finding: WatchdogFinding) -> WatchdogFinding:
    """Round-trip a :class:`WatchdogFinding` through the payload normalizer.

    Guarantees that a finding constructed manually by a detector carries
    the same shape as one reloaded from disk. Used just before persistence.

    Args:
        finding: A WatchdogFinding instance.

    Returns:
        A new WatchdogFinding populated with normalized/defaulted fields.
    """
    payload = _normalize_finding_payload(finding.as_dict())
    # ``**payload`` unpacks the dict into keyword arguments — each key in
    # the dict becomes a named argument to ``WatchdogFinding(...)``.
    return WatchdogFinding(**payload)


# ----- aggregation / grouping -----

def _group_findings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse raw findings into one row per fingerprint (the incident view).

    This is what turns N repeat observations of the same symptom into a
    single incident with ``count = N``. Each incident exposes:

    - ``fingerprint``, ``severity`` (max across observations),
      ``category``/``title``/``owner`` (from the most recent observation),
    - ``count``, ``first_seen_ts``, ``last_seen_ts``,
    - ``latest``: the full most-recent finding payload for drill-down.

    Args:
        records: Raw finding dicts (already normalized or not).

    Returns:
        A list of incident dicts, one per unique fingerprint. Order is
        dict-insertion order (i.e. first appearance in ``records``); the
        caller is expected to re-sort by priority/recency as needed.
    """
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        item = _normalize_finding_payload(record)
        key = item["fingerprint"]
        group = groups.get(key)
        ts = _parse_ts(item["ts"])
        if group is None:
            # First observation of this fingerprint — seed a new incident.
            # ``_latest_dt`` is kept only for comparison while grouping;
            # callers that care about it can read ``last_seen_ts`` instead.
            groups[key] = {
                "fingerprint": key,
                "severity": item["severity"],
                "category": item["category"],
                "title": item["title"],
                "owner": item["owner"],
                "count": 1,
                "first_seen_ts": item["ts"],
                "last_seen_ts": item["ts"],
                "latest": item,
                "_latest_dt": ts,
            }
            continue
        # Repeat observation: keep the single incident row and fold in
        # the new data. This is the core "incident queue" behavior.
        group["count"] += 1
        # Severity escalates monotonically: if a new observation is worse,
        # promote the incident; never demote on softer observations.
        if _severity_rank(item["severity"]) > _severity_rank(group["severity"]):
            group["severity"] = item["severity"]
        first_seen_dt = _parse_ts(group["first_seen_ts"])
        # Out-of-order ingest is possible when operators replay files, so
        # update first_seen_ts if we discover an earlier timestamp.
        if ts and (first_seen_dt is None or ts < first_seen_dt):
            group["first_seen_ts"] = item["ts"]
        latest_dt = group.get("_latest_dt")
        # When this observation is newer than what we had, promote it to
        # "latest" so the card reflects the most recent context.
        if ts and (latest_dt is None or ts > latest_dt):
            group["last_seen_ts"] = item["ts"]
            group["category"] = item["category"]
            group["title"] = item["title"]
            group["owner"] = item["owner"]
            group["latest"] = item
            group["_latest_dt"] = ts
    return list(groups.values())


# ----- dataclass: WatchdogFinding -----

# ``@dataclass`` is a decorator that auto-generates ``__init__``,
# ``__repr__``, and ``__eq__`` from the annotated class attributes.
# Decorators are functions that wrap a class or function to add behavior;
# written as ``@name`` on the line above the definition. Dataclasses keep
# this record type concise while giving us a single place to describe the
# shape of a finding.
@dataclass
class WatchdogFinding:
    """Canonical in-memory representation of one watchdog observation.

    A WatchdogFinding is the data shape that flows out of rule-based and
    AI detectors, through :func:`_normalize_finding`, into the jsonl
    writer, and (after grouping) onto the operator's incident queue.

    Required fields (``severity``, ``category``, ``title``, ``detail``) are
    declared without defaults so the type system enforces them. All other
    fields either carry sensible empty-string / empty-list defaults or are
    populated by :func:`default_factory` callables (timestamps, IDs) so
    constructing a finding with just the required fields still yields a
    valid, persistable object.

    Instances are produced by detectors and the AI layer, persisted by
    :func:`_write_finding`, and reconstructed from disk by ``tail()`` via
    :func:`_normalize_finding_payload`.
    """

    severity: str          # "error" | "warning" | "info"
    category: str          # "perception" | "drift" | "llm" | "stream" | "scene" | "system"
    title: str
    detail: str
    suggestion: str = ""
    impact: str = ""
    likely_cause: str = ""
    owner: str = ""
    runbook: str = ""
    fingerprint: str = ""
    source: str = "rule"   # "rule" | "ai"
    cause_confidence: str = "observed"  # "observed" | "inferred"
    priority_score: int = 0
    # ``field(default_factory=list)`` is required for mutable defaults —
    # using a bare ``= []`` would share one list across every instance,
    # which is a classic Python bug. The factory runs fresh for each new
    # object.
    evidence: list[dict[str, str]] = field(default_factory=list)
    investigation_steps: list[str] = field(default_factory=list)
    debug_commands: list[str] = field(default_factory=list)
    # ``lambda`` is an inline anonymous function; here it generates a
    # fresh UTC ISO-8601 timestamp for every new finding.
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    snapshot_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def as_dict(self) -> dict:
        """Return the finding as a plain dict suitable for JSON serialization.

        Uses :func:`dataclasses.asdict` which recurses into nested
        dataclasses and copies lists/dicts, so the returned dict can be
        mutated by the caller without affecting the source instance.
        """
        return asdict(self)


# ----- persistence: append-only JSON Lines writer + reader -----

def _write_finding(finding: WatchdogFinding) -> None:
    """Append a single normalized finding to ``data/watchdog.jsonl``.

    Each finding is one line. Errors writing to disk are silenced —
    monitoring must never take down the main process because it cannot
    write its own output.

    Args:
        finding: The WatchdogFinding to persist.

    Returns:
        None. Side effects: creates DATA_DIR if missing, appends one line
        to ``_WATCHDOG_PATH``.
    """
    try:
        finding = _normalize_finding(finding)
        # ``parents=True`` makes any missing parent directories, too;
        # ``exist_ok=True`` turns "already exists" into a no-op.
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # ``with`` is a context manager: it runs setup (acquire the lock)
        # and guaranteed teardown (release the lock) even if the body
        # raises. Nested ``with`` blocks release in reverse order.
        with _lock:
            with _WATCHDOG_PATH.open("a", encoding="utf-8") as f:
                # ``ensure_ascii=False`` preserves UTF-8 in detail strings;
                # ``default=str`` handles the rare non-JSON-native value
                # (e.g. a Path) by stringifying it instead of raising.
                f.write(json.dumps(finding.as_dict(), ensure_ascii=False, default=str) + "\n")
    except OSError:
        # Disk full / permission denied / missing volume — log-free silence
        # is fine here; the next tick will try again. Narrow ``except`` so
        # we don't mask programming bugs unrelated to I/O.
        pass


def tail(n: int = _MAX_TAIL) -> list[dict]:
    """Return the most recent N watchdog findings as normalized dicts.

    Args:
        n: Maximum number of trailing findings to return. Defaults to
            ``_MAX_TAIL`` (200).

    Returns:
        A list of normalized finding dicts in file order (oldest first).
        Missing file, unreadable file, and malformed lines all return an
        empty-or-partial list rather than raising, so callers can rely on
        this function under failure.
    """
    if not _WATCHDOG_PATH.exists():
        return []
    try:
        lines = _WATCHDOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    # ``lines[-n:]`` takes the last n items via slice notation. When the
    # file has fewer than n lines, this still works — you just get all
    # the lines.
    for raw in lines[-n:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(_normalize_finding_payload(json.loads(raw)))
        except json.JSONDecodeError:
            # Skip corrupt rows silently so one bad line cannot break
            # the entire incident queue render.
            continue
    return out


def delete_findings(indices: list[int] | None = None) -> int:
    """Delete findings by zero-based line index, or all if indices is None.

    Backs the "dismiss" and "clear all" actions on the monitoring UI.
    Treats the file as a list of lines: reads, filters, rewrites.

    Args:
        indices: The zero-based positions to remove. Pass ``None`` to
            wipe every finding from the file.

    Returns:
        The number of deleted findings. Zero if the file is missing or
        unreadable.
    """
    if not _WATCHDOG_PATH.exists():
        return 0
    with _lock:
        try:
            lines = _WATCHDOG_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        if indices is None:
            count = len(lines)
            _WATCHDOG_PATH.write_text("", encoding="utf-8")
            return count
        # ``set(...)`` gives O(1) membership checks, which matters when
        # ``indices`` is large relative to ``lines``.
        to_remove = set(indices)
        kept = [line for i, line in enumerate(lines) if i not in to_remove]
        removed = len(lines) - len(kept)
        # Rewrite the whole file from the filtered set. The trailing
        # newline is important so the next append starts on a fresh line.
        _WATCHDOG_PATH.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        return removed


def delete_findings_by_id(snapshot_ids: list[str]) -> int:
    """Delete findings matching any of the given ``snapshot_id + ts`` keys.

    The UI identifies a finding by the composite key
    ``{snapshot_id}_{ts}`` rather than by line index (indices are unstable
    under concurrent writes). This function matches that composite key.

    Args:
        snapshot_ids: A list of ``"{snapshot_id}_{ts}"`` strings.

    Returns:
        The number of deleted findings. Zero if the file is missing,
        unreadable, or contained no matches.
    """
    if not _WATCHDOG_PATH.exists():
        return 0
    ids_set = set(snapshot_ids)
    with _lock:
        try:
            lines = _WATCHDOG_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        kept: list[str] = []
        removed = 0
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                # Recompose the same composite key the UI used when it
                # asked for deletion.
                key = f"{obj.get('snapshot_id', '')}_{obj.get('ts', '')}"
                if key in ids_set:
                    removed += 1
                    continue
            except json.JSONDecodeError:
                # Corrupt row — keep it so a manual editor can recover it.
                pass
            kept.append(line)
        _WATCHDOG_PATH.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        return removed


# ----- public API: summary stats -----

def stats() -> dict:
    """Aggregate the incident queue into the payload used by /api/watchdog.

    Reads the last 500 findings, groups them into incidents, and returns
    severity/category breakdowns plus the top-5 incidents by severity,
    then count, then recency.

    Returns:
        A dict with keys:
          - ``total_findings``: raw finding rows considered.
          - ``unique_incidents``: number of distinct fingerprints.
          - ``repeating_incidents``: incidents with ``count > 1``.
          - ``by_severity``: ``{"error": n, "warning": n, "info": n}``.
          - ``by_category``: ``{category: n, ...}``.
          - ``top_incidents``: up to 5 incident dicts, already sorted.
    """
    records = tail(500)
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    incidents = _group_findings(records)
    for incident in incidents:
        sev = incident.get("severity", "unknown")
        # ``d[k] = d.get(k, 0) + 1`` is the canonical "increment a counter
        # dict" pattern; no import from collections needed.
        by_severity[sev] = by_severity.get(sev, 0) + 1
        cat = incident.get("category", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1
    # Negative severity rank and negative count sort descending (worst
    # first). ``last_seen_ts`` breaks ties with the most recent winning —
    # the lexicographic ISO-8601 order matches chronological order.
    top_incidents = sorted(
        incidents,
        key=lambda i: (
            -_severity_rank(str(i.get("severity", "info"))),
            -int(i.get("count", 0)),
            str(i.get("last_seen_ts", "")),
        ),
    )[:5]
    return {
        "total_findings": len(records),
        "unique_incidents": len(incidents),
        # ``sum(1 for i in ... if cond)`` is the idiomatic "count matches"
        # form using a generator expression (no list is materialized).
        "repeating_incidents": sum(1 for i in incidents if int(i.get("count", 0)) > 1),
        "by_severity": by_severity,
        "by_category": by_category,
        "top_incidents": top_incidents,
    }


# ----- rule-based detectors (always available, no LLM needed) -----
#
# These are deterministic checks that run on every tick. They produce
# ``source="rule"`` findings whose ``cause_confidence`` is ``"observed"``.
# Rule findings always beat AI hypotheses in dedupe (``Watchdog.check_once``).

def _make_finding(
    *,
    severity: str,
    category: str,
    title: str,
    detail: str,
    suggestion: str,
    snapshot_id: str,
    impact: str = "",
    likely_cause: str = "",
    fingerprint: str | None = None,
    evidence: list[dict[str, str]] | None = None,
    investigation_steps: list[str] | None = None,
    debug_commands: list[str] | None = None,
    source: str = "rule",
    cause_confidence: str | None = None,
) -> WatchdogFinding:
    """Construct a WatchdogFinding with category defaults already applied.

    The leading bare ``*`` forces every argument to be keyword-only at the
    call site, which keeps the already long call lists below readable and
    self-documenting.

    Args:
        severity: ``"error"`` / ``"warning"`` / ``"info"``.
        category: The subsystem this incident belongs to.
        title: Short symptom title; also the fingerprint input.
        detail: One-paragraph description visible on the incident card.
        suggestion: Operator-facing action to take next.
        snapshot_id: The current tick's snapshot id; reused so every
            finding from the same tick shares one id.
        impact, likely_cause: Optional overrides of the category defaults.
        fingerprint: Optional override; when None, derived from category
            + title via :func:`_fingerprint_for`.
        evidence, investigation_steps, debug_commands: Optional overrides
            of the category defaults.
        source: ``"rule"`` (default) or ``"ai"``.
        cause_confidence: Optional override; defaults to ``"inferred"``
            for AI sources and ``"observed"`` for rule sources.

    Returns:
        A populated WatchdogFinding ready to be written.
    """
    defaults = _defaults_for(category, title, severity)
    evidence_items = evidence or []
    return WatchdogFinding(
        severity=severity,
        category=category,
        title=title,
        detail=detail,
        suggestion=suggestion,
        impact=impact or defaults["impact"],
        likely_cause=likely_cause or defaults["likely_cause"],
        owner=defaults["owner"],
        runbook=defaults["runbook"],
        fingerprint=fingerprint or defaults["fingerprint"],
        source=source,
        cause_confidence=cause_confidence or ("inferred" if source == "ai" else "observed"),
        priority_score=_priority_score(severity, source, len(evidence_items)),
        evidence=evidence_items,
        investigation_steps=investigation_steps or defaults["investigation_steps"],
        debug_commands=debug_commands or defaults["debug_commands"],
        snapshot_id=snapshot_id,
    )


def _rule_checks(snapshot: dict, prev_snapshot: dict | None) -> list[WatchdogFinding]:
    """Run the full rule battery on a snapshot, returning any findings.

    This is the deterministic core of the watchdog. It evaluates four
    groups of detectors (perception, drift, LLM, stream) against the
    current snapshot and — for rate-of-change detectors — the previous
    snapshot.

    Args:
        snapshot: The most recent health snapshot produced by the
            caller-supplied ``collect_fn`` in :class:`Watchdog`.
        prev_snapshot: The snapshot from the previous tick, or ``None``
            on the very first run. Several detectors skip themselves when
            this is absent because deltas are undefined.

    Returns:
        A list of WatchdogFinding objects. Empty when everything is
        healthy. All findings share a single ``snap_id`` so grouping by
        ``snapshot_id`` tells you what co-fired in one tick.
    """
    findings: list[WatchdogFinding] = []
    # A single id for every finding produced by this tick — lets callers
    # correlate co-firing symptoms in one snapshot_id.
    snap_id = uuid.uuid4().hex[:12]

    # Unpack the subsystem sections defensively. Every ``.get(k, {})``
    # guards against a partial snapshot (e.g. when a subsystem has not
    # reported yet) so the detectors below never index into ``None``.
    perc = snapshot.get("perception", {})
    drift = snapshot.get("drift", {})
    llm = snapshot.get("llm", {})
    pipeline = snapshot.get("pipeline", {})
    server = snapshot.get("server", {})
    taxonomy = snapshot.get("taxonomy", {})
    # ``_interval_sec`` is injected by ``Watchdog.check_once`` so rate
    # detectors below know how much time elapsed between snapshots.
    interval_sec = float(snapshot.get("_interval_sec", 60) or 60)

    frames_read = int(pipeline.get("frames_read", 0) or 0)
    frames_processed = int(pipeline.get("frames_processed", 0) or 0)
    target_fps = float(server.get("target_fps", 0) or 0)
    # Inline conditional expression: ``a if cond else b``. Reads previous
    # frames_processed only when we actually have a previous snapshot;
    # falls back to zero otherwise so deltas are well-defined.
    prev_frames_processed = int(prev_snapshot.get("pipeline", {}).get("frames_processed", 0) or 0) if prev_snapshot else 0
    processed_delta = frames_processed - prev_frames_processed

    # The reader is recreated whenever an operator stops/starts a slot
    # (server.py _start_slot/_stop_slot). The new StreamReader resets its
    # frame counters to zero while the process-level event buffer keeps
    # accumulating, so per-tick deltas can go strongly negative across a
    # restart. Detect that here so the throughput detectors below can skip
    # this tick instead of firing a false "stalled" alert.
    #
    # Two signals: uptime regressed (process restarted) OR processed
    # counter regressed without a process restart (slot reader recycled).
    prev_uptime = float(prev_snapshot.get("server", {}).get("uptime_sec", 0) or 0) if prev_snapshot else 0.0
    current_uptime = float(server.get("uptime_sec", 0) or 0)
    reader_restarted = prev_snapshot is not None and (
        current_uptime + 1.0 < prev_uptime or processed_delta < 0
    )

    # 1. Camera / perception quality
    # Detects: camera image quality degrading or failing (low confidence,
    # low luminance, blur, obstruction).
    perc_state = perc.get("state", "nominal")
    perc_reason = perc.get("reason", "unknown")
    perc_conf = float(perc.get("avg_confidence", 0) or 0)
    perc_luma = float(perc.get("luminance", 0) or 0)
    perc_sharp = float(perc.get("sharpness", 0) or 0)
    perc_evidence = [
        _evidence("Perception state", perc_state),
        _evidence("Reason", perc_reason),
        _evidence("Average confidence", f"{perc_conf:.2f}", threshold=">= 0.75", status="breach" if perc_conf < 0.75 else "context"),
        _evidence("Luminance", f"{perc_luma:.0f}"),
        _evidence("Sharpness", f"{perc_sharp:.0f}"),
    ]
    # Detects: the perception monitor flagged the camera path as
    # "degraded" — still producing detections, but below the confidence
    # floor. This is the "hedge your bets" signal.
    if perc_state == "degraded":
        findings.append(_make_finding(
            severity="warning",
            category="perception",
            title="Camera perception degraded",
            detail=(
                f"Perception is degraded because `{perc_reason}` and confidence is only {perc_conf:.2f}. "
                f"This is high enough to keep running, but low enough to trust detections less."
            ),
            suggestion="Check the camera feed now and fix lighting, blur, or obstruction before tuning thresholds.",
            impact="Detection quality is no longer trustworthy enough for clean real-time triage, so you risk both misses and noisy alerts.",
            fingerprint="perception/degraded",
            evidence=perc_evidence,
            snapshot_id=snap_id,
        ))
    # Detects: the perception monitor is in a "failed" state — the
    # camera path is effectively unusable and every downstream signal is
    # compromised. Escalates to ``error`` severity.
    elif perc_state == "failed":
        findings.append(_make_finding(
            severity="error",
            category="perception",
            title="Camera perception failed",
            detail=f"The perception monitor is in `failed` state because `{perc_reason}`.",
            suggestion="Restore the camera path first; every downstream signal depends on a usable image.",
            impact="The system is effectively blind, so conflict detection and any operator coaching based on it are compromised.",
            fingerprint="perception/failed",
            evidence=perc_evidence,
            snapshot_id=snap_id,
        ))
    # Detects: camera is still "nominal" but average detection confidence
    # has crept below 0.70 over at least 10 samples. 10 samples keeps
    # this from firing on transient first-frame noise; 0.70 is below the
    # usual 0.75 comfort floor but above the "failed" threshold.
    elif perc_conf < 0.70 and int(perc.get("samples", 0) or 0) >= 10:
        findings.append(_make_finding(
            severity="info",
            category="perception",
            title="Average confidence remains low",
            detail=f"Average confidence is {perc_conf:.2f}, below the usual comfort floor even though the perception state is still nominal.",
            suggestion="Monitor the trend and inspect recent scenes before the issue turns into alert noise.",
            fingerprint="perception/confidence-low",
            evidence=perc_evidence,
            snapshot_id=snap_id,
        ))

    # 2. Drift / feedback loop health
    # The next block of detectors all work off of the drift section:
    # recent event counts, operator verdicts, precision rollups, and
    # per-bucket breakdowns by event type.
    precision = float(drift.get("precision", 0) or 0)
    feedback_coverage = float(drift.get("feedback_coverage", 0) or 0)
    total_events = int(drift.get("total_events_in_window", 0) or 0)
    labeled_events = int(drift.get("labeled_events", 0) or 0)
    tp = int(drift.get("true_positives", 0) or 0)
    fp = int(drift.get("false_positives", 0) or 0)
    worst_type = _top_bucket(drift.get("by_event_type", {}), prefer_low_precision=True)
    worst_type_text = ""
    if worst_type:
        worst_type_name, worst_type_stats = worst_type
        worst_precision = worst_type_stats.get("precision")
        if isinstance(worst_precision, (int, float)):
            worst_type_text = f"Worst event type is `{worst_type_name}` at {worst_precision:.2f} precision."
        else:
            worst_type_text = f"Worst event type appears to be `{worst_type_name}`, but it still has insufficient labels."

    drift_evidence = [
        _evidence("Precision", f"{precision:.2f}", threshold=">= 0.70", status="breach" if precision < 0.70 else "context"),
        _evidence("Feedback coverage", f"{feedback_coverage:.0%}", threshold="> 15%", status="breach" if feedback_coverage < 0.15 else "context"),
        _evidence("Labels", labeled_events),
        _evidence("Events in window", total_events),
        _evidence("True positives", tp),
        _evidence("False positives", fp),
    ]
    if worst_type_text:
        drift_evidence.append(_evidence("Worst bucket", worst_type_text))

    # Detects: no operator verdicts landed at all across at least 5
    # recent events — the feedback pipeline is effectively silent and
    # drift calculations are untrustworthy. Escalates to ``error`` once
    # 8+ events have accumulated without any labels.
    if total_events >= 5 and feedback_coverage == 0:
        findings.append(_make_finding(
            severity="error" if total_events >= 8 else "warning",
            category="drift",
            title=f"Zero feedback coverage across {total_events} events",
            detail=(
                f"No recent events were labeled by operators, so the drift report is blind. "
                f"Precision is being inferred from {labeled_events} labeled events out of {total_events}."
            ),
            suggestion="Restore the labeling path or operator feedback loop before trusting drift numbers.",
            impact="You can no longer tell whether false positives are rising, which makes model quality regressions much harder to catch early.",
            fingerprint="drift/feedback-coverage",
            evidence=drift_evidence,
            snapshot_id=snap_id,
        ))
    # Detects: feedback coverage is below 15% over a window of 10+
    # events. There is *some* labeling, but not enough to make precision
    # numbers credible enough to justify threshold changes.
    elif total_events >= 10 and feedback_coverage < 0.15:
        findings.append(_make_finding(
            severity="warning",
            category="drift",
            title="Feedback coverage too thin for confidence",
            detail=f"Only {feedback_coverage:.0%} of the recent event window has operator labels.",
            suggestion="Increase verdict coverage before using this drift signal to justify threshold changes.",
            fingerprint="drift/feedback-coverage-thin",
            evidence=drift_evidence,
            snapshot_id=snap_id,
        ))

    # Detects: model precision is trending downward across a window of
    # at least 3 labeled events. The 3-label floor keeps this from firing
    # on statistical noise at the very start of a session.
    if drift.get("trend") == "degrading" and (tp + fp) >= 3:
        findings.append(_make_finding(
            severity="warning",
            category="drift",
            title="Model precision trending down",
            detail=f"Rolling precision fell to {precision:.2f} with {tp} TP and {fp} FP in the current window. {worst_type_text}".strip(),
            suggestion="Review the newest false positives before changing thresholds globally.",
            fingerprint="drift/precision-degrading",
            evidence=drift_evidence,
            snapshot_id=snap_id,
        ))

    # Detects: the drift subsystem has explicitly raised its own alert
    # — precision has fallen below the alert threshold. We promote that
    # signal into the incident queue as a full ML incident card.
    if drift.get("alert_triggered"):
        findings.append(_make_finding(
            severity="error",
            category="drift",
            title="Drift alert triggered",
            detail=f"Precision is {precision:.2f}, below the alert threshold for a labeled window of {tp + fp} events. {worst_type_text}".strip(),
            suggestion="Treat this as an ML incident: review false positives, capture samples, and plan a threshold or model fix.",
            fingerprint="drift/precision-alert",
            evidence=drift_evidence,
            snapshot_id=snap_id,
        ))

    # Detects: a bug in drift window bookkeeping where the start and end
    # timestamps are identical even though the window contains labels.
    # This makes trend math meaningless, so we flag it for the owner.
    # Requires > 1 label: with a single label, start == end is tautological
    # (first and last operator_ts are the same entry), not a bookkeeping bug.
    if drift.get("window_start_ts") and drift.get("window_start_ts") == drift.get("window_end_ts") and (tp + fp) > 1:
        findings.append(_make_finding(
            severity="warning",
            category="drift",
            title="Drift window collapsed to zero duration",
            detail=f"`window_start_ts` and `window_end_ts` are both `{drift.get('window_start_ts')}` even though the window contains {tp + fp} labeled events.",
            suggestion="Fix timestamp assignment before trusting trend calculations.",
            fingerprint="drift/window-collapsed",
            evidence=[
                _evidence("Window start", drift.get("window_start_ts")),
                _evidence("Window end", drift.get("window_end_ts")),
                _evidence("Window labels", tp + fp),
            ],
            snapshot_id=snap_id,
        ))

    # Detects: the labeled window contains false positives and literally
    # no true positives — every verdict is "this was noise". Usually
    # points at a taxonomy or threshold bug that is firing on benign
    # patterns.
    if fp > 0 and tp == 0 and (tp + fp) >= 1:
        findings.append(_make_finding(
            severity="warning",
            category="drift",
            title="False positives with no true positives",
            detail=f"The current feedback window contains {fp} false positives and zero true positives. {worst_type_text}".strip(),
            suggestion="Review the flagged events now; this is often the fastest way to find a taxonomy or threshold bug.",
            fingerprint="drift/false-positive-spike",
            evidence=drift_evidence,
            snapshot_id=snap_id,
        ))

    recent_event_count = int(taxonomy.get("recent_events", 0) or 0)
    unknown_event_ratio = float(taxonomy.get("unknown_event_ratio", 0) or 0)
    unknown_risk_ratio = float(taxonomy.get("unknown_risk_ratio", 0) or 0)
    # Detects: half or more of recent events are landing in
    # ``event_type=unknown`` or ``risk_level=unknown``. This usually
    # means a classifier bug rather than a detection problem, and
    # tuning thresholds on unknown-heavy data will hide the real issue.
    if recent_event_count >= 5 and max(unknown_event_ratio, unknown_risk_ratio) >= 0.5:
        findings.append(_make_finding(
            severity="warning",
            category="drift",
            title="Recent events falling into unknown taxonomy",
            detail=(
                f"{taxonomy.get('unknown_event_types', 0)} of {recent_event_count} recent events have unknown event types and "
                f"{taxonomy.get('unknown_risk_levels', 0)} have unknown risk levels."
            ),
            suggestion="Fix event typing before tuning model thresholds, or you will hide the real failure mode.",
            fingerprint="drift/taxonomy-unknown",
            evidence=[
                _evidence("Recent events", recent_event_count),
                _evidence("Unknown event type ratio", f"{unknown_event_ratio:.0%}", threshold="< 20%", status="breach"),
                _evidence("Unknown risk ratio", f"{unknown_risk_ratio:.0%}", threshold="< 20%", status="breach"),
            ],
            snapshot_id=snap_id,
        ))

    # 3. LLM reliability and instrumentation
    # The LLM layer is best-effort enrichment, never the critical path.
    # These detectors watch error rates, latency, and token accounting
    # to catch silent provider degradation.
    error_rate = float(llm.get("error_rate", 0) or 0)
    llm_calls = int(llm.get("window_calls", 0) or 0)
    top_errors = llm.get("top_errors", []) or []
    top_error = top_errors[0]["error"] if top_errors and isinstance(top_errors[0], dict) else ""
    by_type = llm.get("by_type", {}) or {}
    worst_call_type = ""
    worst_call_error_rate = -1.0
    for call_type, stats in by_type.items():
        calls = int(stats.get("calls", 0) or 0)
        if calls <= 0:
            continue
        call_error_rate = float(stats.get("errors", 0) or 0) / calls
        if call_error_rate > worst_call_error_rate:
            worst_call_type = call_type
            worst_call_error_rate = call_error_rate

    llm_evidence = [
        _evidence("Window calls", llm_calls),
        _evidence("Error rate", f"{error_rate:.1%}", threshold="<= 20%", status="breach" if error_rate > 0.2 else "context"),
        _evidence("P50 latency", f"{float(llm.get('latency_p50_ms', 0) or 0):.0f} ms"),
        _evidence("P95 latency", f"{float(llm.get('latency_p95_ms', 0) or 0):.0f} ms", threshold="< 10000 ms", status="breach" if float(llm.get("latency_p95_ms", 0) or 0) > 10000 else "context"),
    ]
    if worst_call_type:
        llm_evidence.append(_evidence("Worst call type", f"{worst_call_type} ({worst_call_error_rate:.0%} errors)"))
    if top_error:
        llm_evidence.append(_evidence("Top error", top_error))

    # Detects: more than 20% of LLM calls failed across a window of at
    # least 5 calls. 5 is the minimum sample size before a percentage is
    # worth believing; 50%+ error promotes the finding to ``error``.
    if llm_calls >= 5 and error_rate > 0.2:
        likely_cause = "The dominant failure looks like provider rate limiting." if "429" in str(top_error) else ""
        findings.append(_make_finding(
            severity="error" if error_rate >= 0.5 else "warning",
            category="llm",
            title=f"LLM error rate high ({error_rate:.0%})",
            detail=(
                f"{int(llm.get('total_errors_all_time', 0) or 0)} total LLM errors recorded; "
                f"{worst_call_type or 'overall traffic'} is the noisiest path in the current window."
            ),
            suggestion="Inspect recent LLM records and decide whether to reduce load, switch models, or rely on fallback temporarily.",
            likely_cause=likely_cause,
            fingerprint="llm/error-rate",
            evidence=llm_evidence,
            snapshot_id=snap_id,
        ))

    llm_p95 = float(llm.get("latency_p95_ms", 0) or 0)
    # Detects: the 95th-percentile LLM latency exceeds 10 seconds.
    # At that point narration lags live events and coaching loses value.
    # >12 seconds escalates the severity to ``error``.
    if llm_p95 > 10000:
        slowest_type = ""
        slowest_p95 = 0.0
        for call_type, stats in by_type.items():
            p95 = float(stats.get("latency_p95_ms", 0) or 0)
            if p95 > slowest_p95:
                slowest_p95 = p95
                slowest_type = call_type
        findings.append(_make_finding(
            severity="error" if llm_p95 > 12000 else "warning",
            category="llm",
            title=f"LLM latency very high ({llm_p95:.0f}ms p95)",
            detail=f"P95 latency is {llm_p95:.0f}ms and the slowest call type is `{slowest_type or 'unknown'}` at {slowest_p95:.0f}ms.",
            suggestion="Keep safety-critical logic local and shift slow LLM work to a faster model or smaller prompt.",
            fingerprint="llm/latency",
            evidence=llm_evidence,
            snapshot_id=snap_id,
        ))

    prev_llm_p50 = float(prev_snapshot.get("llm", {}).get("latency_p50_ms", 0) or 0) if prev_snapshot else 0.0
    # Detects: a sudden 1.5x jump in median LLM latency between two
    # ticks (not just slow overall — specifically a regression). The
    # >=500ms floor on the previous value filters out noise at very low
    # latencies where small absolute swings look like big ratios.
    if prev_snapshot and prev_llm_p50 >= 500 and float(llm.get("latency_p50_ms", 0) or 0) > prev_llm_p50 * 1.5 and llm_calls >= 5:
        current_p50 = float(llm.get("latency_p50_ms", 0) or 0)
        findings.append(_make_finding(
            severity="warning",
            category="llm",
            title="LLM median latency jumped sharply",
            detail=f"Median latency rose from {prev_llm_p50:.0f}ms to {current_p50:.0f}ms in one interval.",
            suggestion="Check whether load, prompt size, or provider congestion changed before the next interval compounds it.",
            fingerprint="llm/latency-jump",
            evidence=[
                _evidence("Previous p50", f"{prev_llm_p50:.0f} ms"),
                _evidence("Current p50", f"{current_p50:.0f} ms", status="trend"),
            ],
            snapshot_id=snap_id,
        ))

    # Detects (per call type): instrumentation is broken — the call type
    # recorded latency (so calls landed) but zero tokens in either
    # direction (so accounting dropped the response). Firing per call
    # type means each broken path gets its own deduped incident row.
    for call_type, stats in by_type.items():
        calls = int(stats.get("calls", 0) or 0)
        input_tokens = int(stats.get("input_tokens", 0) or 0)
        output_tokens = int(stats.get("output_tokens", 0) or 0)
        latency_p50 = float(stats.get("latency_p50_ms", 0) or 0)
        if calls >= 3 and latency_p50 > 0 and input_tokens == 0 and output_tokens == 0:
            findings.append(_make_finding(
                severity="warning",
                category="llm",
                title=f"{call_type.capitalize()} calls reporting zero tokens",
                detail=f"{calls} `{call_type}` calls recorded latency but zero input/output tokens, which points to broken instrumentation or dropped responses.",
                suggestion="Inspect the raw LLM records for this call type before trusting cost or throughput numbers.",
                fingerprint=f"llm/token-instrumentation/{call_type}",
                evidence=[
                    _evidence("Call type", call_type),
                    _evidence("Calls", calls),
                    _evidence("P50 latency", f"{latency_p50:.0f} ms"),
                    _evidence("Input tokens", input_tokens),
                    _evidence("Output tokens", output_tokens),
                ],
                snapshot_id=snap_id,
            ))

    # 4. Stream throughput and stalling
    # These detectors watch the ingest pipeline itself: reader liveness,
    # processed-frames delta, drop rate, and effective FPS vs target.
    # Detects: the stream reader thread is not alive at all — no frames
    # are being pulled from the source, so nothing downstream will fire.
    if not server.get("running", True):
        findings.append(_make_finding(
            severity="error",
            category="stream",
            title="Stream reader not running",
            detail="The stream reader thread is no longer alive, so the pipeline is not ingesting new frames.",
            suggestion="Verify the source URL and restart the reader after network and source health are confirmed.",
            fingerprint="stream/stopped",
            evidence=[_evidence("Reader running", False)],
            snapshot_id=snap_id,
        ))

    # Detects: the reader says it is running but zero frames were
    # processed in a tick of 10+ seconds. A silent stall looks healthy
    # by every other metric, so we catch it here. 10s is the minimum
    # interval that can confidently distinguish "stalled" from "slow".
    # Skip across reader restarts: the new reader's counters start at
    # zero, which would otherwise look identical to a stall.
    if (
        prev_snapshot
        and not reader_restarted
        and server.get("running", True)
        and processed_delta <= 0
        and interval_sec >= 10
    ):
        findings.append(_make_finding(
            severity="error",
            category="stream",
            title="Frame processing appears stalled",
            detail=f"No new frames were processed in the last {interval_sec:.0f}s while the reader still reports itself as running.",
            suggestion="Treat this like a live-ingest incident: inspect the source, buffering, and host resource pressure now.",
            fingerprint="stream/stalled",
            evidence=[
                _evidence("Frames processed delta", processed_delta),
                _evidence("Interval", f"{interval_sec:.0f}s"),
                _evidence("Reader running", server.get("running", True)),
            ],
            snapshot_id=snap_id,
        ))

    # NOTE: a "frame drop rate" detector that compared frames_processed to
    # frames_read used to live here. It fired constantly because StreamReader
    # subsamples by design — at TARGET_FPS=2 over a 30fps source it pulls
    # every frame but only forwards every 15th one to the perception loop,
    # so the "drop rate" sits at ~93% during healthy operation. The actual
    # concern (effective fps falling below target) is covered by the
    # detector below, which compares processed_delta to target_fps directly.

    # Detects: actual processed FPS is less than half of the configured
    # target FPS. The 200-frame floor ensures we only evaluate this once
    # the pipeline has been warmed up; transient first-tick underruns
    # are not operationally interesting. Skipped across reader restarts
    # because processed_delta will be negative and meaningless.
    if (
        prev_snapshot
        and not reader_restarted
        and target_fps > 0
        and interval_sec > 0
        and frames_read > 200
    ):
        actual_fps = processed_delta / interval_sec if processed_delta >= 0 else 0.0
        if actual_fps < target_fps * 0.5:
            findings.append(_make_finding(
                severity="warning",
                category="stream",
                title=f"Actual FPS ({actual_fps:.1f}) well below target",
                detail=f"The pipeline processed {processed_delta} frames in {interval_sec:.0f}s, or {actual_fps:.1f} fps versus a target of {target_fps:.1f}.",
                suggestion="Check buffering and host load before trusting real-time alerting performance.",
                fingerprint="stream/fps-low",
                evidence=[
                    _evidence("Actual FPS", f"{actual_fps:.1f}", threshold=f">= {target_fps:.1f}", status="breach"),
                    _evidence("Target FPS", f"{target_fps:.1f}"),
                ],
                snapshot_id=snap_id,
            ))

    return findings


# ----- AI hypothesis layer (Claude) -----
#
# Strictly additive. If the LLM stack is unconfigured, unreachable, or
# returns garbage, this layer produces zero findings and the rule-based
# layer carries on alone. Every finding produced here is labeled
# ``source="ai"`` and ``cause_confidence="inferred"`` so the UI can style
# hypotheses distinctly from deterministic observations.

_ANALYSIS_SYSTEM = (
    "You are an AI operations watchdog for a fleet safety system. "
    "You receive periodic health snapshots from a road-conflict detection pipeline. "
    "Analyze the data for issues, failures, anomalies, and inconsistencies. "
    "Return STRICT JSON only — an array of finding objects. Each finding has: "
    '{"severity": "error"|"warning"|"info", "category": string, "title": string, '
    '"detail": string, "suggestion": string, "likely_cause": string}. '
    "Categories: perception, drift, llm, stream, scene, system. "
    "Focus on actionable issues with operator impact. Do NOT report normal or healthy metrics as findings. "
    "Do not emit more than 3 findings. Prefer symptom-level issues over generic commentary. "
    "If everything looks healthy, return an empty array []. "
    "Be concise — title under 10 words, detail under 50 words, suggestion under 30 words. "
    "IMPORTANT: the pipeline subsamples by design. `frames_read` is the source-rate pull (native fps, typically 25–60); "
    "`frames_processed` is throttled to `target_fps` (default 2). A large read/processed gap is expected and NOT a bottleneck — "
    "only flag perception throughput issues when `frames_processed` itself falls below `target_fps`. "
    "Similarly, `llm` skips are the rate-budget back-pressure working as designed; only flag them when error_rate is high "
    "or the bucket is plainly misconfigured."
)


async def _ai_analyze(snapshot: dict, prev_snapshot: dict | None) -> list[WatchdogFinding]:
    """Ask Claude to hypothesize incidents from a snapshot; best-effort.

    ``async def`` declares a coroutine: a function that can ``await``
    other async work without blocking the event loop. It must be called
    with ``await`` or scheduled as a Task. We use it here because the
    LLM round-trip is I/O-bound and we don't want the server stalling
    on it.

    Args:
        snapshot: Current health snapshot.
        prev_snapshot: The snapshot from the previous tick, if any. Used
            only to compute per-key deltas that help the LLM reason
            about trends.

    Returns:
        Up to 3 WatchdogFinding objects on success; an empty list when
        the LLM stack is unconfigured, unreachable, returns invalid
        JSON, or any other error occurs. Findings are capped at 3 so
        the incident queue never drowns in AI speculation.
    """
    # Lazy import so this module still imports cleanly when the LLM
    # stack is entirely absent (e.g. in unit tests). All LLM calls go
    # through the wrappers in ``services/llm.py`` to inherit failover,
    # rate budget, circuit breaker, and cost tracking.
    try:
        from road_safety.services.llm import _complete, llm_configured, MODEL_CHAT
    except ImportError:
        return []

    if not llm_configured():
        return []

    user_msg = "Current health snapshot:\n" + json.dumps(snapshot, indent=2, default=str)
    if prev_snapshot:
        # Compute a terse "what changed" payload so the prompt spends
        # tokens on signal, not on repeating unchanged fields. Only
        # numeric keys get a delta; strings and dicts are skipped.
        deltas: dict[str, Any] = {}
        for key in ("pipeline", "perception", "llm"):
            curr = snapshot.get(key, {})
            prev = prev_snapshot.get(key, {})
            d = {}
            for k, v in curr.items():
                if isinstance(v, (int, float)) and k in prev:
                    diff = v - prev[k]
                    if diff != 0:
                        d[k] = {"current": v, "previous": prev[k], "delta": diff}
            if d:
                deltas[key] = d
        if deltas:
            user_msg += "\n\nChanges since last check:\n" + json.dumps(deltas, indent=2, default=str)

    try:
        # ``await`` suspends this coroutine until ``_complete`` returns
        # — the event loop is free to run other tasks in the meantime.
        raw, _inp, _out = await _complete(
            _ANALYSIS_SYSTEM, user_msg, MODEL_CHAT, max_tokens=1000,
            call_type="watchdog_analysis",
        )
        raw = raw.strip()
        # Defensive: strip markdown fencing if the model wrapped its
        # JSON in ```json blocks despite instructions.
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        items = json.loads(raw)
        if not isinstance(items, list):
            return []

        snap_id = uuid.uuid4().hex[:12]
        findings: list[WatchdogFinding] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "info")).lower()
            # We refuse to surface "info" AI findings — they tend to be
            # commentary rather than incidents and would crowd the queue.
            if severity not in {"error", "warning"}:
                continue
            findings.append(_make_finding(
                severity=severity,
                category=str(item.get("category", "system")),
                title=str(item.get("title", "AI finding")),
                detail=str(item.get("detail", "")),
                suggestion=str(item.get("suggestion", "")),
                likely_cause=str(item.get("likely_cause", "")),
                snapshot_id=snap_id,
                source="ai",
                # AI findings are hypotheses; the UI must signal that
                # the cause is inferred, not observed.
                cause_confidence="inferred",
            ))
            # Hard cap — never let the AI layer monopolize the queue.
            if len(findings) >= 3:
                break
        return findings
    except Exception:
        # Monitoring-layer rule: any LLM failure is a no-op, never a
        # crash. Rule-based findings remain the source of truth.
        return []


# ----- Watchdog service class (background loop + one-shot check) -----

class Watchdog:
    """Background service that drives the incident queue.

    The Watchdog is the long-lived orchestrator: the server instantiates
    one instance with a ``collect_fn()`` that knows how to assemble a
    health snapshot, then schedules :meth:`run_loop` on the asyncio event
    loop. The loop ticks every ``interval_sec`` seconds; each tick runs
    rule-based detectors + the AI layer + persistence + bookkeeping.

    State held on the instance (lifecycle = lifetime of the process):

    - ``_collect``            caller-supplied zero-arg callable returning
                              a fresh health snapshot dict.
    - ``_interval``           seconds between ticks. Also injected into
                              the snapshot as ``_interval_sec`` so rate
                              detectors know how much time has passed.
    - ``_prev_snapshot``      the previous tick's snapshot, used for
                              delta-based detectors. ``None`` on startup.
    - ``_last_run``           wall-clock (time.time) of the last tick.
    - ``_run_count``          total ticks completed since process start.
    - ``_total_findings``     cumulative number of findings emitted.

    Typical consumers:

    - ``server.py`` creates this and schedules it on startup.
    - The ``/api/watchdog`` endpoint calls :meth:`status` for the UI.
    """

    def __init__(
        self,
        collect_fn: Callable[[], dict],
        interval_sec: int = 60,
    ):
        """Store the health collector and the tick cadence.

        Args:
            collect_fn: A zero-arg callable returning a dict-shaped
                snapshot with ``perception``, ``drift``, ``llm``,
                ``pipeline``, ``server``, ``taxonomy`` sections. The
                caller owns whatever heavy lifting builds that dict.
            interval_sec: Seconds between ticks. Defaults to 60s.
        """
        self._collect = collect_fn
        self._interval = interval_sec
        self._prev_snapshot: dict | None = None
        self._last_run: float = 0
        self._run_count: int = 0
        self._total_findings: int = 0

    def status(self) -> dict:
        """Return the live status payload rendered at ``/api/watchdog``.

        Merges the loop's own bookkeeping (last run, run count, total
        emitted) with the aggregated queue stats from :func:`stats`. The
        ``**stats()`` spread unpacks every key of that dict into this one.
        """
        return {
            "enabled": True,
            "interval_sec": self._interval,
            "last_run": self._last_run,
            # Rounded "seconds since last tick" for the UI; ``None`` until
            # the first tick has run so the UI can show an "initializing"
            # state instead of "0.0s ago".
            "last_run_ago_sec": round(time.time() - self._last_run, 1) if self._last_run else None,
            "run_count": self._run_count,
            "total_findings_emitted": self._total_findings,
            **stats(),
        }

    async def check_once(self) -> list[WatchdogFinding]:
        """Execute one full tick: collect, analyze, deduplicate, persist.

        The orchestration is deliberately simple:

        1. Capture a fresh snapshot from the caller-supplied collector.
        2. Run rule-based detectors (always).
        3. Run the AI hypothesis layer (best-effort).
        4. Deduplicate AI findings against rule findings by fingerprint
           AND by exact title — rule findings always win.
        5. Persist every resulting finding to the jsonl file.
        6. Update bookkeeping and return the list.

        Returns:
            The list of WatchdogFinding objects produced this tick,
            already persisted to disk. Callers can ignore the return
            value — the jsonl file is the real source of truth.
        """
        snapshot = self._collect()
        # Inject the tick interval so rate-detectors don't need to know
        # about this object. This keeps ``_rule_checks`` a pure function.
        snapshot["_interval_sec"] = self._interval

        # Rule-based checks (always run)
        findings = _rule_checks(snapshot, self._prev_snapshot)

        # AI analysis (best-effort)
        ai_findings = await _ai_analyze(snapshot, self._prev_snapshot)
        # Deduplicate: prefer rule findings when both describe the same
        # incident. Fingerprint OR title match is treated as "same thing"
        # so the queue does not show a rule and an AI version side-by-side.
        rule_fingerprints = {f.fingerprint for f in findings}
        rule_titles = {f.title for f in findings}
        for af in ai_findings:
            if af.fingerprint not in rule_fingerprints and af.title not in rule_titles:
                findings.append(af)

        for f in findings:
            _write_finding(f)

        self._prev_snapshot = snapshot
        self._last_run = time.time()
        self._run_count += 1
        self._total_findings += len(findings)

        return findings

    async def run_loop(self) -> None:
        """Long-running background loop. Never returns under normal use.

        Intended to be scheduled as
        ``asyncio.create_task(wd.run_loop())`` on server startup. The
        15-second startup sleep gives the rest of the stack time to
        finish booting before the first snapshot is taken — otherwise
        the first tick would fire on half-initialized state and produce
        spurious "stream stalled" findings.

        Cancellation (``asyncio.CancelledError``) is re-raised so
        ``task.cancel()`` from the host works; every *other* exception
        is swallowed so one bad tick does not kill monitoring forever.
        """
        await asyncio.sleep(15)
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                # Honor cooperative cancellation from the host; let the
                # loop unwind cleanly.
                raise
            except Exception:
                # Any other error: log-free silence, next tick retries.
                # Monitoring must be self-healing.
                pass
            await asyncio.sleep(self._interval)
