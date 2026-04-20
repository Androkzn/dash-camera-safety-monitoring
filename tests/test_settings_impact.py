"""Tests for the impact engine's deterministic comparability and deltas."""

from __future__ import annotations

import time

import pytest

from road_safety.services import settings_db
from road_safety.services.impact import (
    ImpactMonitor,
    MIN_AFTER_EVENTS,
    MIN_BASELINE_EVENTS,
    SCENE_JSD_THRESHOLD,
    compute_window,
    evaluate_confidence,
    jensen_shannon_distance,
)


@pytest.fixture(autouse=True)
def _reset_db(tmp_path):
    settings_db._reset_for_tests(tmp_path / "settings.db")
    yield
    settings_db._reset_for_tests(None)


def _make_event(ts: float, *, risk: str = "low", scene: str = "urban", confidence: float = 0.7) -> dict:
    return {
        "timestamp_sec": ts,
        "risk": risk,
        "confidence": confidence,
        "ttc_sec": 1.2,
        "distance_m": 3.0,
        "scene_label": scene,
        "quality_state": "nominal",
    }


def test_jensen_shannon_basics() -> None:
    assert jensen_shannon_distance({"a": 1.0}, {"a": 1.0}) == pytest.approx(0.0, abs=1e-9)
    # Fully disjoint distributions have JSD = 1.0 (base-2 log).
    assert jensen_shannon_distance({"a": 1.0}, {"b": 1.0}) == pytest.approx(1.0, abs=1e-9)
    # Symmetry.
    p, q = {"a": 0.7, "b": 0.3}, {"a": 0.4, "b": 0.6}
    assert jensen_shannon_distance(p, q) == pytest.approx(
        jensen_shannon_distance(q, p), abs=1e-9
    )


def test_compute_window_aggregates_metrics() -> None:
    now = 1000.0
    events = [_make_event(now - 50 + i, risk="high" if i < 5 else "low") for i in range(40)]
    ws = compute_window(events, start_ts=now - 60, end_ts=now)
    assert ws.sample_size == 40
    # 40 events in 60 s = 40 per minute.
    assert ws.event_rate_per_min == pytest.approx(40.0, rel=0.01)
    assert ws.severity_counts["high"] == 5
    assert ws.severity_counts["low"] == 35
    assert ws.scene_distribution["urban"] == pytest.approx(1.0)


def test_evaluate_confidence_high_when_volumes_match_and_scene_stable() -> None:
    now = 1000.0
    events = [_make_event(now - 100 + i) for i in range(50)]
    baseline = compute_window(events, start_ts=now - 200, end_ts=now - 100)
    # Generate after-window with same scene mix and adequate sample.
    after_events = [_make_event(now + i) for i in range(50)]
    after = compute_window(after_events, start_ts=now, end_ts=now + 100)
    tier, reasons = evaluate_confidence(baseline, after)
    # Volumes are NOT in window, so sample sizes are 0 in baseline. Recompute
    # explicitly with proper offsets.
    base = compute_window(events, start_ts=now - 200, end_ts=now)
    aft = compute_window(after_events, start_ts=now, end_ts=now + 100)
    tier, reasons = evaluate_confidence(base, aft)
    assert tier == "high"
    assert reasons == []


def test_evaluate_confidence_caps_when_scene_drifts() -> None:
    now = 1000.0
    base_events = [_make_event(now - 50 + i, scene="urban") for i in range(MIN_BASELINE_EVENTS)]
    after_events = [_make_event(now + i, scene="highway") for i in range(MIN_AFTER_EVENTS)]
    base = compute_window(base_events, start_ts=now - 60, end_ts=now)
    after = compute_window(after_events, start_ts=now, end_ts=now + 60)
    tier, reasons = evaluate_confidence(base, after)
    assert "scene_mix_drift" in reasons
    assert tier in ("medium", "low")


def test_evaluate_confidence_low_when_under_volume() -> None:
    now = 1000.0
    base_events = [_make_event(now - 5 + i) for i in range(3)]
    base = compute_window(base_events, start_ts=now - 60, end_ts=now)
    after = compute_window([], start_ts=now, end_ts=now + 60)
    tier, reasons = evaluate_confidence(base, after)
    assert tier == "low"
    assert "insufficient_events" in reasons


def test_impact_monitor_persists_session_across_restart() -> None:
    """A new ImpactMonitor instance must rehydrate from settings.db."""
    events: list[dict] = []
    mon1 = ImpactMonitor(events_source=lambda: events)
    audit_id = mon1.on_settings_change(
        {"CONF_THRESHOLD": 0.5},
        {"CONF_THRESHOLD": 0.6},
        actor_label="op",
        changed_keys=["CONF_THRESHOLD"],
    )
    # Simulate a restart: drop the in-memory monitor and create a new one.
    mon2 = ImpactMonitor(events_source=lambda: events)
    report = mon2.current_report()
    assert report is not None
    assert report.audit_id == audit_id
    assert report.before["CONF_THRESHOLD"] == 0.5
    assert report.after["CONF_THRESHOLD"] == 0.6


def test_coalesce_preserves_last_good() -> None:
    events: list[dict] = []
    mon = ImpactMonitor(events_source=lambda: events)
    audit1 = mon.on_settings_change(
        {"CONF_THRESHOLD": 0.5},
        {"CONF_THRESHOLD": 0.55},
        actor_label="op",
        changed_keys=["CONF_THRESHOLD"],
    )
    # Coalesce: same session, _last_good still points at the FIRST before.
    audit2 = mon.on_settings_change(
        {"CONF_THRESHOLD": 0.55},
        {"CONF_THRESHOLD": 0.6},
        actor_label="op",
        changed_keys=["CONF_THRESHOLD"],
    )
    assert audit2 == audit1  # coalesced
    target = mon.revert_target()
    assert target is not None
    assert target["CONF_THRESHOLD"] == 0.5
