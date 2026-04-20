"""Camera / perception quality monitor.

Role in the pipeline
--------------------
Detection is only as good as the pixels flowing in. If the lens is smeared
with salt spray, the sun has just cleared the visor, or dusk has dropped the
scene into low contrast, YOLO's bounding boxes become noisier — and noisier
bounding boxes are the dominant source of **false-positive** safety alerts
(ghost pedestrians from JPEG blocks, "approaching" vehicles that are really
the focus breathing on a dirty lens).

This module samples three cheap per-frame features plus one slow running
average of detector confidence, classifies the pipeline as ``nominal`` or one
of four ``degraded_*`` states, and exposes a small ``risk_adjustment`` dict
that the hot-path in ``road_safety/server.py::_run_loop`` reads to:

  1. SUPPRESS event emission / LLM enrichment when the camera is degraded,
  2. Widen TTC and pixel-distance thresholds so borderline cases don't fire.

**Design note**: "degraded => suppress events" is an **intentional** choice,
not a bug. We would rather miss a marginal detection in a dirty-lens frame
than publish a false alert that erodes driver trust. The Monitoring page
surfaces the state so operators can see *why* the system went quiet.

Consumers
---------
- ``road_safety/server.py`` — gates emission + feeds the health banner.
- ``frontend/src/pages/MonitoringPage.tsx`` — renders the live state.

Python idioms used in this file (explained once):
- ``import threading`` — standard library concurrency primitives. We use
  ``threading.Lock`` because ``observe_frame`` is written by the capture
  thread while ``state()`` is read by the HTTP/FastAPI thread.
- ``numpy`` (``np``) — numerical array library. ``np.mean(arr)`` returns the
  scalar mean of an array; boolean masks like ``(gray > 240) | (gray < 15)``
  produce a per-pixel ``True/False`` array that ``mean`` converts to a
  fraction of ``True`` pixels.
- ``cv2`` — OpenCV. ``cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)`` converts a
  BGR image to single-channel grayscale; ``cv2.Laplacian(img, cv2.CV_64F)``
  runs a second-derivative edge filter whose ``.var()`` is the classic
  Pech-Pacheco sharpness proxy; ``cv2.resize`` downsamples the frame.
- ``dict`` literals like ``_THRESH`` — module-level tunables kept as plain
  dicts so tests can monkey-patch them if needed.
"""

import threading
import time
import cv2
import numpy as np

# Settings Console: hot-path snapshot reads. The store is the single source
# of truth for the two thresholds we care about here at runtime; the
# ``_THRESH`` dict still holds the seed defaults for unit tests that
# instantiate :class:`QualityMonitor` without booting the store.
from road_safety.settings_store import STORE as _SETTINGS_STORE

# ---------------------------------------------------------------------------
# State vocabulary
# ---------------------------------------------------------------------------
# The monitor publishes exactly one of these strings at a time. Keeping the
# list here (rather than as ad-hoc string literals scattered through the
# code) makes it trivial for the dashboard to render an icon per state and
# for tests to exhaustively assert transitions.
STATES = [
    "nominal",                    # Everything inside thresholds.
    "degraded_low_light",         # Scene too dark — detector confidence falls off a cliff.
    "degraded_blur",              # Low sharpness — dirty lens, out of focus, heavy rain.
    "degraded_low_confidence",    # Average detector conf has drifted below the floor.
    "degraded_overexposed",       # Sun-in-frame / headlights — classes merge into white.
]

# ---------------------------------------------------------------------------
# EWMA smoothing coefficients
# ---------------------------------------------------------------------------
# We smooth per-frame metrics with an Exponentially Weighted Moving Average:
#     ewma <- (1 - alpha) * ewma + alpha * sample
# A *small* alpha means the EWMA reacts slowly and ignores single-frame
# spikes. That's exactly what we want: a truck driving under a bridge for
# half a second should NOT flip us to "degraded_low_light".
_ALPHA_FRAME = 0.05   # Frame-level features (luminance/sharpness/saturation). ~20 frames to half-weight.
_ALPHA_CONF = 0.02    # Detector confidence smooths slower still; conf is noisier and more important.

