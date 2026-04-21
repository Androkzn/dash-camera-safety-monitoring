"""Tests for the shadow-only detection store + miss-reason analyzer.

These tests never touch the real ``data/`` directory: ``tmp_path`` is
used as a sandbox via monkeypatched module-level paths, so running
``pytest`` in parallel or on a production host can't leak records into
the live store.
"""

import json
from unittest.mock import patch

import numpy as np
import pytest

from road_safety.core import shadow_analysis, shadow_store
from road_safety.core.detection import Detection


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Redirect the store's on-disk paths into ``tmp_path`` for the test.

    Patches the module-level constants both on :mod:`shadow_store` and
    on :mod:`road_safety.config` (where the store re-exports from). We
    also set a small cap so rotation is exercisable in one test.
    """
    thumbs = tmp_path / "thumbnails"
    data = tmp_path
    thumbs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(shadow_store, "THUMBS_DIR", thumbs)
    monkeypatch.setattr(shadow_store, "DATA_DIR", data)
    monkeypatch.setattr(shadow_store, "_RECORDS_PATH", data / "shadow_records.jsonl")
    monkeypatch.setattr(shadow_store, "SHADOW_STORE_MAX_RECORDS", 4)
    return {"thumbs": thumbs, "data": data}


def _fake_frame(h=600, w=800):
    """Zero-filled BGR frame — fast to redact, no real pixel data."""
    return np.zeros((h, w, 3), dtype=np.uint8)


def _pedestrian_proximity_pair():
    a = Detection(cls="person", conf=0.85, x1=100, y1=400, x2=120, y2=500, track_id=None)
    b = Detection(cls="car", conf=0.82, x1=125, y1=400, x2=280, y2=520, track_id=None)
    return a, b


# ---------------------------------------------------------------------------
# shadow_store.save / load round-trip
# ---------------------------------------------------------------------------


class TestSaveLoad:
    def test_save_writes_thumbnail_and_record(self, sandbox):
        a, b = _pedestrian_proximity_pair()
        rec = shadow_store.save(
            shadow_id="deadbeef0001",
            slot_id="primary",
            wall_ts=1700000000.0,
            event_type="pedestrian_proximity",
            secondary_risk="medium",
            distance_m=3.4,
            distance_px=15.0,
            frame=_fake_frame(),
            secondary_pair=(a, b),
            secondary_detections=[a, b],
            primary_detections=[],
        )
        assert rec is not None
        assert rec.shadow_id == "deadbeef0001"
        assert rec.slot_id == "primary"
        assert rec.secondary_risk == "medium"
        assert rec.distance_m == pytest.approx(3.4)
        assert rec.thumbnail.endswith("shadow_deadbeef0001.jpg")
        # Thumbnail physically present on disk
        assert shadow_store.thumbnail_path("deadbeef0001").exists()
        # JSONL line is parseable
        line = sandbox["data"].joinpath("shadow_records.jsonl").read_text().strip()
        parsed = json.loads(line)
        assert parsed["shadow_id"] == "deadbeef0001"
        assert parsed["secondary_pair"][0]["cls"] == "person"

    def test_load_returns_most_recent(self, sandbox):
        a, b = _pedestrian_proximity_pair()
        # Save twice with the same id — load must return the most recent.
        shadow_store.save(
            shadow_id="id_reused",
            slot_id="primary",
            wall_ts=1.0,
            event_type="pedestrian_proximity",
            secondary_risk="medium",
            distance_m=9.0,
            distance_px=20.0,
            frame=_fake_frame(),
            secondary_pair=(a, b),
            secondary_detections=[a, b],
            primary_detections=[],
        )
        shadow_store.save(
            shadow_id="id_reused",
            slot_id="primary",
            wall_ts=2.0,
            event_type="pedestrian_proximity",
            secondary_risk="high",
            distance_m=1.1,
            distance_px=5.0,
            frame=_fake_frame(),
            secondary_pair=(a, b),
            secondary_detections=[a, b],
            primary_detections=[],
        )
        rec = shadow_store.load("id_reused")
        assert rec is not None
        assert rec.secondary_risk == "high"
        assert rec.wall_ts == pytest.approx(2.0)

    def test_load_missing_returns_none(self, sandbox):
        assert shadow_store.load("never-existed") is None

    def test_read_frame_returns_ndarray(self, sandbox):
        a, b = _pedestrian_proximity_pair()
        shadow_store.save(
            shadow_id="frame_test",
            slot_id="primary",
            wall_ts=1.0,
            event_type="pedestrian_proximity",
            secondary_risk="medium",
            distance_m=4.0,
            distance_px=20.0,
            frame=_fake_frame(480, 640),
            secondary_pair=(a, b),
            secondary_detections=[a, b],
            primary_detections=[],
        )
        arr = shadow_store.read_frame("frame_test")
        assert arr is not None
        # Shape is preserved through the redact + cv2 round-trip.
        assert arr.shape == (480, 640, 3)

    def test_save_handles_thumbnail_write_failure(self, sandbox, monkeypatch):
        """A cv2.imwrite failure must not raise and must not write a record."""
        a, b = _pedestrian_proximity_pair()
        monkeypatch.setattr(
            shadow_store, "_write_thumbnail", lambda *a, **kw: False,
        )
        rec = shadow_store.save(
            shadow_id="thumb_fail",
            slot_id="primary",
            wall_ts=1.0,
            event_type="pedestrian_proximity",
            secondary_risk="medium",
            distance_m=4.0,
            distance_px=20.0,
            frame=_fake_frame(),
            secondary_pair=(a, b),
            secondary_detections=[a, b],
            primary_detections=[],
        )
        assert rec is None
        assert shadow_store.load("thumb_fail") is None


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


class TestRotation:
    def test_rotation_drops_oldest_and_unlinks_thumbnails(self, sandbox):
        """Cap=4; after 6 saves only the 4 most recent should remain, and
        the rotated records' thumbnails must be unlinked."""
        a, b = _pedestrian_proximity_pair()
        ids = [f"id_{i:04d}" for i in range(6)]
        for sid in ids:
            shadow_store.save(
                shadow_id=sid,
                slot_id="primary",
                wall_ts=1.0,
                event_type="pedestrian_proximity",
                secondary_risk="medium",
                distance_m=4.0,
                distance_px=20.0,
                frame=_fake_frame(),
                secondary_pair=(a, b),
                secondary_detections=[a, b],
                primary_detections=[],
            )
        lines = sandbox["data"].joinpath("shadow_records.jsonl").read_text().splitlines()
        # Cap is 4, we saved 6, so we keep ≤ 4 rows after rotation.
        assert 0 < len(lines) <= 4
        kept = {json.loads(l)["shadow_id"] for l in lines}
        dropped = set(ids) - kept
        assert dropped, "rotation must drop at least one id"
        # Rotated records' thumbnails should be unlinked on disk.
        for sid in dropped:
            assert not shadow_store.thumbnail_path(sid).exists()
        # Kept thumbnails still present.
        for sid in kept:
            assert shadow_store.thumbnail_path(sid).exists()


