"""Adaptive per-frame FPS controller.

Role in the pipeline
--------------------
Decides, once per captured frame, whether the perception loop should run the
expensive gates (YOLO + ByteTrack + TTC + context) or skip them. Keeps the
StreamReader running at a fixed ceiling and lets the controller drop frames
*after* capture but *before* detection, based on ego-speed estimated from
optical flow (``EgoFlow.speed_proxy_mps``) with an optional GPS cross-check.

Why not dynamically retune the StreamReader
-------------------------------------------
StreamReader bakes ``step = native_fps / target_fps`` at construction time
[``road_safety/core/stream.py``]. Changing its rate requires stopping and
restarting the capture thread, which drops buffered frames, interrupts the
MJPEG stream, and resets the playback position. That is not acceptable on
a per-second cadence. A cheap pre-detection skip gate has no such cost.

Hard constraints that shape the policy
--------------------------------------
* Multi-gate TTC needs ≥4 samples spanning ≥1.5s. That pins the process-rate
  floor at ≥2.67 fps. ``FPS_FLOOR`` default is 3.0 with that headroom.
* Ego-motion runs *before* the gate in the pipeline, so the speed signal
  itself is never starved by the gate's decisions — if we drop to the floor,
  we still get ego-speed updates at the floor rate.
* Quality degradation (night / rain / glare / dirty lens) clamps to the
  floor regardless of speed: running at highway rate on an unreadable
  camera burns CPU for nothing.

Hysteresis
----------
A naive "speed → fps" map oscillates at band boundaries. We add:
* An EMA on the raw speed reading (τ ≈ 1.5s) so sensor jitter doesn't
  translate directly into rate changes.
* Per-band deadzones: entering a faster band requires crossing its
  threshold by ``_ENTER_MARGIN``; falling back requires dropping by
  ``_EXIT_MARGIN``.
* A minimum dwell time per band (``_DWELL_SEC``): once committed to a
  band the controller will not switch for at least this long.

Hybrid GPS
----------
``set_gps_speed(mps)`` is optional — callers that have telematics can
publish a GPS reading and the controller will cross-check it against the
ego proxy. If the two disagree by more than ``_GPS_DIVERGENCE_MPS`` for
``_GPS_DIVERGENCE_SEC`` it logs a warning and prefers GPS; otherwise it
sticks with the ego proxy so fleets without GPS get identical behaviour.

Thread model
------------
One controller per ``StreamSlot``. All methods are called from the single
capture-thread callback (``_on_frame``) — no locking needed. The public
surface is intentionally small (``update`` / ``should_process`` /
``current_target_fps`` / ``set_gps_speed``) so future policies can be
swapped in without changing callers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# Band boundaries (m/s of ego-speed). The controller picks the highest band
# whose enter-threshold the smoothed speed has crossed. Calibrated to
# coincide with SceneContextClassifier regimes: parking < urban < highway.
_BAND_URBAN_ENTER_MPS = 1.5       # ~5 km/h — rolling, not parked
_BAND_HIGHWAY_ENTER_MPS = 10.0    # ~36 km/h — arterial / freeway onramp

# Hysteresis margins. Exit thresholds sit below enter thresholds so a
# vehicle hovering around a boundary doesn't flap between bands.
_BAND_URBAN_EXIT_MPS = 0.8
_BAND_HIGHWAY_EXIT_MPS = 8.0

# Minimum seconds to stay in a band before another transition is allowed.
# Prevents rapid oscillation during stop-and-go traffic.
_DWELL_SEC = 2.0

# EMA time constant for the speed signal. 1.5s smooths tracker jitter
# without masking real accel/decel events that matter (>1s duration).
_SPEED_EMA_TAU_SEC = 1.5

# Ego-flow confidence floor. Below this the speed_proxy_mps is unreliable
# (rain, wipers, low texture) and we freeze the current band rather than
# letting a noisy reading trigger transitions.
_MIN_EGO_CONFIDENCE = 0.3

# GPS divergence alarm. When GPS is supplied and disagrees with the ego
# proxy by more than this many m/s for _GPS_DIVERGENCE_SEC seconds, we
# prefer GPS and log a warning so the operator can investigate.
_GPS_DIVERGENCE_MPS = 5.0
_GPS_DIVERGENCE_SEC = 3.0


@dataclass
class FpsBand:
    """One row in the speed → target-fps policy table."""

    name: str
    enter_mps: float
    exit_mps: float
    target_fps: float


class FpsController:
    """Per-slot adaptive FPS policy.

    Usage (inside the per-slot frame callback)::

        controller.update(ego_flow, quality_state, now_ts=wall_ts)
        if not controller.should_process(wall_ts):
            return  # skip detection; ego + quality still ran above

    The controller is deliberately inert when ``enabled`` is False — it
    returns ``True`` from ``should_process`` so the legacy fixed-rate
    behaviour is preserved under the feature flag.
    """

    def __init__(
        self,
        *,
        floor_fps: float,
        ceil_fps: float,
        static_fps: float,
        enabled: bool = True,
    ) -> None:
        """Construct with the operating envelope.

        Args:
            floor_fps: Minimum process rate. Must be high enough that the
                TTC gate's ≥4-samples-in-≥1.5s window remains satisfiable.
            ceil_fps: Maximum process rate. Should equal the StreamReader's
                capture rate when adaptive is on — the controller cannot
                admit frames the reader never captures.
            static_fps: The rate the StreamReader captures at when adaptive
                is OFF — ``TARGET_FPS`` today. Reported by ``snapshot()``
                when disabled so the UI doesn't mislead the operator with
                the ceiling constant.
            enabled: When False the controller is a no-op — ``should_process``
                always returns True and ``current_target_fps`` returns
                ``static_fps``. Flip via the settings store on a policy change.
        """
        if floor_fps <= 0:
            raise ValueError("floor_fps must be > 0")
        if ceil_fps < floor_fps:
            raise ValueError("ceil_fps must be >= floor_fps")
        if static_fps <= 0:
            raise ValueError("static_fps must be > 0")
        self._floor = float(floor_fps)
        self._ceil = float(ceil_fps)
        self._static = float(static_fps)
        self._enabled = bool(enabled)

        # Policy table. Three bands; thresholds are SI units (m/s). Each
        # band's ``target_fps`` is clamped to [floor, ceil] at read time so
        # an operator who sets floor=5 still gets a sensible envelope
        # even if it collapses the lower bands.
        self._bands: tuple[FpsBand, ...] = (
            FpsBand("parked", 0.0, 0.0, self._floor),
            FpsBand("urban", _BAND_URBAN_ENTER_MPS, _BAND_URBAN_EXIT_MPS, self._mid_fps()),
            FpsBand(
                "highway",
                _BAND_HIGHWAY_ENTER_MPS,
                _BAND_HIGHWAY_EXIT_MPS,
                self._ceil,
            ),
        )

        self._current_band: FpsBand = self._bands[0]
        self._band_entered_at: float | None = None

        self._ema_speed_mps: float | None = None
        self._last_update_ts: float | None = None

        self._gps_mps: float | None = None
        self._gps_ts: float | None = None
        self._gps_divergence_since: float | None = None

        self._last_process_ts: float | None = None
        self._quality_degraded: bool = False

    # ------------------------------------------------------------------
    # Configuration knobs (warm-reloadable from the settings store).
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the controller at runtime.

        Disabling parks the controller (it returns the static rate).
        Enabling starts from the parked band and lets the speed signal
        drive transitions.
        """
        self._enabled = bool(enabled)
        self._current_band = self._bands[0]
        self._band_entered_at = None
        self._last_process_ts = None

    def set_static_fps(self, fps: float) -> None:
        """Update the fixed-mode rate reported when the controller is off.

        Called when the settings store publishes a new ``TARGET_FPS`` so
        the snapshot stays truthful after a warm reload.
        """
        if fps <= 0:
            raise ValueError("static_fps must be > 0")
        self._static = float(fps)

    def set_envelope(self, *, floor_fps: float, ceil_fps: float) -> None:
        """Update the floor/ceiling live — e.g. from the settings store.

        Preserves the current band selection but recomputes each band's
        ``target_fps`` against the new envelope so the effective rate
        tracks the new policy without a transition.
        """
        if floor_fps <= 0 or ceil_fps < floor_fps:
            raise ValueError("invalid FPS envelope")
        self._floor = float(floor_fps)
        self._ceil = float(ceil_fps)
        self._bands = (
            FpsBand("parked", 0.0, 0.0, self._floor),
            FpsBand("urban", _BAND_URBAN_ENTER_MPS, _BAND_URBAN_EXIT_MPS, self._mid_fps()),
            FpsBand(
                "highway",
                _BAND_HIGHWAY_ENTER_MPS,
                _BAND_HIGHWAY_EXIT_MPS,
                self._ceil,
            ),
        )
        # Re-pin current_band by name so the new FpsBand is used.
        for b in self._bands:
            if b.name == self._current_band.name:
                self._current_band = b
                break

    # ------------------------------------------------------------------
    # Signals in.
    # ------------------------------------------------------------------
    def set_gps_speed(self, mps: float | None, *, now_ts: float | None = None) -> None:
        """Publish an optional GPS reading for hybrid cross-check.

        ``None`` clears any previously-set reading. The controller does
        not fail if GPS is never supplied — vehicles without telematics
        behave identically to the pure-ego path.
        """
        self._gps_ts = now_ts if mps is not None else None
        self._gps_mps = float(mps) if mps is not None else None

    def update(
        self,
        ego_flow: Any | None,
        quality_state: dict | None = None,
        *,
        now_ts: float,
    ) -> None:
        """Fold this frame's ego-motion + quality signals into the policy.

        Must be called *every* captured frame — the EMA on speed assumes
        regular updates at capture rate, and quality degradation has to
        be seen at capture rate to react in time. Safe to call with
        ``ego_flow=None`` (first frame or failed optical flow).
        """
        # Quality signal: any non-nominal state clamps to floor.
        # ``None`` (no monitor) is treated as nominal so disabling the
        # quality subsystem doesn't silently disable adaptive FPS.
        if quality_state is not None:
            qstate = quality_state.get("state") or "nominal"
            self._quality_degraded = qstate != "nominal"
        else:
            self._quality_degraded = False

        raw_speed = self._extract_speed(ego_flow)
        self._update_gps_divergence(raw_speed, now_ts)
        effective_speed = self._pick_effective_speed(raw_speed, now_ts)

        if effective_speed is None:
            # Signal too noisy or missing — hold the current band but keep
            # the EMA paused so it doesn't decay toward zero.
            self._last_update_ts = now_ts
            return

        self._ema_speed_mps = self._ema_step(
            prev=self._ema_speed_mps,
            sample=effective_speed,
            now_ts=now_ts,
        )
        self._last_update_ts = now_ts

        # Evaluate band transition.
        self._maybe_transition(now_ts)

    # ------------------------------------------------------------------
    # Policy decision.
    # ------------------------------------------------------------------
    def current_target_fps(self) -> float:
        """Target process rate right now.

        When disabled, returns the static (fixed-mode) rate — the
        StreamReader is capturing at that rate and the gate is a
        pass-through, so this is the honest number. Under quality
        degradation it clamps to floor. Otherwise returns the current
        band's target.
        """
        if not self._enabled:
            return self._static
        if self._quality_degraded:
            return self._floor
        return self._current_band.target_fps

    def should_process(self, now_ts: float) -> bool:
        """Gate: admit this frame for detection, or skip it.

        Uses a simple deadline: if ``now_ts`` is at or past the previous
        process timestamp plus ``1/target``, admit. Otherwise skip. This
        yields an average rate close to ``current_target_fps`` regardless
        of the capture rate (as long as capture >= target).
        """
        if not self._enabled:
            return True
        target = self.current_target_fps()
        if target <= 0:
            return False
        if self._last_process_ts is None:
            self._last_process_ts = now_ts
            return True
        interval = 1.0 / target
        if (now_ts - self._last_process_ts) + 1e-6 >= interval:
            self._last_process_ts = now_ts
            return True
        return False

    # ------------------------------------------------------------------
    # Diagnostics for status endpoints / ops sampler.
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """Opaque diagnostic payload for ``/api/live/sources`` etc.

        Keys are intentionally stable so the UI can chart them:
        ``target_fps_active``, ``band``, ``smoothed_speed_mps``,
        ``quality_degraded``, ``enabled``, ``floor_fps``, ``ceil_fps``,
        ``gps_mps``.
        """
        return {
            "enabled": self._enabled,
            "target_fps_active": round(self.current_target_fps(), 2),
            "band": self._current_band.name,
            "smoothed_speed_mps": (
                round(self._ema_speed_mps, 2)
                if self._ema_speed_mps is not None
                else None
            ),
            "quality_degraded": self._quality_degraded,
            "floor_fps": round(self._floor, 2),
            "ceil_fps": round(self._ceil, 2),
            "gps_mps": (
                round(self._gps_mps, 2) if self._gps_mps is not None else None
            ),
        }

    # ------------------------------------------------------------------
    # Internals.
    # ------------------------------------------------------------------
    def _mid_fps(self) -> float:
        """Urban-band target: roughly the midpoint, snapped to 0.5 fps."""
        raw = 0.5 * (self._floor + self._ceil)
        snapped = round(raw * 2.0) / 2.0
        return max(self._floor, min(self._ceil, snapped))

    def _extract_speed(self, ego_flow: Any | None) -> float | None:
        """Pull the unsigned speed proxy from an ``EgoFlow``-shaped object.

        Accepts anything with ``speed_proxy_mps`` + ``confidence``
        attributes to avoid a hard import dependency on the dataclass.
        Returns ``None`` when signal is missing or unreliable.
        """
        if ego_flow is None:
            return None
        try:
            conf = float(getattr(ego_flow, "confidence", 0.0))
            speed = float(getattr(ego_flow, "speed_proxy_mps", 0.0))
        except (TypeError, ValueError):
            return None
        if conf < _MIN_EGO_CONFIDENCE:
            return None
        return abs(speed)

    def _update_gps_divergence(
        self, raw_ego_speed: float | None, now_ts: float
    ) -> None:
        """Track how long ego + GPS have been disagreeing; log once per streak."""
        if self._gps_mps is None or raw_ego_speed is None:
            self._gps_divergence_since = None
            return
        delta = abs(self._gps_mps - raw_ego_speed)
        if delta < _GPS_DIVERGENCE_MPS:
            self._gps_divergence_since = None
            return
        if self._gps_divergence_since is None:
            self._gps_divergence_since = now_ts
            return
        streak = now_ts - self._gps_divergence_since
        if streak >= _GPS_DIVERGENCE_SEC:
            log.warning(
                "adaptive_fps: gps/ego speed divergence %.1f m/s for %.1fs "
                "(gps=%.1f ego=%.1f) — preferring gps",
                delta, streak, self._gps_mps, raw_ego_speed,
            )
            # Reset the streak so we don't spam the log every frame.
            self._gps_divergence_since = now_ts + 30.0

    def _pick_effective_speed(
        self, raw_ego_speed: float | None, now_ts: float
    ) -> float | None:
        """Hybrid rule: prefer GPS when fresh, otherwise fall back to ego.

        The ego proxy is described in ``egomotion.py`` as "coarse ... not
        defensible for quantitative claims" — it's good enough for scene
        classification but routinely under-reads on low-texture footage
        and is hardcoded to 0.0 for side-mounted cameras. GPS is
        calibrated m/s, so when it's available it wins outright. The
        divergence streak is still tracked for diagnostics (see
        ``_update_gps_divergence``) but no longer gates signal selection.
        """
        gps_fresh = (
            self._gps_mps is not None
            and self._gps_ts is not None
            and (now_ts - self._gps_ts) <= 2.0
        )
        if gps_fresh:
            return self._gps_mps
        if raw_ego_speed is not None:
            return raw_ego_speed
        return None

    def _ema_step(
        self, *, prev: float | None, sample: float, now_ts: float
    ) -> float:
        """One EMA step with a time-constant-based weight.

        ``alpha = dt / (tau + dt)`` yields a first-order low-pass with
        effective time constant ``_SPEED_EMA_TAU_SEC`` regardless of
        sample spacing, which matters because the capture cadence is not
        perfectly regular.
        """
        if prev is None or self._last_update_ts is None:
            return sample
        dt = max(0.0, now_ts - self._last_update_ts)
        if dt <= 0.0:
            return prev
        alpha = dt / (_SPEED_EMA_TAU_SEC + dt)
        return (1.0 - alpha) * prev + alpha * sample

    def _maybe_transition(self, now_ts: float) -> None:
        """Evaluate whether the smoothed speed warrants a band change."""
        speed = self._ema_speed_mps
        if speed is None:
            return
        # Respect dwell time after a recent transition.
        if (
            self._band_entered_at is not None
            and (now_ts - self._band_entered_at) < _DWELL_SEC
        ):
            return
        new_band = self._select_band(speed)
        if new_band is self._current_band:
            return
        log.debug(
            "adaptive_fps: band %s -> %s at speed=%.2f m/s target_fps=%.1f",
            self._current_band.name, new_band.name, speed, new_band.target_fps,
        )
        self._current_band = new_band
        self._band_entered_at = now_ts

    def _select_band(self, speed_mps: float) -> FpsBand:
        """Pick the correct band under hysteresis.

        Rising: cross a band's ``enter_mps`` from below → move up.
        Falling: drop below the current band's ``exit_mps`` → move down
        to the highest band still valid under a rising evaluation.
        """
        parked, urban, highway = self._bands
        current = self._current_band

        # Check for an upward transition first — the policy biases toward
        # catching acceleration events quickly.
        if current is parked and speed_mps >= urban.enter_mps:
            if speed_mps >= highway.enter_mps:
                return highway
            return urban
        if current is urban and speed_mps >= highway.enter_mps:
            return highway

        # Downward.
        if current is highway and speed_mps < highway.exit_mps:
            if speed_mps < urban.exit_mps:
                return parked
            return urban
        if current is urban and speed_mps < urban.exit_mps:
            return parked

        return current
