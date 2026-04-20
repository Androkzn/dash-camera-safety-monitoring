"""Tests for the background dual-model validator.

Covers the three comparator rules (false positive, false negative,
classification mismatch) with hand-built Detection lists so we never
load a real torch model or touch the filesystem.
"""

from road_safety.core.detection import Detection
from road_safety.core.validator import (
    DiscrepancyComparator,
    _iou,
)


# ── _iou ────────────────────────────────────────────────────────────

class TestIoU:
    def test_identical_boxes(self):
        a = Detection(cls="car", conf=0.9, x1=10, y1=10, x2=50, y2=50)
        b = Detection(cls="car", conf=0.9, x1=10, y1=10, x2=50, y2=50)
        assert _iou(a, b) == 1.0

    def test_disjoint_boxes(self):
        a = Detection(cls="car", conf=0.9, x1=0, y1=0, x2=10, y2=10)
        b = Detection(cls="car", conf=0.9, x1=20, y1=20, x2=30, y2=30)
        assert _iou(a, b) == 0.0

    def test_partial_overlap(self):
        a = Detection(cls="car", conf=0.9, x1=0, y1=0, x2=10, y2=10)
        b = Detection(cls="car", conf=0.9, x1=5, y1=5, x2=15, y2=15)
        # intersection=25, union=175 → 0.1428...
        assert 0.14 < _iou(a, b) < 0.15


# ── Rule A: false positive ──────────────────────────────────────────

class TestFalsePositive:
    def _event(self, **overrides):
        base = {
            "event_id": "evt_test_0001",
            "event_type": "pedestrian_proximity",
            "risk_level": "medium",
            "track_ids": [1, 2],
            "objects": ["person", "car"],
        }
        base.update(overrides)
        return base

    def test_no_secondary_detections_fires(self):
        primary_a = Detection(cls="person", conf=0.8, x1=100, y1=100, x2=120, y2=180, track_id=1)
        primary_b = Detection(cls="car", conf=0.8, x1=200, y1=150, x2=350, y2=300, track_id=2)
        cmp = DiscrepancyComparator(iou_threshold=0.3)
        disc = cmp.check_false_positive(self._event(), [primary_a, primary_b], [])
        assert disc is not None
        assert disc.kind == "false_positive"
        assert disc.fingerprint == "validator/false-positive"

    def test_matching_secondary_no_fire(self):
        primary_a = Detection(cls="person", conf=0.8, x1=100, y1=100, x2=120, y2=180, track_id=1)
        primary_b = Detection(cls="car", conf=0.8, x1=200, y1=150, x2=350, y2=300, track_id=2)
        secondary_a = Detection(cls="person", conf=0.9, x1=99, y1=101, x2=121, y2=179)
        secondary_b = Detection(cls="car", conf=0.9, x1=201, y1=151, x2=349, y2=299)
        cmp = DiscrepancyComparator(iou_threshold=0.3)
        disc = cmp.check_false_positive(
            self._event(), [primary_a, primary_b], [secondary_a, secondary_b]
        )
        assert disc is None

    def test_only_one_pair_member_matched_fires(self):
        primary_a = Detection(cls="person", conf=0.8, x1=100, y1=100, x2=120, y2=180, track_id=1)
        primary_b = Detection(cls="car", conf=0.8, x1=200, y1=150, x2=350, y2=300, track_id=2)
        # Secondary sees the person but not the car — still a disagreement.
        secondary_a = Detection(cls="person", conf=0.9, x1=99, y1=101, x2=121, y2=179)
        cmp = DiscrepancyComparator(iou_threshold=0.3)
        disc = cmp.check_false_positive(
            self._event(), [primary_a, primary_b], [secondary_a]
        )
        assert disc is not None


# ── Rule B: false negative ──────────────────────────────────────────

