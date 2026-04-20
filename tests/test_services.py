"""Tests for road_safety.services — vehicle registry, drift, LLM obs, redact, digest."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from road_safety.core.detection import Detection
from road_safety.services.registry import RoadRegistry, VehicleState, MAX_SCORE


# ═══════════════════════════════════════════════════════════════════
# RoadRegistry
# ═══════════════════════════════════════════════════════════════════

class TestRoadRegistry:
    def test_record_event_creates_vehicle(self):
        reg = RoadRegistry()
        reg.record_event({"vehicle_id": "v1", "risk_level": "low", "event_type": "tailgating"})
        v = reg.get_vehicle("v1")
        assert v is not None
        assert v["total_events"] == 1
        assert v["events_by_risk"]["low"] == 1
        assert v["events_by_type"]["tailgating"] == 1

    def test_record_event_penalty(self):
        reg = RoadRegistry()
        reg.record_event({"vehicle_id": "v1", "risk_level": "high"})
        v = reg.get_vehicle("v1")
        assert v["safety_score"] == MAX_SCORE - 10

    def test_multiple_events(self):
        reg = RoadRegistry()
        for _ in range(3):
            reg.record_event({"vehicle_id": "v1", "risk_level": "medium"})
        v = reg.get_vehicle("v1")
        assert v["total_events"] == 3
        assert v["safety_score"] == MAX_SCORE - 9

    def test_safety_score_never_negative(self):
        reg = RoadRegistry()
        for _ in range(20):
            reg.record_event({"vehicle_id": "v1", "risk_level": "high"})
        v = reg.get_vehicle("v1")
        assert v["safety_score"] >= 0

    def test_record_feedback(self):
        reg = RoadRegistry()
        reg.record_event({"vehicle_id": "v1"})
        reg.record_feedback("evt_1", "tp", "v1")
        reg.record_feedback("evt_2", "fp", "v1")
        v = reg.get_vehicle("v1")
        assert v["feedback_tp"] == 1
        assert v["feedback_fp"] == 1
        assert v["precision"] == 0.5

    def test_decay_scores(self):
        reg = RoadRegistry()
        reg.record_event({"vehicle_id": "v1", "risk_level": "high"})
        score_before = reg.get_vehicle("v1")["safety_score"]
        reg.decay_scores()
        score_after = reg.get_vehicle("v1")["safety_score"]
        assert score_after > score_before

    def test_decay_does_not_exceed_max(self):
        reg = RoadRegistry()
        reg.record_event({"vehicle_id": "v1", "risk_level": "low"})
        for _ in range(300):
            reg.decay_scores()
        v = reg.get_vehicle("v1")
        assert v["safety_score"] == MAX_SCORE

    def test_get_unknown_vehicle(self):
        reg = RoadRegistry()
        assert reg.get_vehicle("unknown") is None

    def test_road_summary_empty(self):
        reg = RoadRegistry()
        s = reg.road_summary()
        assert s["vehicle_count"] == 0
        assert s["total_events"] == 0
        assert s["lowest_score_vehicle"] is None

    def test_road_summary_multi_vehicle(self):
        reg = RoadRegistry()
        reg.record_event({"vehicle_id": "v1", "risk_level": "high"})
        reg.record_event({"vehicle_id": "v2", "risk_level": "low"})
        s = reg.road_summary()
        assert s["vehicle_count"] == 2
        assert s["total_events"] == 2
        assert s["lowest_score_vehicle"]["vehicle_id"] == "v1"

    def test_driver_leaderboard_ranking(self):
        reg = RoadRegistry()
        reg.record_event({"vehicle_id": "v1", "driver_id": "d1", "risk_level": "high"})
        reg.record_event({"vehicle_id": "v2", "driver_id": "d2", "risk_level": "low"})
        lb = reg.driver_leaderboard()
        assert lb[0]["driver_id"] == "d1"
        assert lb[0]["safety_score"] < lb[1]["safety_score"]

    def test_driver_leaderboard_limit(self):
        reg = RoadRegistry()
        for i in range(25):
            reg.record_event({"vehicle_id": f"v{i}", "driver_id": f"d{i}"})
        lb = reg.driver_leaderboard(limit=5)
        assert len(lb) == 5


class TestVehicleState:
    def test_as_dict_shape(self):
        vs = VehicleState(vehicle_id="v1", road_id="r1", driver_id="d1")
        d = vs.as_dict()
        assert "vehicle_id" in d
        assert "safety_score" in d
        assert "precision" in d


# ═══════════════════════════════════════════════════════════════════
# LLM Observer
# ═══════════════════════════════════════════════════════════════════

class TestLLMObserver:
    def test_record_basic(self):
        from road_safety.services.llm_obs import LLMObserver
        obs = LLMObserver()
        rec = obs.record("narration", "haiku", input_tokens=100, output_tokens=50, latency_ms=200)
        assert rec.call_type == "narration"
        assert rec.success is True

    def test_record_error(self):
        from road_safety.services.llm_obs import LLMObserver
        obs = LLMObserver()
        obs.record("chat", "haiku", success=False, error="rate_limited")
        s = obs.stats()
        assert s["total_errors_all_time"] == 1

    def test_record_skip(self):
        from road_safety.services.llm_obs import LLMObserver
        obs = LLMObserver()
        obs.record_skip("narration", "haiku", reason="no_api_key")
        s = obs.stats()
        assert s["total_skips_all_time"] == 1

    def test_stats_empty(self):
        from road_safety.services.llm_obs import LLMObserver
        obs = LLMObserver()
        s = obs.stats()
        assert s["window_calls"] == 0
        assert s["cost_usd"] == 0.0

    def test_stats_with_data(self):
        from road_safety.services.llm_obs import LLMObserver
        obs = LLMObserver()
        for i in range(5):
            obs.record("narration", "haiku", input_tokens=100, output_tokens=50, latency_ms=100 + i * 50)
        s = obs.stats()
        assert s["window_calls"] == 5
        assert s["cost_usd"] > 0
        assert s["latency_p50_ms"] > 0

    def test_stats_window_filter(self):
        from road_safety.services.llm_obs import LLMObserver
        obs = LLMObserver()
        obs.record("chat", "haiku", input_tokens=50, output_tokens=25, latency_ms=100)
        s = obs.stats(window_sec=3600)
        assert s["window_calls"] == 1

    def test_recent(self):
        from road_safety.services.llm_obs import LLMObserver
        obs = LLMObserver()
        obs.record("chat", "haiku", input_tokens=50, output_tokens=25, latency_ms=100)
        recent = obs.recent(n=10)
        assert len(recent) == 1
        assert recent[0]["call_type"] == "chat"

    def test_ring_buffer_cap(self):
        from road_safety.services.llm_obs import LLMObserver
        obs = LLMObserver(max_records=5)
        for i in range(10):
            obs.record("chat", "haiku", input_tokens=i)
        assert len(obs.recent(n=100)) == 5

    def test_estimated_cost(self):
        from road_safety.services.llm_obs import LLMRecord
        rec = LLMRecord(
            call_type="chat", model="default",
            input_tokens=1000, output_tokens=500,
            latency_ms=200, success=True,
        )
        assert rec.estimated_cost_usd > 0

    def test_stats_include_top_errors(self):
        from road_safety.services.llm_obs import LLMObserver
        obs = LLMObserver()
        obs.record("chat", "haiku", success=False, error="429 Too Many Requests")
        obs.record("chat", "haiku", success=False, error="429 Too Many Requests")
        obs.record("chat", "haiku", success=False, error="auth failed")
        stats = obs.stats()
        assert stats["top_errors"][0]["error"] == "429 Too Many Requests"
        assert stats["top_errors"][0]["count"] == 2


# ═══════════════════════════════════════════════════════════════════
# DriftMonitor
# ═══════════════════════════════════════════════════════════════════

class TestDriftMonitor:
    def _write_feedback(self, path: Path, entries: list[dict]):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def _write_events(self, path: Path, events: list[dict]):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(events))

    def test_compute_empty(self, _isolate_data_dir):
        from road_safety.services.drift import DriftMonitor
        dm = DriftMonitor(feedback_path=_isolate_data_dir / "feedback.jsonl")
        r = dm.compute()
        assert r.window_size == 0
        assert r.precision == 0.0

    def test_compute_all_tp(self, _isolate_data_dir):
        from road_safety.services.drift import DriftMonitor
        fb_path = _isolate_data_dir / "feedback.jsonl"
        ev_path = _isolate_data_dir / "events.json"
        self._write_feedback(fb_path, [
            {"event_id": f"e{i}", "verdict": "tp", "operator_ts": f"2026-04-15T0{i}:00:00Z"}
            for i in range(5)
        ])
        self._write_events(ev_path, [
            {"event_id": f"e{i}", "risk_level": "high", "event_type": "pedestrian_proximity"}
            for i in range(5)
        ])
        dm = DriftMonitor(feedback_path=fb_path, events_path=ev_path, window_size=10)
        r = dm.compute()
        assert r.precision == 1.0
        assert r.true_positives == 5
        assert r.false_positives == 0
        assert r.alert_triggered is False

    def test_compute_low_precision_triggers_alert(self, _isolate_data_dir):
        from road_safety.services.drift import DriftMonitor
        fb_path = _isolate_data_dir / "feedback.jsonl"
        entries = [{"event_id": f"e{i}", "verdict": "fp", "operator_ts": f"2026-04-15T0{i}:00:00Z"} for i in range(4)]
        entries.append({"event_id": "eX", "verdict": "tp", "operator_ts": "2026-04-15T05:00:00Z"})
        self._write_feedback(fb_path, entries)
        dm = DriftMonitor(feedback_path=fb_path, window_size=10, alert_threshold=0.7)
        r = dm.compute()
        assert r.precision < 0.7
        assert r.alert_triggered is True

    def test_in_memory_event_source(self, _isolate_data_dir):
        from road_safety.services.drift import DriftMonitor
        fb_path = _isolate_data_dir / "feedback.jsonl"
        self._write_feedback(fb_path, [
            {"event_id": "live1", "verdict": "tp"}
        ])
        dm = DriftMonitor(feedback_path=fb_path, window_size=10)
        dm.set_event_source(lambda: [{"event_id": "live1", "risk_level": "medium", "event_type": "tailgating"}])
        r = dm.compute()
        assert r.true_positives == 1
        assert "medium" in r.by_risk_level or r.by_risk_level.get("medium") is not None


class TestDriftWarning:
    def test_none_when_no_alert(self):
        from road_safety.services.drift import DriftReport, drift_warning_message
        report = DriftReport(
            window_size=10, true_positives=8, false_positives=2,
            precision=0.8, by_risk_level={}, by_event_type={},
            window_start_ts="", window_end_ts="",
            alert_triggered=False, trend="stable",
        )
        assert drift_warning_message(report) is None

    def test_message_when_alert(self):
        from road_safety.services.drift import DriftReport, drift_warning_message
        report = DriftReport(
            window_size=10, true_positives=3, false_positives=7,
            precision=0.3, by_risk_level={}, by_event_type={
                "pedestrian_proximity": {"precision": 0.2, "tp": 1, "fp": 4, "status": "ok"},
            },
            window_start_ts="", window_end_ts="",
            alert_triggered=True, trend="degrading",
        )
        msg = drift_warning_message(report)
        assert msg is not None
        assert "0.30" in msg
        assert "pedestrian_proximity" in msg


# ═══════════════════════════════════════════════════════════════════
# ActiveLearningSampler
# ═══════════════════════════════════════════════════════════════════

class TestActiveLearningSampler:
    def test_maybe_sample_outside_boundary(self, _isolate_data_dir):
        from road_safety.services.drift import ActiveLearningSampler
        als = ActiveLearningSampler(out_dir=_isolate_data_dir / "al")
        result = als.maybe_sample({"event_id": "e1", "confidence": 0.9})
        assert result is None

    def test_maybe_sample_inside_boundary(self, _isolate_data_dir):
        from road_safety.services.drift import ActiveLearningSampler
        als = ActiveLearningSampler(out_dir=_isolate_data_dir / "al")
        als._rng.random = lambda: 0.0
        result = als.maybe_sample({"event_id": "e1", "confidence": 0.42})
        assert result is not None
        assert result.reason == "decision_boundary"

    def test_sample_disputed_always_creates(self, _isolate_data_dir):
        from road_safety.services.drift import ActiveLearningSampler
        als = ActiveLearningSampler(out_dir=_isolate_data_dir / "al")
        result = als.sample_disputed({"event_id": "e2", "confidence": 0.9}, note="wrong")
        assert result is not None
        assert result.reason == "disputed"
        pending = list(als.pending_dir.glob("*.json"))
        assert len(pending) == 1

    def test_export_batch_empty(self, _isolate_data_dir):
        from road_safety.services.drift import ActiveLearningSampler
        als = ActiveLearningSampler(out_dir=_isolate_data_dir / "al")
        assert als.export_batch() is None

    def test_export_batch_with_pending(self, _isolate_data_dir):
        from road_safety.services.drift import ActiveLearningSampler
        als = ActiveLearningSampler(out_dir=_isolate_data_dir / "al")
        als.sample_disputed({"event_id": "e3", "confidence": 0.8})
        zip_path = als.export_batch()
        assert zip_path is not None
        assert zip_path.suffix == ".zip"
        assert zip_path.exists()
        remaining = list(als.pending_dir.glob("*.json"))
        assert len(remaining) == 0


# ═══════════════════════════════════════════════════════════════════
# Redact
# ═══════════════════════════════════════════════════════════════════

class TestRedact:
    def test_hash_plate_deterministic(self):
        from road_safety.services.redact import hash_plate
        h1 = hash_plate("ABC123")
        h2 = hash_plate("ABC123")
        assert h1 == h2
        assert len(h1) > 8

    def test_hash_plate_different_plates(self):
        from road_safety.services.redact import hash_plate
        h1 = hash_plate("ABC123")
        h2 = hash_plate("XYZ789")
        assert h1 != h2

    def test_public_thumbnail_name(self):
        from road_safety.services.redact import public_thumbnail_name
        name = public_thumbnail_name("evt_1234")
        assert "public" in name
        assert "evt_1234" in name


# ═══════════════════════════════════════════════════════════════════
# Watchdog
# ═══════════════════════════════════════════════════════════════════

class TestWatchdog:
    def test_rule_checks_emit_actionable_fields(self):
        from road_safety.services.watchdog import _rule_checks

        snapshot = {
            "_interval_sec": 60,
            "server": {"running": True, "target_fps": 2.0},
            "pipeline": {"frames_read": 400, "frames_processed": 200},
            "perception": {
                "state": "degraded",
                "reason": "low_luminance",
                "avg_confidence": 0.61,
                "luminance": 28,
                "sharpness": 44,
                "samples": 18,
            },
            "drift": {
                "precision": 0.0,
                "feedback_coverage": 0.0,
                "labeled_events": 0,
                "total_events_in_window": 9,
                "true_positives": 0,
                "false_positives": 1,
                "trend": "degrading",
                "alert_triggered": True,
                "window_start_ts": "2026-04-15T05:49:36",
                "window_end_ts": "2026-04-15T05:49:36",
                "by_event_type": {"unknown": {"tp": 0, "fp": 1, "precision": None, "status": "insufficient"}},
            },
            "llm": {
                "window_calls": 7,
                "error_rate": 0.57,
                "latency_p50_ms": 1800,
                "latency_p95_ms": 12800,
                "total_errors_all_time": 11,
                "top_errors": [{"error": "429 Too Many Requests", "count": 4}],
                "by_type": {
                    "narration": {
                        "calls": 5,
                        "errors": 3,
                        "skips": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "latency_p50_ms": 1200,
                        "latency_p95_ms": 1400,
                    }
                },
            },
            "taxonomy": {
                "recent_events": 8,
                "unknown_event_types": 6,
                "unknown_risk_levels": 6,
                "unknown_event_ratio": 0.75,
                "unknown_risk_ratio": 0.75,
            },
        }
        prev_snapshot = {
            "pipeline": {"frames_processed": 200},
            "llm": {"latency_p50_ms": 900},
        }

        findings = _rule_checks(snapshot, prev_snapshot)

        assert findings
        drift_finding = next(f for f in findings if f.fingerprint == "drift/feedback-coverage")
        assert drift_finding.impact
        assert drift_finding.owner == "ML quality"
        assert drift_finding.investigation_steps
        assert drift_finding.debug_commands

        llm_finding = next(f for f in findings if f.fingerprint == "llm/error-rate")
        assert llm_finding.likely_cause
        assert any(item["label"] == "Top error" for item in llm_finding.evidence)

        stream_finding = next(f for f in findings if f.fingerprint == "stream/stalled")
        assert "live-ingest incident" in stream_finding.suggestion

    def test_tail_normalizes_legacy_records(self, tmp_path, monkeypatch):
        from road_safety.services import watchdog

        path = tmp_path / "watchdog.jsonl"
        path.write_text(json.dumps({
            "severity": "warning",
            "category": "drift",
            "title": "Zero feedback coverage across all events",
            "detail": "9 events in window have 0 labeled events.",
            "suggestion": "Enable event labeling pipeline.",
            "ts": "2026-04-15T20:58:36.257Z",
            "snapshot_id": "abc123",
        }) + "\n")
        monkeypatch.setattr(watchdog, "_WATCHDOG_PATH", path)

        records = watchdog.tail(10)
        assert len(records) == 1
        assert records[0]["fingerprint"] == "drift/feedback-coverage"
        assert records[0]["owner"] == "ML quality"
        assert records[0]["debug_commands"]

    def test_stats_group_repeated_incidents(self, tmp_path, monkeypatch):
        from road_safety.services import watchdog

        path = tmp_path / "watchdog.jsonl"
        lines = [
            {
                "severity": "warning",
                "category": "llm",
                "title": "LLM latency very high (12800ms p95)",
                "detail": "P95 latency is 12800ms.",
                "suggestion": "Reduce prompt size.",
                "ts": "2026-04-15T20:58:36.257Z",
                "snapshot_id": "snap1",
            },
            {
                "severity": "warning",
                "category": "llm",
                "title": "LLM latency very high (11900ms p95)",
                "detail": "P95 latency is 11900ms.",
                "suggestion": "Reduce prompt size.",
                "ts": "2026-04-15T20:59:36.257Z",
                "snapshot_id": "snap2",
            },
        ]
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        monkeypatch.setattr(watchdog, "_WATCHDOG_PATH", path)

        summary = watchdog.stats()
        assert summary["total_findings"] == 2
        assert summary["unique_incidents"] == 1
        assert summary["repeating_incidents"] == 1
        assert summary["top_incidents"][0]["count"] == 2
