"""Tests for road_safety.core.orientation_policy — per-camera-orientation gates.

Exercises the helpers (`is_reversing`, `in_blind_zone`, `blind_zone_dwell_sec`)
and the main `classify_event` dispatcher across forward / rear / side cam
policies, following the SAE J3063 taxonomy the module enforces.
"""

from collections import deque
from collections import namedtuple

import pytest

from road_safety.config import CameraCalibration
from road_safety.core.detection import Detection, TrackHistory, TrackSample
from road_safety.core.egomotion import EgoFlow
from road_safety.core.orientation_policy import (
    BSW_DWELL_SEC,
    EGO_DIRECTION_MIN_CONFIDENCE,
    PolicyDecision,
    blind_zone_dwell_sec,
    classify_event,
    in_blind_zone,
    is_reversing,
)


# ── Helpers ─────────────────────────────────────────────────────────

def _cal(orientation: str, *, focal: float = 600.0, height: float = 1.25) -> CameraCalibration:
    """Build a realistic CameraCalibration for the given orientation."""
    return CameraCalibration(
        focal_px=focal,
        height_m=height,
        horizon_frac=0.45,
        orientation=orientation,
        bumper_offset_m=0.5,
    )


def _ego(
    *,
    direction: str = "forward",
    confidence: float = 0.9,
    speed: float = 5.0,
    ex: float = 0.0,
    ey: float = 1.0,
) -> EgoFlow:
    """Build an EgoFlow snapshot with the given direction + confidence."""
    return EgoFlow(
        ex=ex,
        ey=ey,
        confidence=confidence,
        speed_proxy_mps=speed,
        direction=direction,
        direction_confidence=confidence,
    )


def _det(
    track_id: int,
    cls: str = "car",
    *,
    cx: float = 960.0,
    cy: float = 540.0,
    w: int = 80,
    h: int = 80,
    conf: float = 0.9,
) -> Detection:
    """Build a Detection whose bbox center lands on (cx, cy)."""
    half_w = w // 2
    half_h = h // 2
    return Detection(
        cls=cls,
        conf=conf,
        x1=int(cx - half_w),
        y1=int(cy - half_h),
        x2=int(cx + half_w),
        y2=int(cy + half_h),
        track_id=track_id,
    )


def _history_with_samples(
    track_id: int,
    samples: list[tuple[float, float, float, float]],
) -> TrackHistory:
    """Stuff pre-built TrackSamples into a fresh TrackHistory.

    Each tuple is ``(t, cx, cy, bottom)``. Height is fixed at 80 px since none
    of the dwell / lateral-dominance gates consult it on the side-cam path.
    """
    history = TrackHistory(maxlen=max(len(samples), 12))
    dq: deque = deque(maxlen=history._maxlen)
    for t, cx, cy, bottom in samples:
        dq.append(TrackSample(t=t, height=80, bottom=int(bottom), cx=cx, cy=cy))
    history._tracks[track_id] = dq
    return history


# ── Group A: helpers ────────────────────────────────────────────────


def test_is_reversing_requires_ego_not_none():
    assert is_reversing(None) is False


@pytest.mark.parametrize(
    "direction",
    ["forward", "stationary"],
)
def test_is_reversing_requires_reverse_direction(direction):
    ego = _ego(direction=direction, confidence=0.9)
    assert is_reversing(ego) is False


@pytest.mark.parametrize(
    "confidence, expected",
    [
        (0.1, False),  # below floor
        (0.9, True),   # well above floor
        (EGO_DIRECTION_MIN_CONFIDENCE, True),  # exactly at the floor
    ],
)
def test_is_reversing_requires_confidence_floor(confidence, expected):
    ego = _ego(direction="reverse", confidence=confidence)
    assert is_reversing(ego) is expected


def test_in_blind_zone_only_for_side_orientation():
    """Forward and rear cams never report blind-zone presence."""
    Stub = namedtuple("Stub", ["center"])
    # A center dead in the middle of a 1920×1080 frame — inside the side ROI.
    mid = Stub(center=(960.0, 540.0))
    assert in_blind_zone(mid, 1920, 1080, "forward") is False
    assert in_blind_zone(mid, 1920, 1080, "rear") is False
    assert in_blind_zone(mid, 1920, 1080, "side") is True


