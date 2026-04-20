"""Tests for road_safety.core — detection pipeline, context, quality."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from road_safety.core.detection import (
    Detection,
    TrackHistory,
    TrackSample,
    bbox_edge_distance,
    build_event_summary,
    classify_risk,
    estimate_distance_m,
    estimate_ttc_sec,
    find_interactions,
)


# ── bbox_edge_distance ──────────────────────────────────────────────

class TestBboxEdgeDistance:
    def test_overlapping_boxes_return_zero(self):
        a = Detection(cls="person", conf=0.9, x1=10, y1=10, x2=50, y2=50)
        b = Detection(cls="car", conf=0.9, x1=30, y1=30, x2=80, y2=80)
        assert bbox_edge_distance(a, b) == 0.0

    def test_adjacent_horizontal(self):
        a = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=10, y2=10)
        b = Detection(cls="car", conf=0.9, x1=10, y1=0, x2=20, y2=10)
        assert bbox_edge_distance(a, b) == 0.0

    def test_separated_horizontal(self):
        a = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=10, y2=10)
        b = Detection(cls="car", conf=0.9, x1=20, y1=0, x2=30, y2=10)
        assert bbox_edge_distance(a, b) == 10.0

    def test_separated_diagonal(self):
        a = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=10, y2=10)
        b = Detection(cls="car", conf=0.9, x1=13, y1=14, x2=30, y2=30)
        dist = bbox_edge_distance(a, b)
        assert dist == pytest.approx(5.0, abs=0.1)

    def test_symmetry(self):
        a = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=10, y2=10)
        b = Detection(cls="car", conf=0.9, x1=50, y1=50, x2=70, y2=70)
        assert bbox_edge_distance(a, b) == bbox_edge_distance(b, a)


# ── estimate_distance_m ─────────────────────────────────────────────

class TestEstimateDistance:
    def test_person_returns_value(self):
        det = Detection(cls="person", conf=0.9, x1=100, y1=100, x2=150, y2=300)
        result = estimate_distance_m(det, frame_h=600)
        assert result is not None
        assert 0.5 < result < 200

    def test_car_returns_value(self):
        det = Detection(cls="car", conf=0.9, x1=200, y1=350, x2=400, y2=500)
        result = estimate_distance_m(det, frame_h=600)
        assert result is not None
        assert result > 0

    def test_tiny_bbox_returns_none(self):
        det = Detection(cls="person", conf=0.9, x1=100, y1=100, x2=101, y2=101)
        result = estimate_distance_m(det, frame_h=600)
        assert result is None

    def test_unknown_class(self):
        det = Detection(cls="bicycle", conf=0.9, x1=100, y1=400, x2=200, y2=500)
        result = estimate_distance_m(det, frame_h=600)
        assert result is None or isinstance(result, float)

    def test_bumper_offset_subtracts_from_reading(self):
        """``offset_m`` shifts the published distance toward the bumper."""
        det = Detection(cls="car", conf=0.9, x1=200, y1=350, x2=400, y2=500)
        raw = estimate_distance_m(det, frame_h=600, offset_m=0.0)
        shifted = estimate_distance_m(det, frame_h=600, offset_m=1.7)
        assert raw is not None and shifted is not None
        assert shifted == pytest.approx(max(0.0, raw - 1.7), abs=0.01)

    def test_bumper_offset_clamps_at_zero(self):
        """Offset larger than the raw distance clamps to 0, never negative."""
        det = Detection(cls="car", conf=0.9, x1=200, y1=350, x2=400, y2=500)
        result = estimate_distance_m(det, frame_h=600, offset_m=999.0)
        assert result == 0.0

    def test_side_cam_skips_ground_plane(self):
        """Side cams use known-height only; result must equal known-height prior alone."""
        det = Detection(cls="car", conf=0.9, x1=200, y1=350, x2=400, y2=500)
        side = estimate_distance_m(det, frame_h=600, skip_ground_plane=True)
        # When ground-plane is skipped and the only candidate is the
        # known-height prior, the result must equal that prior exactly.
        from road_safety.core.detection import FOCAL_PX, TYPICAL_HEIGHT_M
        expected = round(FOCAL_PX * TYPICAL_HEIGHT_M["car"] / det.height, 2)
        assert side == pytest.approx(expected, abs=0.01)

    def test_per_camera_focal_changes_distance(self):
        """A 0.5x ultra-wide focal (~260 px) reports a *closer* distance than 1x (~600 px)."""
        det = Detection(cls="car", conf=0.9, x1=200, y1=350, x2=400, y2=500)
        wide_1x = estimate_distance_m(det, frame_h=600, focal_px=600.0, skip_ground_plane=True)
        ultra_05x = estimate_distance_m(det, frame_h=600, focal_px=260.0, skip_ground_plane=True)
        assert wide_1x is not None and ultra_05x is not None
        # ratio of distances ≈ ratio of focal lengths (pinhole linearity).
        assert ultra_05x < wide_1x
        assert ultra_05x == pytest.approx(wide_1x * 260.0 / 600.0, rel=0.02)


# ── estimate_ttc_sec ─────────────────────────────────────────────────

class TestEstimateTTC:
    def test_insufficient_history(self):
        assert estimate_ttc_sec([]) is None
        assert estimate_ttc_sec([TrackSample(t=0, height=100, bottom=300, cx=100, cy=200)]) is None

    def test_static_object_returns_none(self):
        samples = [
            TrackSample(t=0.0, height=100, bottom=300, cx=100, cy=200),
            TrackSample(t=0.5, height=100, bottom=300, cx=100, cy=200),
            TrackSample(t=1.0, height=100, bottom=300, cx=100, cy=200),
        ]
        assert estimate_ttc_sec(samples) is None

    def test_approaching_object(self):
        samples = [
            TrackSample(t=0.0, height=50, bottom=300, cx=100, cy=250),
            TrackSample(t=0.5, height=55, bottom=304, cx=102, cy=254),
            TrackSample(t=1.0, height=62, bottom=310, cx=105, cy=260),
            TrackSample(t=1.5, height=70, bottom=318, cx=108, cy=268),
            TrackSample(t=2.0, height=80, bottom=325, cx=112, cy=275),
        ]
        ttc = estimate_ttc_sec(samples)
        assert ttc is not None
        assert 0 < ttc < 30

    def test_receding_object_returns_none(self):
        samples = [
            TrackSample(t=0.0, height=100, bottom=300, cx=100, cy=200),
            TrackSample(t=0.4, height=95, bottom=298, cx=100, cy=198),
            TrackSample(t=0.8, height=92, bottom=296, cx=100, cy=196),
            TrackSample(t=1.2, height=88, bottom=293, cx=100, cy=193),
            TrackSample(t=1.6, height=80, bottom=290, cx=100, cy=190),
        ]
        assert estimate_ttc_sec(samples) is None


# ── classify_risk ────────────────────────────────────────────────────

class TestClassifyRisk:
    def test_high_ttc(self):
        assert classify_risk(ttc_sec=0.5, distance_m=None, fallback_px=999) == "high"

    def test_medium_ttc(self):
        assert classify_risk(ttc_sec=0.8, distance_m=None, fallback_px=999) == "medium"

    def test_low_ttc(self):
        assert classify_risk(ttc_sec=10.0, distance_m=None, fallback_px=999) == "low"

    def test_high_distance(self):
        assert classify_risk(ttc_sec=None, distance_m=2.0, fallback_px=999) == "high"

    def test_medium_distance(self):
        assert classify_risk(ttc_sec=None, distance_m=5.0, fallback_px=999) == "medium"

    def test_low_distance(self):
        assert classify_risk(ttc_sec=None, distance_m=20.0, fallback_px=999) == "low"

    def test_pixel_fallback_high(self):
        assert classify_risk(ttc_sec=None, distance_m=None, fallback_px=10) == "high"

    def test_pixel_fallback_medium(self):
        assert classify_risk(ttc_sec=None, distance_m=None, fallback_px=50) == "medium"

    def test_pixel_fallback_low(self):
        assert classify_risk(ttc_sec=None, distance_m=None, fallback_px=300) == "low"

    def test_ttc_overrides_distance(self):
        assert classify_risk(ttc_sec=0.5, distance_m=50.0, fallback_px=999) == "high"

    def test_worst_wins(self):
        assert classify_risk(ttc_sec=1.2, distance_m=2.0, fallback_px=999) == "high"


# ── find_interactions ────────────────────────────────────────────────

class TestFindInteractions:
    def test_person_near_vehicle(self, sample_detection, sample_vehicle):
        p = Detection(cls="person", conf=0.9, x1=280, y1=250, x2=310, y2=400, track_id=1)
        v = Detection(cls="car", conf=0.9, x1=300, y1=250, x2=500, y2=450, track_id=2)
        interactions = find_interactions([p, v])
        assert len(interactions) >= 1
        assert interactions[0][0] == "pedestrian_proximity"

    def test_person_far_from_vehicle(self):
        p = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=30, y2=80, track_id=1)
        v = Detection(cls="car", conf=0.9, x1=800, y1=500, x2=1000, y2=700, track_id=2)
        interactions = find_interactions([p, v])
        assert len(interactions) == 0

    def test_vehicle_pair_close(self):
        a = Detection(cls="car", conf=0.9, x1=100, y1=100, x2=200, y2=200, track_id=1)
        b = Detection(cls="car", conf=0.9, x1=220, y1=100, x2=320, y2=200, track_id=2)
        interactions = find_interactions([a, b])
        types = [i[0] for i in interactions]
        assert "vehicle_close_interaction" in types

    def test_vehicle_pair_far(self):
        a = Detection(cls="car", conf=0.9, x1=0, y1=0, x2=100, y2=100, track_id=1)
        b = Detection(cls="car", conf=0.9, x1=500, y1=500, x2=600, y2=600, track_id=2)
        interactions = find_interactions([a, b])
        assert len(interactions) == 0

    def test_empty_detections(self):
        assert find_interactions([]) == []

    def test_only_persons(self):
        p1 = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=30, y2=80, track_id=1)
        p2 = Detection(cls="person", conf=0.9, x1=40, y1=0, x2=70, y2=80, track_id=2)
        interactions = find_interactions([p1, p2])
        assert len(interactions) == 0


# ── TrackHistory ─────────────────────────────────────────────────────

class TestTrackHistory:
    def test_update_and_retrieve(self):
        th = TrackHistory()
        det = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=50, y2=100, track_id=1)
        th.update(det, t=1.0)
        th.update(det, t=2.0)
        samples = th.samples(1)
        assert len(samples) == 2

    def test_none_track_id_ignored(self):
        th = TrackHistory()
        det = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=50, y2=100, track_id=None)
        th.update(det, t=1.0)
        assert th.samples(None) == []

    def test_prune_removes_stale(self):
        th = TrackHistory()
        det = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=50, y2=100, track_id=5)
        th.update(det, t=1.0)
        th.prune(live_ids=set(), now=100.0, stale_sec=10.0)
        assert th.samples(5) == []

    def test_prune_keeps_live(self):
        th = TrackHistory()
        det = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=50, y2=100, track_id=5)
        th.update(det, t=1.0)
        th.prune(live_ids={5}, now=2.0)
        assert len(th.samples(5)) == 1

    def test_maxlen_enforced(self):
        th = TrackHistory(maxlen=3)
        det = Detection(cls="person", conf=0.9, x1=0, y1=0, x2=50, y2=100, track_id=1)
        for i in range(10):
            th.update(det, t=float(i))
        assert len(th.samples(1)) == 3


# ── Detection dataclass ─────────────────────────────────────────────

class TestDetection:
    def test_center(self, sample_detection):
        cx, cy = sample_detection.center
        assert cx == 130.0
        assert cy == 300.0

    def test_width(self, sample_detection):
        assert sample_detection.width == 60

    def test_height(self, sample_detection):
        assert sample_detection.height == 200

    def test_bottom(self, sample_detection):
        assert sample_detection.bottom == 400


# ── build_event_summary ──────────────────────────────────────────────

class TestBuildEventSummary:
    def test_with_distance_m(self, sample_detection, sample_vehicle):
        s = build_event_summary(
            "pedestrian_proximity", sample_detection, sample_vehicle,
            45.0, "high", ttc_sec=1.2, distance_m=2.8,
        )
        assert "2.8m" in s
        assert "TTC" in s
        assert "high" in s

    def test_without_distance_m(self, sample_detection, sample_vehicle):
        s = build_event_summary(
            "pedestrian_proximity", sample_detection, sample_vehicle,
            45.0, "medium",
        )
        assert "45px" in s
        assert "medium" in s


# ── SceneContextClassifier ───────────────────────────────────────────

class TestSceneContext:
    def test_classify_returns_result(self):
        from road_safety.core.context import SceneContextClassifier
        sc = SceneContextClassifier()
        ctx = sc.classify()
        assert ctx.label in ("urban", "highway", "parking", "unknown")

    def test_observe_then_classify(self):
        from road_safety.core.context import SceneContextClassifier
        sc = SceneContextClassifier()
        dets = [
            Detection(cls="person", conf=0.9, x1=0, y1=0, x2=30, y2=80, track_id=i)
            for i in range(5)
        ] + [
            Detection(cls="car", conf=0.9, x1=100*i, y1=200, x2=100*i+80, y2=300, track_id=10+i)
            for i in range(3)
        ]
        now = time.time()
        for i in range(20):
            sc.observe(dets, now + i * 0.5, speed_proxy_mps=2.0)
        ctx = sc.classify()
        assert ctx.label != ""
        assert ctx.confidence >= 0

    def test_adaptive_thresholds(self):
        from road_safety.core.context import SceneContextClassifier
        sc = SceneContextClassifier()
        ctx = sc.classify()
        thr = sc.adaptive_thresholds(ctx)
        assert thr.ttc_high_sec > 0
        assert thr.ttc_med_sec > thr.ttc_high_sec
        assert thr.dist_high_m > 0
        assert thr.dist_med_m > thr.dist_high_m


# ── QualityMonitor ───────────────────────────────────────────────────

class TestQualityMonitor:
    def test_initial_state_nominal(self):
        from road_safety.core.quality import QualityMonitor
        qm = QualityMonitor()
        s = qm.state()
        assert s["state"] == "nominal"
        assert "reason" in s
        assert "samples" in s

    def test_risk_adjustment_shape(self):
        from road_safety.core.quality import QualityMonitor
        qm = QualityMonitor()
        adj = qm.risk_adjustment()
        assert "ttc_multiplier" in adj
        assert "pixel_dist_multiplier" in adj
        assert adj["ttc_multiplier"] >= 1.0
        assert adj["pixel_dist_multiplier"] >= 1.0