# ---------------------------------------------------------------------------
# shadow_analysis.analyze — per-gate diagnostic
# ---------------------------------------------------------------------------


def _record_with(primary_detections, secondary_pair, event_type="pedestrian_proximity"):
    """Build a ShadowRecord in-memory (bypassing the disk writer) so the
    analyzer test is pure-logic."""
    from road_safety.core.shadow_store import (
        ShadowRecord,
        _det_to_dict,
    )

    a, b = secondary_pair
    return ShadowRecord(
        shadow_id="analysis_only",
        slot_id="primary",
        wall_ts=1.0,
        event_type=event_type,
        secondary_risk="medium",
        distance_m=4.0,
        distance_px=15.0,
        frame_h=600,
        frame_w=800,
        secondary_pair=[_det_to_dict(a), _det_to_dict(b)],
        secondary_detections=[_det_to_dict(a), _det_to_dict(b)],
        primary_detections=[_det_to_dict(d) for d in primary_detections],
        thumbnail="thumbnails/shadow_analysis_only.jpg",
    )


class TestAnalyze:
    def test_primary_never_saw_flagged_as_detected_gate(self):
        a, b = _pedestrian_proximity_pair()
        record = _record_with(primary_detections=[], secondary_pair=(a, b))
        result = shadow_analysis.analyze(record)
        # The "detected" gate should fail on at least one member when
        # primary_detections is empty.
        detected_gates = [
            g for m in result.members for g in m.gates if g.gate == "detected"
        ]
        assert all(not g.passed for g in detected_gates)
        assert "detected" in result.miss_reason or "no primary" in result.miss_reason

    def test_low_confidence_vehicle_flagged_as_confidence_gate(self):
        # Two vehicles. Secondary produced high-conf pair; primary saw one
        # of them at 0.35 conf — below CONF_THRESHOLD 0.50 → would have
        # been filtered. The analyzer should surface this as the miss
        # reason via the "detected" gate (IoU match is noisy at low
        # confidence, but either way one of the gates must fail).
        sa = Detection(cls="car", conf=0.95, x1=100, y1=200, x2=220, y2=320)
        sb = Detection(cls="car", conf=0.92, x1=225, y1=205, x2=350, y2=325)
        pa = Detection(cls="car", conf=0.35, x1=100, y1=200, x2=220, y2=320)
        record = _record_with(
            primary_detections=[pa],
            secondary_pair=(sa, sb),
            event_type="vehicle_close_interaction",
        )
        result = shadow_analysis.analyze(record)
        # At least one gate must fail, giving the UI something to show.
        failed = [g for m in result.members for g in m.gates if not g.passed]
        failed.extend(g for g in result.pair_gates if not g.passed)
        assert failed, "analyzer should flag at least one gate failure"
        assert result.miss_reason  # non-empty headline

    def test_all_gates_pass_returns_downstream_message(self):
        # Fabricate a case where the primary saw both members at good
        # confidence + area + aspect ratio. Then the analyzer's
        # offline-checkable gates all pass and it must disclose that
        # the miss is downstream (convergence / TTC / etc).
        sa = Detection(cls="person", conf=0.85, x1=100, y1=400, x2=130, y2=500)
        sb = Detection(cls="car", conf=0.82, x1=140, y1=400, x2=280, y2=520)
        pa = Detection(cls="person", conf=0.85, x1=100, y1=400, x2=130, y2=500)
        pb = Detection(cls="car", conf=0.82, x1=140, y1=400, x2=280, y2=520)
        record = _record_with(
            primary_detections=[pa, pb], secondary_pair=(sa, sb),
        )
        result = shadow_analysis.analyze(record)
        all_passed = (
            all(g.passed for m in result.members for g in m.gates)
            and all(g.passed for g in result.pair_gates)
        )
        assert all_passed
        assert "downstream" in result.miss_reason