@pytest.mark.parametrize(
    "cx, cy, expected",
    [
        (960.0, 540.0, True),   # dead center — inside ROI
        (960.0, 50.0, False),   # top strip — above vertical ROI
        (960.0, 1070.0, False), # bottom strip — below vertical ROI
        (100.0, 540.0, False),  # far-left edge — outside horizontal band
        (1820.0, 540.0, False), # far-right edge — outside horizontal band
    ],
)
def test_in_blind_zone_geometry(cx, cy, expected):
    Stub = namedtuple("Stub", ["center"])
    det = Stub(center=(cx, cy))
    assert in_blind_zone(det, 1920, 1080, "side") is expected


def test_blind_zone_dwell_zero_without_history():
    history = TrackHistory(maxlen=12)
    assert blind_zone_dwell_sec(42, history, 1920, 1080, "side") == 0.0


def test_blind_zone_dwell_growing_streak():
    # Five samples 0.2s apart, all bottoms inside the vertical band
    # (y_lo ≈ 270, y_hi ≈ 1026 for a 1080-tall frame). Span: 0.8s.
    samples = [
        (0.0, 960.0, 500.0, 500),
        (0.2, 960.0, 520.0, 520),
        (0.4, 960.0, 540.0, 540),
        (0.6, 960.0, 560.0, 560),
        (0.8, 960.0, 580.0, 580),
    ]
    history = _history_with_samples(7, samples)
    dwell = blind_zone_dwell_sec(7, history, 1920, 1080, "side")
    assert dwell == pytest.approx(0.8, abs=1e-3)


def test_blind_zone_dwell_older_out_of_zone_breaks_streak():
    # Earliest sample out-of-band should NOT extend the dwell past the break.
    # y_lo ≈ 270 → bottom=50 is above the band (outside).
    samples = [
        (0.0, 960.0, 500.0, 50),    # out-of-band — breaks the streak
        (0.2, 960.0, 520.0, 520),
        (0.4, 960.0, 540.0, 540),
        (0.6, 960.0, 560.0, 560),
        (0.8, 960.0, 580.0, 580),
    ]
    history = _history_with_samples(7, samples)
    dwell = blind_zone_dwell_sec(7, history, 1920, 1080, "side")
    # Streak goes from t=0.2 → t=0.8, so dwell should be ~0.6s, not 0.8s.
    assert dwell == pytest.approx(0.6, abs=1e-3)


def test_blind_zone_dwell_returns_zero_when_latest_out_of_zone():
    # Latest sample above the band → no active dwell streak.
    samples = [
        (0.0, 960.0, 500.0, 500),
        (0.2, 960.0, 520.0, 520),
        (0.4, 960.0, 540.0, 540),
        (0.6, 960.0, 560.0, 560),
        (0.8, 960.0, 580.0, 50),  # latest is out-of-band
    ]
    history = _history_with_samples(7, samples)
    assert blind_zone_dwell_sec(7, history, 1920, 1080, "side") == 0.0


# ── Group B: classify_event dispatch ────────────────────────────────


@pytest.mark.parametrize(
    "event_type",
    ["pedestrian_proximity", "vehicle_close_interaction"],
)
def test_forward_always_emits_fcw(event_type):
    calibration = _cal("forward")
    primary = _det(1, cls="person", cx=500.0, cy=540.0)
    secondary = _det(2, cls="car", cx=520.0, cy=540.0)
    history = TrackHistory(maxlen=12)
    decision = classify_event(
        calibration=calibration,
        event_type=event_type,
        primary=primary,
        secondary=secondary,
        frame_w=1920,
        frame_h=1080,
        ego=_ego(direction="forward", confidence=0.9),
        track_history=history,
    )
    assert isinstance(decision, PolicyDecision)
    assert decision.emit is True
    assert decision.taxonomy == "FCW"
    assert decision.display_event_type is None