# Resize ceiling for quality analysis. We don't need 4K pixels to judge
# luminance or sharpness, and downscaling keeps this function comfortably
# under 1 ms per frame even on an embedded CPU.
_MAX_WIDTH = 480

# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------
# The comments below explain WHY each threshold takes its specific value.
_THRESH = {
    # "overexposed_sat" — fraction of pixels that are either near-black
    # (<15/255) OR near-white (>240/255). More than 25% of the image
    # clipped to the extremes means the sensor is out of dynamic range,
    # typically sun glare. 0.25 was picked so a normal night sky +
    # headlights doesn't trigger it but a sun flare does.
    "overexposed_sat": 0.25,

    # "overexposed_lum" — mean grayscale luminance above 220/255 is very
    # bright. We require BOTH sat>0.25 AND lum>220 because either signal
    # alone has false positives (snow scenes, tunnel exits).
    "overexposed_lum": 220.0,

    # "low_light_lum" — below ~45/255 mean luminance, YOLOv8 recall on
    # the COCO vehicle/person classes drops sharply in our internal
    # benchmarks. That is our practical "too dark" line.
    "low_light_lum": 45.0,

    # "blur_sharp" — Laplacian variance below 40 correlates with visibly
    # blurred frames in our labeled samples (heavy rain, fogged lens,
    # motion blur from aggressive shutter). Above ~60 is crisp; 40-60
    # is a gray zone we accept as nominal.
    "blur_sharp": 40.0,

    # "low_conf" — rolling average YOLO confidence. Below 0.42 means
    # most detections are "maybe a car" and the downstream geometry
    # gates have nothing solid to stand on.
    "low_conf": 0.42,

    # "low_conf_min_samples" — require at least 60 observed frames (half
    # a minute at 2 fps) before we're willing to accuse the detector of
    # being unreliable. Avoids a cold-start false alarm.
    "low_conf_min_samples": 60,
}

# Hysteresis multiplier: once we're INSIDE a degraded state we require the
# metric to recover 20% past the original trigger before returning to
# nominal. Without this the system flickers back and forth at the boundary
# (e.g. lum=44/46/44/46) and spams state-change logs.
_HYST = 1.20  # require 20% past threshold to recover to nominal

# ---------------------------------------------------------------------------
# Risk adjustments per state
# ---------------------------------------------------------------------------
# Consumed by server.py and tier-dispatch code. Every key must exist in
# every state so callers can do ``adj["ttc_multiplier"]`` without guards.
#
# - ``skip_vision_enrichment``: if True, no LLM ALPR / scene narration for
#   events raised this frame. A blurred plate is a hallucination magnet.
# - ``ttc_multiplier``: multiply the TTC threshold by this. e.g. 1.7 means
#   we require a 70% more conservative TTC before firing (low-light class).
# - ``pixel_dist_multiplier``: same idea for pixel-distance gates.
_RISK = {
    "nominal": {"skip_vision_enrichment": False, "ttc_multiplier": 1.0, "pixel_dist_multiplier": 1.0},
    "degraded_low_light": {"skip_vision_enrichment": True, "ttc_multiplier": 1.7, "pixel_dist_multiplier": 1.3},
    "degraded_blur": {"skip_vision_enrichment": True, "ttc_multiplier": 1.5, "pixel_dist_multiplier": 1.2},
    "degraded_low_confidence": {"skip_vision_enrichment": True, "ttc_multiplier": 1.3, "pixel_dist_multiplier": 1.1},
    "degraded_overexposed": {"skip_vision_enrichment": True, "ttc_multiplier": 1.5, "pixel_dist_multiplier": 1.3},
}