# ---------------------------------------------------------------------------
# Validator wiring — shadow_id gets stamped on false-negative findings
# ---------------------------------------------------------------------------


class TestValidatorShadowIntegration:
    def test_false_negative_persists_shadow_and_stamps_evidence(self):
        from road_safety.core import validator as validator_mod
        from road_safety.core.validator import (
            DiscrepancyComparator, ValidatorJob, ValidatorWorker,
        )

        # Spy replaces the real shadow_store.save so the test stays pure.
        calls = []

        def _spy_save(**kwargs):
            calls.append(kwargs)
            return object()  # non-None sentinel so the worker thinks it succeeded

        written = []

        def _fake_write_finding(finding):
            written.append(finding)

        class _FakeFinding:
            """Mimics just enough of WatchdogFinding: snapshot_id + evidence list."""

            def __init__(self, **kwargs):
                self.snapshot_id = "spyid00112233"
                self.evidence = list(kwargs.get("evidence") or [])
                self.kind_marker = kwargs

        worker = ValidatorWorker(
            detector=object(),  # unused on the code path we exercise
            comparator=DiscrepancyComparator(),
            write_finding=_fake_write_finding,
            finding_ctor=_FakeFinding,
            save_shadow_record=_spy_save,
        )

        a = Detection(cls="person", conf=0.85, x1=100, y1=400, x2=120, y2=500)
        b = Detection(cls="car", conf=0.82, x1=125, y1=400, x2=280, y2=520)
        cmp = DiscrepancyComparator()
        disc = cmp.check_false_negative(
            frame_height=600,
            primary_detections=[],
            secondary_detections=[a, b],
            primary_emitted_recently=False,
        )
        assert disc is not None

        job = ValidatorJob(
            kind="sampled",
            slot_id="primary",
            wall_ts=123.0,
            frame=_fake_frame(),
            primary_detections=[],
        )
        # Directly exercise _emit (private, but the invariant under test
        # is behavioural: one call produces one shadow save + one finding
        # write, with shadow_id propagated to evidence).
        worker._emit(disc, job, [a, b])

        assert len(calls) == 1
        assert calls[0]["shadow_id"] == "spyid00112233"
        assert calls[0]["slot_id"] == "primary"
        assert calls[0]["event_type"] == "pedestrian_proximity"

        assert len(written) == 1
        finding = written[0]
        labels = {e["label"] for e in finding.evidence}
        assert "shadow_id" in labels