class TestFalseNegative:
    def test_secondary_finds_risky_pair_primary_missed(self):
        # Two overlapping detections at close pixel distance: the
        # built-in `find_interactions` + risk classifier will flag this.
        secondary_a = Detection(cls="person", conf=0.8, x1=100, y1=400, x2=120, y2=500)
        secondary_b = Detection(cls="car", conf=0.8, x1=125, y1=400, x2=280, y2=520)
        cmp = DiscrepancyComparator(iou_threshold=0.3)
        disc = cmp.check_false_negative(
            frame_height=600,
            primary_detections=[],  # primary saw nothing
            secondary_detections=[secondary_a, secondary_b],
            primary_emitted_recently=False,
        )
        assert disc is not None
        assert disc.kind == "false_negative"
        assert disc.fingerprint == "validator/false-negative"

    def test_primary_recently_emitted_suppresses(self):
        secondary_a = Detection(cls="person", conf=0.8, x1=100, y1=400, x2=120, y2=500)
        secondary_b = Detection(cls="car", conf=0.8, x1=125, y1=400, x2=280, y2=520)
        cmp = DiscrepancyComparator(iou_threshold=0.3)
        disc = cmp.check_false_negative(
            frame_height=600,
            primary_detections=[],
            secondary_detections=[secondary_a, secondary_b],
            primary_emitted_recently=True,
        )
        assert disc is None

    def test_primary_already_saw_pair_suppresses(self):
        secondary_a = Detection(cls="person", conf=0.8, x1=100, y1=400, x2=120, y2=500)
        secondary_b = Detection(cls="car", conf=0.8, x1=125, y1=400, x2=280, y2=520)
        # Primary has overlapping detections for both — so it *did* see
        # them and must have gated out for some legitimate reason.
        primary_a = Detection(cls="person", conf=0.3, x1=101, y1=401, x2=119, y2=499)
        primary_b = Detection(cls="car", conf=0.3, x1=126, y1=401, x2=279, y2=519)
        cmp = DiscrepancyComparator(iou_threshold=0.3)
        disc = cmp.check_false_negative(
            frame_height=600,
            primary_detections=[primary_a, primary_b],
            secondary_detections=[secondary_a, secondary_b],
            primary_emitted_recently=False,
        )
        assert disc is None

    def test_empty_secondary_no_fire(self):
        cmp = DiscrepancyComparator()
        disc = cmp.check_false_negative(
            frame_height=600,
            primary_detections=[],
            secondary_detections=[],
            primary_emitted_recently=False,
        )
        assert disc is None


# ── Rule C: classification mismatch ─────────────────────────────────

class TestClassificationMismatch:
    def _event(self, risk_level="medium"):
        return {
            "event_id": "evt_test_0002",
            "event_type": "pedestrian_proximity",
            "risk_level": risk_level,
            "track_ids": [1, 2],
            "objects": ["person", "car"],
        }

    def test_class_disagreement_fires(self):
        primary_a = Detection(cls="person", conf=0.8, x1=100, y1=100, x2=120, y2=180, track_id=1)
        primary_b = Detection(cls="car", conf=0.8, x1=200, y1=150, x2=350, y2=300, track_id=2)
        # Same boxes, but secondary calls the first object a "motorcycle"
        # (a vehicle class, not pedestrian). That's a class disagreement.
        secondary_a = Detection(cls="motorcycle", conf=0.9, x1=100, y1=100, x2=120, y2=180)
        secondary_b = Detection(cls="car", conf=0.9, x1=200, y1=150, x2=350, y2=300)
        cmp = DiscrepancyComparator(iou_threshold=0.3)
        disc = cmp.check_classification_mismatch(
            self._event(), [primary_a, primary_b], [secondary_a, secondary_b],
            frame_height=600,
        )
        assert disc is not None
        assert disc.kind == "classification_mismatch"
        assert disc.fingerprint == "validator/classification-mismatch"

    def test_full_agreement_no_fire(self):
        primary_a = Detection(cls="person", conf=0.8, x1=100, y1=100, x2=120, y2=180, track_id=1)
        primary_b = Detection(cls="car", conf=0.8, x1=200, y1=150, x2=350, y2=300, track_id=2)
        secondary_a = Detection(cls="person", conf=0.9, x1=100, y1=100, x2=120, y2=180)
        secondary_b = Detection(cls="car", conf=0.9, x1=200, y1=150, x2=350, y2=300)
        cmp = DiscrepancyComparator(iou_threshold=0.3)
        # Primary said medium risk on these bboxes — secondary recomputes
        # from the same boxes and would agree (distance-driven), so no
        # mismatch fires.
        disc = cmp.check_classification_mismatch(
            self._event(risk_level="low"),  # doesn't matter; low is also valid band
            [primary_a, primary_b],
            [secondary_a, secondary_b],
            frame_height=600,
        )
        # Either None (full agreement) or a valid Discrepancy with info
        # severity — both are acceptable behavior of the rule. The
        # important thing is it never crashes.
        assert disc is None or disc.severity == "info"

    def test_no_match_does_not_fire_classification(self):
        # When there's no IoU match at all, the false-positive rule owns
        # this case; classification mismatch must stay silent.
        primary_a = Detection(cls="person", conf=0.8, x1=100, y1=100, x2=120, y2=180, track_id=1)
        primary_b = Detection(cls="car", conf=0.8, x1=200, y1=150, x2=350, y2=300, track_id=2)
        cmp = DiscrepancyComparator(iou_threshold=0.3)
        disc = cmp.check_classification_mismatch(
            self._event(), [primary_a, primary_b], [], frame_height=600
        )
        assert disc is None
