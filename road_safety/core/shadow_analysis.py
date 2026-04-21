"""Shadow-only miss-reason diagnostic.

Given a :class:`road_safety.core.shadow_store.ShadowRecord` we know the
exact pair the *secondary* detector flagged and every box the *primary*
detector produced on the same frame. This module evaluates each member
of the secondary pair against the primary's published gates and returns
a structured explanation of *why* the primary did not emit an event.

Gates evaluated (all defined in :mod:`road_safety.core.detection`):

    * known class (``VEHICLE_CLASSES`` / ``PEDESTRIAN_CLASSES``)
    * class-specific confidence floor
        (``CONF_THRESHOLD`` / ``PERSON_CONF_THRESHOLD``)
    * class-specific minimum bbox area
        (``MIN_BBOX_AREA`` / ``PERSON_MIN_BBOX_AREA``)
    * person aspect-ratio guard (``w / h <= 0.7``)
    * primary saw the object? (best IoU against ``primary_detections``)
    * interaction-generation gate
        (``pedestrian_proximity``: edge dist ≤ 120 px,
         ``vehicle_close_interaction``: edge dist ≤ 30 px
         AND mean pair confidence ≥ ``VEHICLE_PAIR_CONF_FLOOR``)
    * depth-gate (vehicle-vehicle only,
         ``estimate_inter_distance_m <= VEHICLE_INTER_DISTANCE_GATE_M``)

What we deliberately *don't* test here:

    * convergence-angle / TTC gates — those need per-track history
      that is not preserved in the shadow record. The analysis flags
      them as "not checkable offline" when reached, so the UI can
      explain rather than silently omit them.

Never raises — each gate is wrapped so one unexpected input can't
take down the analysis endpoint.
"""

from dataclasses import asdict, dataclass
from typing import Any, Optional

from road_safety.config import CameraCalibration, camera_calibration_for
from road_safety.core.detection import (
    CONF_THRESHOLD,
    MIN_BBOX_AREA,
    PEDESTRIAN_CLASSES,
    PERSON_CONF_THRESHOLD,
    PERSON_MIN_BBOX_AREA,
    VEHICLE_CLASSES,
    VEHICLE_INTER_DISTANCE_GATE_M,
    VEHICLE_PAIR_CONF_FLOOR,
    Detection,
    bbox_edge_distance,
    estimate_inter_distance_m,
)
from road_safety.core.shadow_store import (
    ShadowRecord,
    record_primary_detections,
    record_secondary_detections,
    record_secondary_pair,
)


# IoU match threshold when asking "did the primary also see this box?"
# Same default as ``VALIDATOR_IOU_THRESHOLD`` — a lenient match so noisy
# primary detections still count as "seen".
_MATCH_IOU = 0.3

# Pedestrian proximity edge-pixel gate from ``find_interactions``.
_PED_PROX_PX = 120
# Vehicle-vehicle edge-pixel gate from ``find_interactions``.
_VEH_CLOSE_PX = 30


@dataclass
class GateVerdict:
    """One primary-gate verdict for one detection.

    Attributes:
        gate: Stable machine name (``"class"``, ``"confidence"``,
            ``"bbox_area"``, ``"aspect_ratio"``, ``"detected"``,
            ``"interaction"``, ``"depth"``).
        passed: True when the gate would let this detection through.
        actual: Human-readable reading (``"conf=0.41"``).
        threshold: Human-readable floor / ceiling
            (``"conf >= 0.50"``).
        note: Optional extra context, e.g. ``"person class — aspect
            ratio guard applies"``.
    """

    gate: str
    passed: bool
    actual: str
    threshold: str
    note: str = ""


@dataclass
class MemberAnalysis:
    """Per pair-member result. ``cls`` carries the secondary's label."""

    cls: str
    conf: float
    gates: list[GateVerdict]


@dataclass
class ShadowAnalysis:
    """Full diagnostic payload the UI renders in the dialog."""

    shadow_id: str
    event_type: str
    miss_reason: str
    members: list[MemberAnalysis]
    pair_gates: list[GateVerdict]
    calibration_used: str  # "slot" | "default"

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Gate helpers — each one evaluates exactly one gate for one detection
# ---------------------------------------------------------------------------