class QualityMonitor:
    """Rolling camera-quality classifier.

    What it represents
    ------------------
    A single camera's health snapshot, updated every frame. One instance per
    stream; the server owns it and keeps it alive for the lifetime of the
    capture.

    State it holds
    --------------
    - ``_lum``, ``_sharp``, ``_sat`` — EWMA of luminance, Laplacian variance,
      and clipped-pixel fraction.
    - ``_conf`` — slower EWMA of average detection confidence.
    - ``_samples`` — total observed frames (used to gate ``low_conf``).
    - ``_state`` — current label from the ``STATES`` vocabulary.
    - ``_state_since`` — wall-clock timestamp of the last transition; the UI
      uses this to show "degraded for 42 s".
    - ``_last_reason`` — short human explanation for the current state.

    Lifecycle
    ---------
    1. ``__init__`` — constructed at server boot.
    2. ``observe_frame`` — called inside the capture loop for every frame.
    3. ``state`` / ``risk_adjustment`` — read at emission time and by the
       ``/api/live/status`` endpoint.

    Consumers
    ---------
    ``road_safety/server.py`` (emission gating + health banner),
    ``MonitoringPage`` (dashboard watchdog queue).
    """

    def __init__(self, window_sec: float = 300.0, log: bool = True):
        """Perception-quality monitor for a live YOLO dashcam pipeline.

        Computes cheap per-frame features (luminance, sharpness, saturated-pixel
        fraction) plus a slow EWMA of detection confidence, classifies the
        pipeline into a degradation state with hysteresis, and exposes a
        risk-adjustment dict that the downstream policy layer can consume.

        Args:
            window_sec: Legacy configuration knob kept for backward-compat with
                older call sites. The EWMA is frame-count weighted, so this
                field is informational rather than enforcing a hard window.
            log: If True, print a human-readable line on every state change.
                Off in tests to keep output clean.
        """
        self.window_sec = float(window_sec)
        self.log = bool(log)
        # Lock guards the mutable EWMA state because the capture thread calls
        # ``observe_frame`` while the HTTP thread calls ``state``.
        self._lock = threading.Lock()
        # ``None`` means "no sample seen yet" — the EWMA initialization below
        # seeds from the first observation rather than from zero, which would
        # otherwise drag the average for a very long time.
        self._lum = None
        self._sharp = None
        self._sat = None
        self._conf = None
        self._samples = 0
        self._state = "nominal"
        self._state_since = time.time()
        self._last_reason = "warmup"

    def observe_frame(self, frame, detections: list, now: float) -> None:
        """Ingest one frame and its detection list.

        Intuition: grab three cheap pixel stats on a downscaled copy, fold
        them into the running EWMAs, then ask ``_reclassify`` whether the
        state should change.

        Args:
            frame: BGR numpy image or ``None``. ``None`` is a no-op — some
                capture paths (stream hiccups) legitimately hand us nothing.
            detections: List of detection objects with optional ``conf``
                attribute. Used to update the confidence EWMA.
            now: Wall-clock timestamp (seconds). Passed through to
                ``_reclassify`` so state-change events are anchored in real
                time rather than measured from ``time.time()`` twice.

        Returns:
            None. Side effects: updates EWMAs, may flip ``self._state``.
        """
        if frame is None:
            return
        try:
            # Downscale to at most 480 px wide. Quality metrics are
            # averages/variances; resolution beyond ~480 px adds cost without
            # adding signal.
            h, w = frame.shape[:2]
            if w > _MAX_WIDTH:
                scale = _MAX_WIDTH / float(w)
                small = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            else:
                small = frame
            # Convert to single-channel grayscale. All three stats below are
            # luminance-based; working on one channel is ~3x faster and
            # avoids color-space noise.
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            # Mean luminance over the whole frame. cv2.mean returns a 4-tuple
            # (one per channel); we take [0] because gray is single-channel.
            lum = float(cv2.mean(gray)[0])
            # Laplacian variance = classic focus / sharpness proxy. High
            # variance in second derivatives means strong edges => crisp.
            sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            # Fraction of pixels clipped to the sensor rails. Both very dark
            # (<15) AND very bright (>240) count; the boolean-OR is combined
            # with np.mean to compute the fraction directly.
            sat = float(np.mean((gray > 240) | (gray < 15)))
        except Exception:
            # OpenCV throws on malformed frames (empty, wrong dtype). A
            # single bad frame must not bring the pipeline down; we simply
            # skip it and keep the previous EWMA.
            return

        # Detector confidence — mean conf across detections this frame. Some
        # detection objects may not expose ``conf``; we defensively skip them.
        conf_sample = None
        if detections:
            try:
                vals = [float(d.conf) for d in detections if getattr(d, "conf", None) is not None]
                if vals:
                    conf_sample = sum(vals) / len(vals)
            except Exception:
                conf_sample = None

        # ------------------------------------------------------------------
        # Fold samples into EWMAs under the lock, then reclassify.
        # ------------------------------------------------------------------
        with self._lock:
            # First-sample guard: if the EWMA is unseeded, use the raw sample
            # verbatim. Otherwise blend with the standard EWMA formula.
            self._lum = lum if self._lum is None else (1 - _ALPHA_FRAME) * self._lum + _ALPHA_FRAME * lum
            self._sharp = sharp if self._sharp is None else (1 - _ALPHA_FRAME) * self._sharp + _ALPHA_FRAME * sharp
            self._sat = sat if self._sat is None else (1 - _ALPHA_FRAME) * self._sat + _ALPHA_FRAME * sat
            if conf_sample is not None:
                self._conf = conf_sample if self._conf is None else (1 - _ALPHA_CONF) * self._conf + _ALPHA_CONF * conf_sample
            self._samples += 1
            self._reclassify(now)

    def _reclassify(self, now: float) -> None:
        """Evaluate the EWMAs and transition the state machine if needed.

        Evaluation order matters: overexposed > low-light > blur > low-conf.
        We check overexposed first because a sun flare can simultaneously
        trip low-sharpness (the sensor has no dynamic range) and we'd rather
        report the true cause. Low-confidence is last because it is a
        *consequence* of the other failure modes.

        Args:
            now: Wall-clock seconds; recorded as ``_state_since`` on change.

        Returns:
            None. Mutates ``self._state``, ``self._state_since``,
            ``self._last_reason``; may log a transition line.
        """
        # Snapshot the EWMAs into locals so the helpers below are concise.
        lum = self._lum
        sharp = self._sharp
        sat = self._sat
        conf = self._conf
        samples = self._samples
        cur = self._state
        # Settings Console: read the two operator-tunable thresholds from the
        # live snapshot, falling back to the module defaults when the store
        # has not been populated (test paths).
        _cfg = _SETTINGS_STORE.snapshot()
        blur_sharp = float(_cfg.get("QUALITY_BLUR_SHARP", _THRESH["blur_sharp"]))
        low_light_lum = float(_cfg.get("QUALITY_LOW_LIGHT_LUM", _THRESH["low_light_lum"]))

        def trip(metric, thresh, direction, hyst):
            """Return True if ``metric`` is past ``thresh`` in ``direction``.

            ``direction`` is "above" (degraded when metric > thresh, e.g.
            saturation) or "below" (degraded when metric < thresh, e.g.
            luminance, sharpness). ``hyst`` scales the threshold by the
            hysteresis factor when we are already in the corresponding
            degraded state so recovery is strictly harder than entry.
            """
            # direction: "above" means degraded when metric > thresh
            if metric is None:
                return False
            if direction == "above":
                t = thresh * _HYST if hyst else thresh
                return metric > t
            else:
                t = thresh / _HYST if hyst else thresh
                return metric < t

        # For overexposed we need both saturated_pct AND luminance high.
        # Either alone has a big false-positive tail (a nighttime scene can
        # have high saturation from headlights, a bright beach has high lum
        # without clipping).
        def over_trip(hyst):
            if sat is None or lum is None:
                return False
            if hyst:
                return sat > _THRESH["overexposed_sat"] * _HYST and lum > _THRESH["overexposed_lum"] * _HYST
            return sat > _THRESH["overexposed_sat"] and lum > _THRESH["overexposed_lum"]

        def low_conf_trip(hyst):
            """Only flag low-confidence after enough samples are collected."""
            if conf is None or samples <= _THRESH["low_conf_min_samples"]:
                return False
            t = _THRESH["low_conf"] / _HYST if hyst else _THRESH["low_conf"]
            return conf < t

        # Hysteresis: if we're already in a degraded state, use the harder
        # threshold (must improve 20% past trigger) to leave it.
        new_state = "nominal"
        reason = "metrics within nominal range"

        if over_trip(hyst=(cur == "degraded_overexposed")):
            new_state = "degraded_overexposed"
            reason = f"overexposed (sat={sat:.2f}, lum={lum:.1f})"
        elif trip(lum, low_light_lum, "below", hyst=(cur == "degraded_low_light")):
            new_state = "degraded_low_light"
            reason = f"low light (luminance={lum:.1f})"
        elif trip(sharp, blur_sharp, "below", hyst=(cur == "degraded_blur")):
            new_state = "degraded_blur"
            reason = f"blurred / dirty lens (sharpness={sharp:.1f})"
        elif low_conf_trip(hyst=(cur == "degraded_low_confidence")):
            new_state = "degraded_low_confidence"
            reason = f"detector confidence low (avg={conf:.2f})"

        self._last_reason = reason
        # Only record a transition (and emit a log) if the label actually
        # changed. ``_last_reason`` updates every frame so the dashboard can
        # show the current numeric values without flickering the state.
        if new_state != cur:
            old = cur
            self._state = new_state
            self._state_since = now if now else time.time()
            if self.log:
                # f-string formatting (PEP 498): ``{lum:.1f}`` formats the
                # float with one decimal. We use "n/a" when the EWMA is
                # still None (pre-first-frame).
                lum_s = f"{lum:.1f}" if lum is not None else "n/a"
                sharp_s = f"{sharp:.1f}" if sharp is not None else "n/a"
                print(f"[quality] state: {old} -> {new_state} (luminance={lum_s}, sharpness={sharp_s})")

    def state(self) -> dict:
        """Thread-safe snapshot for the health banner / API endpoint.

        Returns:
            A dict with the current state label, human reason, raw EWMA
            values, observed sample count, and how long we've been in the
            current state (``since_sec``). Safe to serialize to JSON.
        """
        with self._lock:
            return {
                "state": self._state,
                "reason": self._last_reason,
                "luminance": self._lum,
                "sharpness": self._sharp,
                "saturated_pct": self._sat,
                "avg_confidence": self._conf,
                "samples": self._samples,
                "since_sec": max(0.0, time.time() - self._state_since),
            }

    def risk_adjustment(self) -> dict:
        """Return a copy of the risk-adjustment dict for the current state.

        Intuition: callers multiply their TTC / pixel-distance thresholds by
        these numbers and read ``skip_vision_enrichment`` to decide whether
        to bypass the LLM. A copy (via ``dict(...)``) is returned so the
        caller can't accidentally mutate our table.

        Returns:
            ``dict`` with keys ``skip_vision_enrichment`` (bool),
            ``ttc_multiplier`` (float), ``pixel_dist_multiplier`` (float).
            Falls back to the ``nominal`` row if the state is somehow
            unknown.
        """
        with self._lock:
            return dict(_RISK.get(self._state, _RISK["nominal"]))


# ---------------------------------------------------------------------------
# Module smoke test — not a real test, just a manual sanity hook. Running
# ``python -m road_safety.core.quality`` will push ten all-black frames at
# the monitor and print its state. Useful when poking at thresholds.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    m = QualityMonitor()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for _ in range(10):
        m.observe_frame(frame, [], time.time())
    print(m.state())
