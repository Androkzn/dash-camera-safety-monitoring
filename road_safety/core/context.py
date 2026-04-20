"""Scene-context classification for adaptive risk thresholds.

Role in the pipeline
--------------------
Detection ('is there a car?') is scene-agnostic. Risk ('is that car dangerous
*now*?') is not. The same geometric conflict — a vehicle 8 meters ahead,
closing at 2 m/s — means one thing in a parking lot (routine) and a very
different thing on a highway (imminent collision). This module watches the
rolling rate of pedestrians and vehicles plus an optional ego-speed proxy,
decides whether we are in **urban**, **highway**, **parking**, or
**unknown** territory, and hands the hot-path in ``detection.py`` a fresh
set of TTC / distance thresholds that reflect the scene.

Why this module exists
----------------------
detection.py uses fixed risk thresholds (TTC_HIGH=1.5s, TTC_MED=3.0s,
DIST_HIGH=3.0m, DIST_MED=8.0m). Those numbers are defensible for city streets
at 25-35 mph, but they are *wrong* in two opposite directions:

  * Highway at 65 mph (~29 m/s): a 1.5s TTC leaves ~44m of stopping distance,
    which is below a loaded truck's minimum stop. Research (NHTSA FCW,
    MobilEye) puts the realistic highway FCW band at >=3s. A system that
    fires "high" only at 1.5s TTC on a highway is effectively telling the
    driver *after* the crash is unavoidable.

  * Parking lot at 3 mph (~1.3 m/s): a 1.5s TTC corresponds to an object
    within 2m — but at parking speeds that's a perfectly normal clearance
    when maneuvering around cars. Firing "high" here drowns the driver in
    false positives and teaches them to ignore the system.

Same risk *semantic* ("the driver needs to react now"), different numerical
thresholds. So we classify the scene (urban / highway / parking / unknown)
from cheap, always-available signals — detection-density rolling windows
and an optional ego-speed proxy — then hand back thresholds calibrated to
that scene. Callers use adaptive_thresholds() in place of the module-level
constants in detection.py.

This module is intentionally additive: it does not import from or modify
detection.py. It consumes any object with a `.cls` attribute (which
Detection satisfies) so it stays decoupled and testable.

Consumers
---------
- ``road_safety/core/detection.py`` via ``AdaptiveThresholds``.
- ``road_safety/server.py`` — logs the label with each event.

Python idioms used here (once-per-file):
- ``from __future__ import annotations`` — makes all type hints lazy strings
  so forward references (a class mentioning itself) work without quotes.
- ``@dataclass`` — decorator from the stdlib that auto-writes ``__init__``,
  ``__repr__`` and ``__eq__`` from the listed fields. Lets us declare plain
  value-object records with no boilerplate.
- ``collections.deque`` — double-ended queue optimized for O(1)
  append/popleft at the ends. We use it as a time-ordered ring buffer.
- ``tuple[float, str]`` — PEP 604 / 585 typing: a tuple with exactly two
  fields of the given types.
- ``float | None`` — union type ("either a float or None"). Same as
  ``Optional[float]``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Class sets we recognize
# ---------------------------------------------------------------------------
# Class sets — kept local so we don't depend on detection.py imports.
# A ``set`` literal gives O(1) membership checks, which matters because
# ``observe`` runs per detection per frame.
_PEDESTRIAN_CLASSES = {"person"}
_VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}

# ---------------------------------------------------------------------------
# Secondary window for the parking-lot rule
# ---------------------------------------------------------------------------
# Parking rule looks at a shorter tail than the main window, because a
# vehicle can cross a quiet residential street in <60s and we don't want
# that to be mistaken for "parking lot".
# 30 s was picked because it is the shortest window that still smooths the
# per-frame detection noise at 2 fps (60 frames).
_PARKING_WINDOW_SEC = 30.0

# ---------------------------------------------------------------------------
# Default thresholds — identical to detection.py so ``unknown`` is a no-op
# ---------------------------------------------------------------------------
# Default thresholds — these mirror detection.py's module constants so an
# "unknown" scene behaves exactly like today. Any change here must be
# mirrored in detection.py or the two will drift.
_DEFAULT_TTC_HIGH = 0.5
_DEFAULT_TTC_MED = 1.0
_DEFAULT_DIST_HIGH = 2.0
_DEFAULT_DIST_MED = 5.0


@dataclass
class SceneContext:
    """Result object from a single ``classify()`` call.

    Attributes
    ----------
    label: One of ``urban``, ``highway``, ``parking``, ``unknown``.
    confidence: Subjective 0..1 weighting; callers may down-weight overlays
        when confidence is low rather than treating the label as ground truth.
    speed_proxy_mps: Optional ego-speed reading if the caller supplied one.
    pedestrian_rate_per_min: Observed rate over the rolling window.
    vehicle_rate_per_min: Same for vehicles.
    reason: Short human-readable justification string suitable for logs and
        the dashboard (e.g. "pedestrian rate 5.4/min > 3").
    """

    label: str                     # "urban" | "highway" | "parking" | "unknown"
    confidence: float              # 0..1
    speed_proxy_mps: float | None  # from ego-flow if available
    pedestrian_rate_per_min: float # rolling 60s
    vehicle_rate_per_min: float    # rolling 60s
    reason: str                    # short human-readable justification


@dataclass
class AdaptiveThresholds:
    """TTC and pixel-distance cutoffs tailored to a scene label.

    Attributes
    ----------
    ttc_high_sec: Time-to-contact cutoff for HIGH-severity events.
    ttc_med_sec: Cutoff for MEDIUM severity; anything above is ignored.
    dist_high_m: Metric-distance cutoff for HIGH severity.
    dist_med_m: Cutoff for MEDIUM severity.
    """

    ttc_high_sec: float
    ttc_med_sec: float
    dist_high_m: float
    dist_med_m: float


class SceneContextClassifier:
    """Rolling scene classifier fed one frame's detections at a time.

    What it represents
    ------------------
    A rolling-window view of traffic density plus an optional speed proxy,
    compacted into a categorical scene label. One instance per camera.

    State it holds
    --------------
    - ``_events``: time-stamped (ts, class) pairs inside the rolling window.
    - ``_last_ts``: most recent frame timestamp — the reference point for
      all rate computations.
    - ``_last_speed``: most recent ego-speed proxy or ``None``.

    Lifecycle
    ---------
    1. ``__init__`` at boot.
    2. ``observe(detections, now_ts, speed_proxy_mps)`` every frame.
    3. ``classify()`` and ``adaptive_thresholds()`` called at emission time.

    Thread-model: single-threaded, one instance per camera/stream.
    Performance: O(k) per observe() where k is the prune count; fine at 2fps.
    """

    def __init__(self, window_sec: float = 60.0):
        """Initialise the classifier with an empty window.

        Args:
            window_sec: Length of the rolling density window. 60 s is the
                default because it smooths single-frame spikes while still
                reacting quickly when the vehicle pulls off the highway.
        """
        self._window_sec = window_sec
        # (ts, cls) tuples. deque for O(1) append; prune is linear from left.
        # ``deque[tuple[float, str]]`` is a typed deque of timestamp+class
        # pairs. Oldest entries live on the left, newest on the right.
        self._events: deque[tuple[float, str]] = deque()
        self._last_ts: float = 0.0
        self._last_speed: float | None = None

    def observe(
        self,
        detections: list,
        now_ts: float,
        speed_proxy_mps: float | None = None,
    ) -> None:
        """Call once per frame. Updates rolling detection-density windows.

        Args:
            detections: Iterable of detection-like objects (anything with a
                ``.cls`` string attribute). We specifically record only
                pedestrians and vehicles; signs, traffic-lights, etc. are
                ignored because they are not useful density signals.
            now_ts: Wall-clock seconds for this frame.
            speed_proxy_mps: Optional ego-speed proxy from ``EgoMotionEstimator``.
                ``None`` means "we couldn't estimate this frame"; ``classify``
                falls back to density-only rules in that case.

        Returns:
            None. Mutates ``_events``, ``_last_ts``, ``_last_speed``.
        """
        self._last_ts = now_ts
        self._last_speed = speed_proxy_mps

        for det in detections:
            # ``getattr(det, "cls", None)`` is Python's safe attribute read —
            # returns None if ``cls`` does not exist on ``det`` rather than
            # raising AttributeError. This keeps the classifier tolerant of
            # detector objects that do not expose a class string.
            cls = getattr(det, "cls", None)
            if cls in _PEDESTRIAN_CLASSES or cls in _VEHICLE_CLASSES:
                self._events.append((now_ts, cls))

        # Prune anything older than now - window_sec from the left.
        # ``popleft`` is O(1) on a deque, so this stays cheap even with a
        # burst of old entries aging out at once.
        cutoff = now_ts - self._window_sec
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _rates(self, window_sec: float) -> tuple[float, float]:
        """Return (pedestrian_per_min, vehicle_per_min) over the last window_sec.

        Intuition: scan newest-to-oldest, counting by class, and stop once we
        cross the cutoff. Dividing by ``window_min`` gives a rate that is
        comparable across different window sizes.

        Args:
            window_sec: Length of the look-back window in seconds. Must be
                > 0; zero or negative returns ``(0.0, 0.0)`` as a safety net
                so accidental division by zero can't happen.

        Returns:
            Tuple ``(pedestrians_per_min, vehicles_per_min)``.
        """
        if window_sec <= 0:
            return 0.0, 0.0
        cutoff = self._last_ts - window_sec
        peds = 0
        vehs = 0
        # Iterate from the right (newest first) and stop once we cross cutoff.
        # The deque is time-ordered so this is safe. ``reversed`` on a deque
        # is O(1) per step and avoids materializing a copy.
        for ts, cls in reversed(self._events):
            if ts < cutoff:
                break
            if cls in _PEDESTRIAN_CLASSES:
                peds += 1
            elif cls in _VEHICLE_CLASSES:
                vehs += 1
        window_min = window_sec / 60.0
        return peds / window_min, vehs / window_min

    def classify(self) -> SceneContext:
        """Evaluate the four classification rules in priority order.

        Rule order matters and is deliberate:
            1. highway — fastest to evaluate, strongest distinguishing signal.
            2. urban — dense pedestrians or dense vehicles at low/no speed.
            3. parking — near-empty scene over the short window.
            4. unknown — fallback; maps to default thresholds.

        Returns:
            A ``SceneContext`` with the label, confidence, raw rates, and a
            string ``reason`` suitable for logs and the dashboard.
        """
        ped_rate, veh_rate = self._rates(self._window_sec)
        speed = self._last_speed

        # Rule 1 — highway: fast and not many pedestrians.
        # 13 m/s ~ 29 mph. Above this, being in dense pedestrian traffic is
        # implausible; the very low ped-rate filter catches the edge case of
        # a highway that happens to run through a pedestrian bridge.
        if speed is not None and speed > 13 and ped_rate < 1:
            return SceneContext(
                label="highway",
                confidence=0.8,
                speed_proxy_mps=speed,
                pedestrian_rate_per_min=ped_rate,
                vehicle_rate_per_min=veh_rate,
                reason=f"speed {speed:.1f} m/s > 13 and pedestrian rate {ped_rate:.1f}/min < 1",
            )

        # Rule 2 — urban: dense pedestrians, or dense vehicles at low/unknown speed.
        # ``slow_or_unknown`` captures both the "we can see we're slow" and
        # the "ego-flow has no confidence this frame" cases — both point
        # away from highway and toward urban.
        slow_or_unknown = speed is None or speed < 8
        # ped_rate > 3/min = a pedestrian every 20 seconds on average. That
        # matches a city sidewalk; suburbia sits well below this.
        # veh_rate > 6/min = a vehicle every 10 s, which is urban arterial
        # density. Raising it would miss light-urban scenes; lowering it
        # catches busy highways.
        if ped_rate > 3 or (veh_rate > 6 and slow_or_unknown):
            return SceneContext(
                label="urban",
                confidence=0.7,
                speed_proxy_mps=speed,
                pedestrian_rate_per_min=ped_rate,
                vehicle_rate_per_min=veh_rate,
                reason=(
                    f"pedestrian rate {ped_rate:.1f}/min > 3"
                    if ped_rate > 3
                    else f"vehicle rate {veh_rate:.1f}/min > 6 at low/unknown speed"
                ),
            )

        # Rule 3 — parking: almost no traffic in the last 30s.
        # We use the short 30-s window here (not the 60-s window) because
        # parking lots often sit empty for sustained spans and we want to
        # react quickly once the stream goes quiet.
        ped30, veh30 = self._rates(_PARKING_WINDOW_SEC)
        if veh30 < 0.5 and ped30 < 0.5:
            return SceneContext(
                label="parking",
                confidence=0.6,
                speed_proxy_mps=speed,
                pedestrian_rate_per_min=ped_rate,
                vehicle_rate_per_min=veh_rate,
                reason=(
                    f"last 30s: vehicle {veh30:.2f}/min < 0.5 and "
                    f"pedestrian {ped30:.2f}/min < 0.5"
                ),
            )

        # Rule 4 — fallback. Confidence 0.3 explicitly marks this as weak.
        return SceneContext(
            label="unknown",
            confidence=0.3,
            speed_proxy_mps=speed,
            pedestrian_rate_per_min=ped_rate,
            vehicle_rate_per_min=veh_rate,
            reason="no rule matched; using default thresholds",
        )

    def adaptive_thresholds(self, ctx: SceneContext) -> AdaptiveThresholds:
        """Scene-calibrated replacements for detection.py's module constants.

        Calibrated for *observation/analytics* cameras, not in-vehicle FCW.
        Based on SSAM, SAFE-UP, and PET research:
        - PET > 1.0 s => low severity (no evasive action needed)
        - Only sub-second closing at converging angles is genuinely high-risk

        Args:
            ctx: The scene context previously returned by ``classify``.

        Returns:
            ``AdaptiveThresholds`` tuned for the context's label. For
            ``unknown`` we return the ``_DEFAULT_*`` constants so behavior
            is indistinguishable from pre-adaptive code.
        """
        if ctx.label == "highway":
            # 65 mph ~ 29 m/s — higher speed requires more reaction time.
            # TTC band widens because a truck cannot physically stop inside
            # 1.5 s at highway speed. Distance band widens similarly.
            return AdaptiveThresholds(
                ttc_high_sec=1.5, ttc_med_sec=3.0,
                dist_high_m=5.0, dist_med_m=15.0,
            )
        if ctx.label == "urban":
            # Urban observation cameras: short TTCs only because real
            # conflicts are close and fast. Wider thresholds here become
            # alert-fatigue machines.
            return AdaptiveThresholds(
                ttc_high_sec=0.4, ttc_med_sec=0.8,
                dist_high_m=1.5, dist_med_m=3.0,
            )
        if ctx.label == "parking":
            # Parking: tight distances because everything is close. Speed
            # is already low, so TTC cutoffs sit right around 0.5 s.
            return AdaptiveThresholds(
                ttc_high_sec=0.5, ttc_med_sec=1.0,
                dist_high_m=0.8, dist_med_m=2.5,
            )
        # unknown — identical to detection.py defaults.
        return AdaptiveThresholds(
            ttc_high_sec=_DEFAULT_TTC_HIGH, ttc_med_sec=_DEFAULT_TTC_MED,
            dist_high_m=_DEFAULT_DIST_HIGH, dist_med_m=_DEFAULT_DIST_MED,
        )