def _iou(a: Detection, b: Detection) -> float:
    """Intersection-over-union. Duplicated from validator to avoid the
    cross-module dependency (validator imports from here would be a
    cycle and would force loading the torch-heavy comparator just to
    run a geometry check)."""
    ix1 = max(a.x1, b.x1)
    iy1 = max(a.y1, b.y1)
    ix2 = min(a.x2, b.x2)
    iy2 = min(a.y2, b.y2)
    iw = max(ix2 - ix1, 0)
    ih = max(iy2 - iy1, 0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(a.x2 - a.x1, 0) * max(a.y2 - a.y1, 0)
    area_b = max(b.x2 - b.x1, 0) * max(b.y2 - b.y1, 0)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _best_match(det: Detection, pool: list[Detection]) -> tuple[Optional[Detection], float]:
    best: Optional[Detection] = None
    best_iou = 0.0
    for cand in pool:
        score = _iou(det, cand)
        if score > best_iou:
            best_iou = score
            best = cand
    return best, best_iou


def _class_gate(det: Detection) -> GateVerdict:
    passed = det.cls in VEHICLE_CLASSES or det.cls in PEDESTRIAN_CLASSES
    return GateVerdict(
        gate="class",
        passed=passed,
        actual=f"cls={det.cls!r}",
        threshold="cls in VEHICLE_CLASSES | PEDESTRIAN_CLASSES",
    )


def _conf_gate(det: Detection) -> GateVerdict:
    if det.cls in PEDESTRIAN_CLASSES:
        floor = PERSON_CONF_THRESHOLD
        label = "PERSON_CONF_THRESHOLD"
    else:
        floor = CONF_THRESHOLD
        label = "CONF_THRESHOLD"
    return GateVerdict(
        gate="confidence",
        passed=det.conf >= floor,
        actual=f"conf={det.conf:.2f}",
        threshold=f"conf >= {floor:.2f} ({label})",
    )


def _area_gate(det: Detection) -> GateVerdict:
    w = max(det.x2 - det.x1, 0)
    h = max(det.y2 - det.y1, 0)
    area = w * h
    if det.cls in PEDESTRIAN_CLASSES:
        floor = PERSON_MIN_BBOX_AREA
        label = "PERSON_MIN_BBOX_AREA"
    else:
        floor = MIN_BBOX_AREA
        label = "MIN_BBOX_AREA"
    return GateVerdict(
        gate="bbox_area",
        passed=area >= floor,
        actual=f"area={area} px² ({w}x{h})",
        threshold=f"area >= {floor} px² ({label})",
    )


def _aspect_gate(det: Detection) -> GateVerdict:
    if det.cls not in PEDESTRIAN_CLASSES:
        # Non-persons don't have this gate; report a benign PASS so the UI
        # doesn't render a misleading "failed" row.
        return GateVerdict(
            gate="aspect_ratio",
            passed=True,
            actual="n/a",
            threshold="w/h <= 0.70 (person only)",
            note="not a person",
        )
    h = max(det.y2 - det.y1, 1)
    w = max(det.x2 - det.x1, 0)
    ratio = w / h
    return GateVerdict(
        gate="aspect_ratio",
        passed=ratio <= 0.70,
        actual=f"w/h={ratio:.2f}",
        threshold="w/h <= 0.70 (person aspect guard)",
    )


def _seen_gate(det: Detection, primary: list[Detection]) -> GateVerdict:
    match, iou = _best_match(det, primary)
    if match is None:
        return GateVerdict(
            gate="detected",
            passed=False,
            actual="no primary detection",
            threshold=f"best IoU >= {_MATCH_IOU:.2f}",
            note="primary YOLO output was empty or dissimilar",
        )
    return GateVerdict(
        gate="detected",
        passed=iou >= _MATCH_IOU,
        actual=f"best IoU={iou:.2f} (cls={match.cls}, conf={match.conf:.2f})",
        threshold=f"best IoU >= {_MATCH_IOU:.2f}",
    )


def _interaction_gate(
    event_type: str,
    pair: tuple[Detection, Detection],
) -> GateVerdict:
    a, b = pair
    dist_px = bbox_edge_distance(a, b)
    if event_type == "pedestrian_proximity":
        return GateVerdict(
            gate="interaction",
            passed=dist_px <= _PED_PROX_PX,
            actual=f"edge dist={dist_px:.0f} px",
            threshold=f"edge dist <= {_PED_PROX_PX} px",
        )
    if event_type == "vehicle_close_interaction":
        mean_conf = (a.conf + b.conf) / 2.0
        if dist_px > _VEH_CLOSE_PX:
            return GateVerdict(
                gate="interaction",
                passed=False,
                actual=f"edge dist={dist_px:.0f} px",
                threshold=f"edge dist <= {_VEH_CLOSE_PX} px",
            )
        return GateVerdict(
            gate="interaction",
            passed=mean_conf >= VEHICLE_PAIR_CONF_FLOOR,
            actual=f"mean conf={mean_conf:.2f}",
            threshold=f"mean conf >= {VEHICLE_PAIR_CONF_FLOOR:.2f} (VEHICLE_PAIR_CONF_FLOOR)",
        )
    return GateVerdict(
        gate="interaction",
        passed=True,
        actual=f"event_type={event_type}",
        threshold="(no pixel gate for this event_type)",
    )


def _depth_gate(
    event_type: str,
    pair: tuple[Detection, Detection],
    frame_h: int,
    calibration: CameraCalibration,
) -> GateVerdict:
    if event_type != "vehicle_close_interaction":
        return GateVerdict(
            gate="depth",
            passed=True,
            actual="n/a",
            threshold=f"inter-distance <= {VEHICLE_INTER_DISTANCE_GATE_M:.1f} m",
            note="not a vehicle pair",
        )
    a, b = pair
    try:
        dist_m = estimate_inter_distance_m(a, b, frame_h, calibration=calibration)
    except Exception:  # noqa: BLE001 — never crash the analysis on edge cases
        dist_m = None
    if dist_m is None:
        return GateVerdict(
            gate="depth",
            passed=True,
            actual="inter-distance unavailable",
            threshold=f"inter-distance <= {VEHICLE_INTER_DISTANCE_GATE_M:.1f} m",
            note="monocular depth produced no estimate — primary would fall back",
        )
    return GateVerdict(
        gate="depth",
        passed=dist_m <= VEHICLE_INTER_DISTANCE_GATE_M,
        actual=f"inter-distance={dist_m:.2f} m",
        threshold=f"inter-distance <= {VEHICLE_INTER_DISTANCE_GATE_M:.1f} m",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze(record: ShadowRecord) -> ShadowAnalysis:
    """Build the per-gate diagnostic for a stored shadow record.

    Never raises. Individual gates may contribute a benign-PASS when
    their inputs are degenerate (e.g. monocular depth unavailable), so
    a healthy diagnostic will usually have at least one "failed" gate —
    that failed gate is what the UI highlights as the miss reason.
    """
    primary = record_primary_detections(record)
    pair = record_secondary_pair(record)
    calibration = camera_calibration_for(record.slot_id)

    members: list[MemberAnalysis] = []
    for det in pair:
        gates = [
            _class_gate(det),
            _conf_gate(det),
            _area_gate(det),
            _aspect_gate(det),
            _seen_gate(det, primary),
        ]
        members.append(MemberAnalysis(cls=det.cls, conf=float(det.conf), gates=gates))

    pair_gates = [
        _interaction_gate(record.event_type, pair),
        _depth_gate(record.event_type, pair, record.frame_h, calibration),
    ]

    reason = _summarise_reason(members, pair_gates)

    return ShadowAnalysis(
        shadow_id=record.shadow_id,
        event_type=record.event_type,
        miss_reason=reason,
        members=members,
        pair_gates=pair_gates,
        # Every known slot resolves to a per-camera calibration; if the
        # operator passed a slot id that doesn't match a known entry the
        # global default gets applied and we flag that here so the UI
        # can disclose the calibration caveat.
        calibration_used="slot" if record.slot_id else "default",
    )


def _summarise_reason(
    members: list[MemberAnalysis],
    pair_gates: list[GateVerdict],
) -> str:
    """Pick the most informative failure as the headline miss reason.

    Order of preference:
        1. A per-member gate failed (primary would have filtered the box).
        2. A pair-level gate failed (interaction / depth rejected the pair).
        3. Everything passed here → the miss is downstream of the gates
           we can replay offline (convergence / TTC / ego-motion /
           episode-sustained-risk).
    """
    for member in members:
        for gate in member.gates:
            if not gate.passed:
                return f"{member.cls}: {gate.gate} gate — {gate.actual} vs {gate.threshold}"
    for gate in pair_gates:
        if not gate.passed:
            return f"pair: {gate.gate} gate — {gate.actual} vs {gate.threshold}"
    return (
        "All offline-checkable primary gates pass on the secondary's pair — "
        "the miss is downstream (convergence / TTC / ego-motion / sustained-"
        "risk). Try the re-run action to inspect the primary output."
    )


def analysis_to_dict(analysis: ShadowAnalysis) -> dict[str, Any]:
    """Stable JSON shape returned by ``/api/shadow/{id}/analysis``."""
    return analysis.as_dict()