def test_rear_suppresses_when_not_reversing():
    calibration = _cal("rear", focal=260.0, height=1.10)
    primary = _det(1, cls="car", cx=960.0, cy=540.0)
    secondary = _det(2, cls="car", cx=1000.0, cy=560.0)
    history = TrackHistory(maxlen=12)
    decision = classify_event(
        calibration=calibration,
        event_type="vehicle_close_interaction",
        primary=primary,
        secondary=secondary,
        frame_w=1920,
        frame_h=1080,
        ego=_ego(direction="forward", confidence=0.9),
        track_history=history,
    )
    assert decision.emit is False
    assert decision.taxonomy == "NONE"
    assert "reversing" in decision.reason


def test_rear_emits_rcw_when_reversing_longitudinal():
    calibration = _cal("rear", focal=260.0, height=1.10)
    primary = _det(1, cls="car", cx=960.0, cy=540.0)
    # Secondary has a longitudinal-dominant motion: cy moves ~100 px over 1s,
    # cx stays put → RCW (not RCTA).
    secondary = _det(2, cls="car", cx=960.0, cy=640.0)
    samples = [
        (0.0, 960.0, 540.0, 540),
        (0.25, 960.0, 565.0, 565),
        (0.5, 960.0, 590.0, 590),
        (0.75, 960.0, 615.0, 615),
        (1.0, 960.0, 640.0, 640),
    ]
    history = _history_with_samples(2, samples)
    decision = classify_event(
        calibration=calibration,
        event_type="vehicle_close_interaction",
        primary=primary,
        secondary=secondary,
        frame_w=1920,
        frame_h=1080,
        ego=_ego(direction="reverse", confidence=0.9),
        track_history=history,
    )
    assert decision.emit is True
    assert decision.taxonomy == "RCW"
    assert decision.display_event_type == "reverse_collision_risk"


def test_rear_emits_rcta_when_reversing_lateral():
    calibration = _cal("rear", focal=260.0, height=1.10)
    primary = _det(1, cls="car", cx=960.0, cy=540.0)
    # Lateral-dominant: cx sweeps ~300 px over 1s, cy ~static.
    secondary = _det(2, cls="car", cx=1260.0, cy=540.0)
    samples = [
        (0.0, 960.0, 540.0, 540),
        (0.25, 1035.0, 542.0, 542),
        (0.5, 1110.0, 540.0, 540),
        (0.75, 1185.0, 542.0, 542),
        (1.0, 1260.0, 540.0, 540),
    ]
    history = _history_with_samples(2, samples)
    decision = classify_event(
        calibration=calibration,
        event_type="vehicle_close_interaction",
        primary=primary,
        secondary=secondary,
        frame_w=1920,
        frame_h=1080,
        ego=_ego(direction="reverse", confidence=0.9),
        track_history=history,
    )
    assert decision.emit is True
    assert decision.taxonomy == "RCTA"
    assert decision.display_event_type == "rear_cross_traffic"


def test_side_suppresses_out_of_blind_zone():
    calibration = _cal("side", focal=260.0, height=1.0)
    primary = _det(1, cls="car", cx=960.0, cy=540.0)
    # Far-left edge — outside the horizontal ROI band.
    secondary = _det(2, cls="car", cx=50.0, cy=540.0)
    history = TrackHistory(maxlen=12)
    decision = classify_event(
        calibration=calibration,
        event_type="pedestrian_proximity",
        primary=primary,
        secondary=secondary,
        frame_w=1920,
        frame_h=1080,
        ego=_ego(direction="stationary", confidence=0.0),
        track_history=history,
    )
    assert decision.emit is False
    assert decision.taxonomy == "NONE"
    assert "blind zone" in decision.reason


