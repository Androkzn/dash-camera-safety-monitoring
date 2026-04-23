"""Tests for road_safety.core.adaptive_fps.FpsController.

The controller's correctness is a state-transition problem: given a
speed/quality signal over time, the target_fps output should follow
a small set of invariants. We test those invariants directly with a
fake ``EgoFlow``-shaped signal so the tests run without OpenCV.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from road_safety.core.adaptive_fps import (
    FpsController,
    _BAND_HIGHWAY_ENTER_MPS,
    _BAND_URBAN_ENTER_MPS,
    _DWELL_SEC,
    _GPS_DIVERGENCE_MPS,
    _GPS_DIVERGENCE_SEC,
)


@dataclass
class FakeEgoFlow:
    """Minimal EgoFlow-shaped object — controller only reads two fields."""

    speed_proxy_mps: float
    confidence: float = 0.8


def _tick(
    ctrl: FpsController,
    speed: float,
    *,
    start_ts: float,
    seconds: float,
    dt: float = 0.2,
    quality_state: str = "nominal",
) -> float:
    """Push a steady speed signal into ``ctrl`` for ``seconds`` of wall time.

    Returns the final wall-clock timestamp so subsequent calls can chain.
    Also ticks ``should_process`` once per step so the internal deadline
    tracker sees a plausible caller cadence.
    """
    t = start_ts
    end = start_ts + seconds
    while t < end:
        ctrl.update(
            FakeEgoFlow(speed_proxy_mps=speed),
            {"state": quality_state},
            now_ts=t,
        )
        ctrl.should_process(t)
        t += dt
    return t


# ────────────────────────────────────────────────────────────────────
# Envelope / construction
# ────────────────────────────────────────────────────────────────────

class TestEnvelope:
    def test_rejects_non_positive_floor(self):
        with pytest.raises(ValueError):
            FpsController(floor_fps=0.0, ceil_fps=6.0, static_fps=2.0)

    def test_rejects_ceiling_below_floor(self):
        with pytest.raises(ValueError):
            FpsController(floor_fps=6.0, ceil_fps=3.0, static_fps=2.0)

    def test_rejects_non_positive_static(self):
        with pytest.raises(ValueError):
            FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=0.0)

    def test_set_envelope_updates_bands(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        c.set_envelope(floor_fps=4.0, ceil_fps=12.0)
        snap = c.snapshot()
        assert snap["floor_fps"] == 4.0
        assert snap["ceil_fps"] == 12.0


# ────────────────────────────────────────────────────────────────────
# Disabled = pass-through
# ────────────────────────────────────────────────────────────────────

class TestDisabled:
    def test_disabled_admits_every_frame(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0, enabled=False)
        # Push a parked signal — an enabled controller would floor.
        for i in range(20):
            c.update(FakeEgoFlow(0.0), {"state": "nominal"}, now_ts=float(i) * 0.1)
            assert c.should_process(float(i) * 0.1) is True

    def test_disabled_reports_static_rate(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0, enabled=False)
        assert c.current_target_fps() == 2.0

    def test_set_enabled_false_returns_to_static(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0, enabled=True)
        _tick(c, speed=0.0, start_ts=0.0, seconds=5.0)
        assert c.current_target_fps() == 3.0  # parked → floor
        c.set_enabled(False)
        assert c.current_target_fps() == 2.0  # fixed-mode rate

    def test_set_static_fps_updates_disabled_rate(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0, enabled=False)
        c.set_static_fps(4.5)
        assert c.current_target_fps() == 4.5
        with pytest.raises(ValueError):
            c.set_static_fps(0.0)


# ────────────────────────────────────────────────────────────────────
# Band transitions (the core policy table)
# ────────────────────────────────────────────────────────────────────

class TestBandTransitions:
    def test_starts_in_parked_band(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        assert c.snapshot()["band"] == "parked"
        assert c.current_target_fps() == 3.0

    def test_parked_to_urban_on_rising_speed(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Hold well above the urban-enter threshold long enough to
        # satisfy dwell + EMA warmup.
        _tick(c, speed=_BAND_URBAN_ENTER_MPS + 2.0, start_ts=0.0, seconds=6.0)
        assert c.snapshot()["band"] == "urban"

    def test_urban_to_highway_on_rising_speed(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Ramp: first urban, then highway.
        t = _tick(c, speed=_BAND_URBAN_ENTER_MPS + 2.0, start_ts=0.0, seconds=6.0)
        assert c.snapshot()["band"] == "urban"
        _tick(c, speed=_BAND_HIGHWAY_ENTER_MPS + 3.0, start_ts=t, seconds=6.0)
        assert c.snapshot()["band"] == "highway"
        assert c.current_target_fps() == 6.0

    def test_highway_down_to_parked_on_stop(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        t = _tick(c, speed=_BAND_HIGHWAY_ENTER_MPS + 5.0, start_ts=0.0, seconds=10.0)
        assert c.snapshot()["band"] == "highway"
        _tick(c, speed=0.0, start_ts=t, seconds=10.0)
        assert c.snapshot()["band"] == "parked"


# ────────────────────────────────────────────────────────────────────
# Hysteresis: dead-band + dwell time
# ────────────────────────────────────────────────────────────────────

class TestHysteresis:
    def test_no_immediate_flap_at_band_boundary(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Sit just above the urban-enter boundary briefly — less than
        # dwell time — and assert we don't get pulled back down the
        # moment speed dips a hair below.
        t = _tick(c, speed=_BAND_URBAN_ENTER_MPS + 1.5, start_ts=0.0, seconds=4.0)
        assert c.snapshot()["band"] == "urban"
        # Drop to just below enter but above exit threshold.
        # Urban exit is at 0.8 mps — feed 1.0 mps, still above exit.
        _tick(c, speed=1.0, start_ts=t, seconds=3.0)
        assert c.snapshot()["band"] == "urban"

    def test_dwell_prevents_rapid_ping_pong(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Enter urban.
        t = _tick(c, speed=_BAND_URBAN_ENTER_MPS + 2.0, start_ts=0.0, seconds=5.0)
        assert c.snapshot()["band"] == "urban"
        # Immediately drop to zero — but only for less than dwell time.
        # Band must hold while dwell is in effect.
        short = _DWELL_SEC * 0.5
        _tick(c, speed=0.0, start_ts=t, seconds=short)
        assert c.snapshot()["band"] == "urban"


# ────────────────────────────────────────────────────────────────────
# Quality degradation clamps to floor
# ────────────────────────────────────────────────────────────────────

class TestQualityDegradation:
    def test_degraded_quality_clamps_to_floor_at_highway_speed(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Drive into highway first so the band is committed.
        t = _tick(
            c,
            speed=_BAND_HIGHWAY_ENTER_MPS + 5.0,
            start_ts=0.0,
            seconds=8.0,
        )
        assert c.current_target_fps() == 6.0
        # Flip quality to degraded — target should drop to floor even
        # though we're still in the highway band.
        c.update(
            FakeEgoFlow(_BAND_HIGHWAY_ENTER_MPS + 5.0),
            {"state": "degraded"},
            now_ts=t,
        )
        assert c.current_target_fps() == 3.0


# ────────────────────────────────────────────────────────────────────
# should_process deadline tracking
# ────────────────────────────────────────────────────────────────────

class TestShouldProcess:
    def test_admits_first_frame(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        assert c.should_process(0.0) is True

    def test_rate_limits_to_target_fps(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Current target is floor (3.0 fps) → interval 0.333s.
        assert c.should_process(0.0) is True
        # Try to admit 5ms later — must refuse.
        assert c.should_process(0.005) is False
        # Try to admit after the full interval — must admit.
        assert c.should_process(0.35) is True

    def test_average_rate_matches_target(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Simulate 2 seconds of 6 fps capture at the floor (3 fps target).
        admitted = 0
        dt = 1.0 / 6.0
        for i in range(12):
            t = i * dt
            # Reset EMA each iter so we stay at floor.
            c.update(FakeEgoFlow(0.0), {"state": "nominal"}, now_ts=t)
            if c.should_process(t):
                admitted += 1
        # 2 seconds @ 3 fps = 6 admits. Allow ±1 for deadline slack.
        assert 5 <= admitted <= 7


# ────────────────────────────────────────────────────────────────────
# Ego confidence gating: low-confidence flow is ignored
# ────────────────────────────────────────────────────────────────────

class TestLowConfidenceEgo:
    def test_low_confidence_readings_do_not_promote_band(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Push high-speed readings but with confidence below threshold —
        # the controller should treat them as missing signal and hold
        # at the parked band.
        for i in range(40):
            c.update(
                FakeEgoFlow(speed_proxy_mps=20.0, confidence=0.1),
                {"state": "nominal"},
                now_ts=float(i) * 0.2,
            )
        assert c.snapshot()["band"] == "parked"


# ────────────────────────────────────────────────────────────────────
# Hybrid GPS cross-check
# ────────────────────────────────────────────────────────────────────

class TestHybridGps:
    def test_gps_only_does_not_crash_without_ego(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        c.set_gps_speed(10.0, now_ts=0.0)
        c.update(None, {"state": "nominal"}, now_ts=0.0)
        # With no ego signal and fresh GPS, the controller should still
        # be able to generate a snapshot and make a decision.
        snap = c.snapshot()
        assert snap["gps_mps"] == 10.0

    def test_diverging_gps_wins_after_streak(self):
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Ego says stopped, GPS says highway speed. After the divergence
        # grace period the effective signal should follow GPS, not ego.
        start = 0.0
        gps_speed = _BAND_HIGHWAY_ENTER_MPS + 5.0
        # Feed the divergence for ≥ grace period + dwell so the transition
        # has time to propagate through the EMA.
        t = start
        duration = _GPS_DIVERGENCE_SEC + _DWELL_SEC + 4.0
        while t < start + duration:
            c.set_gps_speed(gps_speed, now_ts=t)
            c.update(
                FakeEgoFlow(speed_proxy_mps=0.0),
                {"state": "nominal"},
                now_ts=t,
            )
            t += 0.2
        assert c.snapshot()["band"] == "highway"

    def test_gps_always_wins_when_fresh(self):
        """Fresh GPS is the calibrated signal and wins outright over ego.

        Regression guard: previously a small GPS/ego disagreement
        (< 5 m/s) stuck with ego, which meant a slow-moving vehicle
        whose ego proxy under-read would stay stuck in the parked band
        (exactly the screenshot-bug the user hit: 10 km/h GPS, parked
        tiles). The contract is now "GPS wins when fresh" — full stop.
        """
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Ego stuck at zero, GPS just above urban-enter.
        t = 0.0
        gps_speed = _BAND_URBAN_ENTER_MPS + 1.5
        while t < 8.0:
            c.set_gps_speed(gps_speed, now_ts=t)
            c.update(
                FakeEgoFlow(speed_proxy_mps=0.0),
                {"state": "nominal"},
                now_ts=t,
            )
            t += 0.2
        assert c.snapshot()["band"] == "urban"

    def test_stale_gps_falls_back_to_ego(self):
        """If GPS hasn't been published recently, ego takes over."""
        c = FpsController(floor_fps=3.0, ceil_fps=6.0, static_fps=2.0)
        # Seed GPS once then never refresh — it should go stale after 2s.
        c.set_gps_speed(_BAND_URBAN_ENTER_MPS + 2.0, now_ts=0.0)
        # Feed ~10s of zero-ego with an unfresh GPS value.
        t = 0.2
        while t < 10.0:
            c.update(
                FakeEgoFlow(speed_proxy_mps=0.0),
                {"state": "nominal"},
                now_ts=t,
            )
            t += 0.2
        # After the 2s freshness window, the stale GPS is ignored and
        # the controller should fall back to ego (0) → parked.
        assert c.snapshot()["band"] == "parked"