def test_side_suppresses_short_dwell():
    calibration = _cal("side", focal=260.0, height=1.0)
    primary = _det(1, cls="car", cx=960.0, cy=540.0)
    secondary = _det(2, cls="person", cx=960.0, cy=540.0)
    # Only one sample → dwell = 0.0 s, which is < BSW_DWELL_SEC.
    samples = [(0.0, 960.0, 540.0, 540)]
    history = _history_with_samples(2, samples)
    decision = classify_event(
        calibration=calibration,
        event_type="pedestrian_proximity",
        primary=primary,
        secondary=secondary,
        frame_w=1920,
        frame_h=1080,
        ego=_ego(direction="stationary", confidence=0.0),
        track_history=history,
    )
    assert decision.emit is False
    assert decision.taxonomy == "NONE"
    assert "dwell" in decision.reason


def test_side_emits_bsw_with_pedestrian_display():
    calibration = _cal("side", focal=260.0, height=1.0)
    primary = _det(1, cls="car", cx=960.0, cy=540.0)
    secondary = _det(2, cls="person", cx=960.0, cy=540.0)
    # Samples span 1.0 s (≫ BSW_DWELL_SEC = 0.4 s), all in-band.
    samples = [
        (0.0, 960.0, 540.0, 540),
        (0.25, 960.0, 545.0, 545),
        (0.5, 960.0, 550.0, 550),
        (0.75, 960.0, 555.0, 555),
        (1.0, 960.0, 560.0, 560),
    ]
    history = _history_with_samples(2, samples)
    decision = classify_event(
        calibration=calibration,
        event_type="pedestrian_proximity",
        primary=primary,
        secondary=secondary,
        frame_w=1920,
        frame_h=1080,
        ego=_ego(direction="stationary", confidence=0.0),
        track_history=history,
    )
    assert decision.emit is True
    assert decision.taxonomy == "BSW"
    assert decision.display_event_type == "blind_spot_pedestrian"


def test_side_emits_bsw_with_vehicle_display():
    calibration = _cal("side", focal=260.0, height=1.0)
    primary = _det(1, cls="car", cx=960.0, cy=540.0)
    secondary = _det(2, cls="car", cx=960.0, cy=540.0)
    samples = [
        (0.0, 960.0, 540.0, 540),
        (0.25, 960.0, 545.0, 545),
        (0.5, 960.0, 550.0, 550),
        (0.75, 960.0, 555.0, 555),
        (1.0, 960.0, 560.0, 560),
    ]
    history = _history_with_samples(2, samples)
    decision = classify_event(
        calibration=calibration,
        event_type="vehicle_close_interaction",
        primary=primary,
        secondary=secondary,
        frame_w=1920,
        frame_h=1080,
        ego=_ego(direction="stationary", confidence=0.0),
        track_history=history,
    )
    assert decision.emit is True
    assert decision.taxonomy == "BSW"
    assert decision.display_event_type == "blind_spot_vehicle"


def test_side_suppresses_when_ego_reversing():
    calibration = _cal("side", focal=260.0, height=1.0)
    primary = _det(1, cls="car", cx=960.0, cy=540.0)
    secondary = _det(2, cls="person", cx=960.0, cy=540.0)
    history = TrackHistory(maxlen=12)
    decision = classify_event(
        calibration=calibration,
        event_type="pedestrian_proximity",
        primary=primary,
        secondary=secondary,
        frame_w=1920,
        frame_h=1080,
        ego=_ego(direction="reverse", confidence=0.9),
        track_history=history,
    )
    assert decision.emit is False
    assert decision.taxonomy == "NONE"
    assert "reversing" in decision.reason


def test_unknown_orientation_suppresses():
    calibration = _cal("diagonal")  # operator typo
    primary = _det(1, cls="person", cx=500.0, cy=540.0)
    secondary = _det(2, cls="car", cx=520.0, cy=540.0)
    history = TrackHistory(maxlen=12)
    decision = classify_event(
        calibration=calibration,
        event_type="pedestrian_proximity",
        primary=primary,
        secondary=secondary,
        frame_w=1920,
        frame_h=1080,
        ego=_ego(direction="forward", confidence=0.9),
        track_history=history,
    )
    assert decision.emit is False
    assert decision.taxonomy == "NONE"
    assert "unknown" in decision.reason
