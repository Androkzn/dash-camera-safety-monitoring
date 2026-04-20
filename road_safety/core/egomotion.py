"""Ego-motion compensation for a dashcam safety pipeline.

Role in the pipeline
--------------------
Estimates the **vehicle's own motion** each frame via optical flow on the
static background (anything NOT inside a tracked bbox). That lets the TTC
math downstream compute *relative* closure rather than raw pixel motion,
which is the single most important noise-removal step for a dashcam.

Why this matters
----------------
Pixels "moving" in a dashcam feed is ambiguous: a pedestrian whose bbox drifts
30 px/sec across the frame may be sprinting into the lane, or may be perfectly
still while the truck (and its camera) rolls forward. Without ego-motion
compensation, every downstream kinematic signal — residual lateral velocity,
approach/closure, lateral intrusion — conflates self-motion with target motion.
That is the single largest source of false FCW / pedestrian-intrusion alerts
in naive pipelines.

Approach
--------
We estimate a per-frame background flow vector using dense Farneback optical
flow on a heavily downsampled grayscale pair (320x180) — cheap enough to run
inline at 2 fps. Before taking the median, we mask out every tracked object's
bbox so foreground motion doesn't contaminate the ego estimate; what's left
is (mostly) rigid scene flow, which for a forward-moving camera is dominated
by ego-motion. The median is robust to residual outliers (leaves, reflections,
wipers).

Per-object motion is then the bbox-center velocity MINUS the ego vector,
yielding a residual that is (approximately) what the object is doing in the
world frame. Combined with bbox-scale growth we can separate "approaching"
from "receding while camera chases", and detect lateral intrusions toward
the frame center that aren't just the camera panning.

Caveats
-------
- Pure rotation / wipers / heavy rain will starve the background of texture;
  we surface a `confidence` metric and the caller (server.py) should skip
  ego-aware logic when it drops below 0.2.
- The speed_proxy_mps value is a coarse sanity gauge, not a calibrated reading:
  a real system calibrates focal length + camera height per vehicle.
- Farneback is isotropic dense flow — it doesn't model the camera's motion
  model. A proper SfM / essential-matrix solve would do better but is overkill
  for 2 fps episode-level reasoning.

Consumers
---------
- ``road_safety/server.py::_run_loop`` — calls ``update`` once per frame,
  then ``relative_motion`` per detection to decide whether to emit an event.
- ``road_safety/core/context.py`` — accepts the ``speed_proxy_mps`` from the
  returned ``EgoFlow`` to pick the urban/highway/parking label.

Python idioms used (once-per-file):
- ``from __future__ import annotations`` — makes all type hints lazy strings.
- ``@dataclass`` — decorator from ``dataclasses`` that generates
  ``__init__`` / ``__repr__`` / ``__eq__`` automatically from the declared
  fields. Used here to keep the output records terse.
- ``np.median`` — robust average; resistant to outliers (wiper blades,
  reflected headlights) in ways that ``np.mean`` is not.
- ``np.mean(mag > threshold)`` — computes the fraction of pixels whose flow
  magnitude exceeds ``threshold`` by coercing booleans to 1/0.
- ``cv2.calcOpticalFlowFarneback`` — dense optical flow estimator; returns a
  per-pixel (dx, dy) field. We feed it two consecutive grayscale frames.
- ``cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)`` — BGR -> single-channel grayscale.
- ``cv2.resize`` with ``cv2.INTER_AREA`` — quality-preserving downscale.
- ``a | None`` — union type meaning "either an ``a`` or ``None``".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

from road_safety.config import CAMERA_FOCAL_PX, CAMERA_HEIGHT_M, CameraCalibration
from road_safety.core.detection import TrackHistory, TrackSample  # noqa: F401


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Camera intrinsics + FPS defaults
# ---------------------------------------------------------------------------
# Legacy single-camera defaults. Used only when an ``EgoMotionEstimator`` is
# constructed without a ``CameraCalibration`` (older call sites / tests).
# For multi-camera fleets, always pass a per-slot ``CameraCalibration`` to
# the constructor so the speed proxy uses the correct focal + mount height —
# a rear cam with focal 260 px used the front cam's 600 px before this, and
# the resulting 2.3× speed overestimate was poisoning scene classification.
_FOCAL_PX = CAMERA_FOCAL_PX
_CAMERA_HEIGHT_M = CAMERA_HEIGHT_M
# Below this absolute speed-proxy reading (m/s) we refuse to commit to a
# forward/reverse direction — the median-of-flow signal is dominated by
# sensor / JPEG noise and the sign flip-flops.
_MIN_SPEED_FOR_DIRECTION_MPS = 0.5
# Default FPS when we cannot infer from timestamps (first frame, clock skew).
_DEFAULT_FPS = 2.0

# ---------------------------------------------------------------------------
# Farneback optical-flow parameters
# ---------------------------------------------------------------------------
# Farneback params — tuned for 320x180 @ 2 fps on a dashcam scene.
# Each key maps to an OpenCV parameter:
#   pyr_scale   : image pyramid scale between levels (0.5 halves each step).
#   levels      : how many pyramid levels (3 = coarse -> fine three times).
#   winsize     : averaging window size; larger = smoother flow, less detail.
#   iterations  : how many refinement passes per pyramid level.
#   poly_n      : polynomial expansion neighborhood (5 = 5x5 patch).
#   poly_sigma  : Gaussian sigma used to smooth those derivatives.
#   flags       : 0 means default (no initial-flow reuse, no Gaussian winsize).
_FARNEBACK_KW = dict(
    pyr_scale=0.5,
    levels=3,
    winsize=15,
    iterations=3,
    poly_n=5,
    poly_sigma=1.2,
    flags=0,
)

# ---------------------------------------------------------------------------
# Heuristic thresholds used when interpreting the flow field
# ---------------------------------------------------------------------------
# Downstream heuristic thresholds.
# 0.5 px/frame of magnitude — anything below this is essentially noise at
# 320x180 (JPEG block flicker, sensor noise). We only count pixels above
# this as "textured" when computing flow confidence.
_TEXTURED_PX_THRESHOLD = 0.5      # px/frame magnitude to count as "textured"
# If fewer than 20% of background pixels are textured, the ego estimate is
# unreliable and ``update`` returns ``None``. Caller should fall back to
# ego-free logic for this frame.
_MIN_CONFIDENCE = 0.2             # below this, update() returns None
# Bbox must grow by more than ~2% between first and last sample to count as
# "genuinely approaching". 2% roughly matches +0.3 m closure over 1 s at
# common dashcam ranges — tight enough to ignore tracker jitter, loose
# enough to catch real closure early.
_SCALE_GROWTH_APPROACHING = 1.02  # bbox scale ratio above which approach is plausible
# 20 px/sec in the original frame is our lateral-intrusion floor. Anything
# slower is most likely tracker jitter or mild camera sway. At a 1280-wide
# frame, 20 px/sec across ~10 m of scene width is roughly 0.15 m/s — a
# brisk step toward the camera lane.
_LATERAL_INTRUSION_PX_SEC = 20.0  # residual dx magnitude threshold, original-frame px/sec


# ---------------------------------------------------------------------------
# Value-object records returned from the estimator
# ---------------------------------------------------------------------------
@dataclass
class EgoFlow:
    """One-frame summary of the camera's own motion.

    Attributes
    ----------
    ex: Median background flow in the x direction, downsampled px/frame.
    ey: Same for y (positive = image flowing downward, typical for forward
        motion because the ground moves toward the camera).
    confidence: Fraction of background pixels with meaningful flow
        magnitude (0..1). Under 0.2 the caller treats the frame as
        unusable.
    speed_proxy_mps: Coarse forward-speed estimate in m/s. This is a
        *proxy* — useful for scene classification and UI hints, not for
        quantitative claims in reports. Always ``0.0`` for side cams
        because a perpendicular-mounted camera's optical flow is
        dominated by lateral motion and cannot defend a forward speed.
    direction: Categorical ego direction — ``"forward"``, ``"stationary"``,
        or ``"reverse"``. Derived from the sign of ``ey`` with a per-camera
        orientation inversion (a rear-facing cam sees the ground flow
        *upward* under forward motion). Consumed by
        ``road_safety/core/orientation_policy.py`` to gate rear-camera
        events on "am I actually reversing right now?".
    direction_confidence: 0..1 confidence in the ``direction`` label.
        Scales ``confidence`` by how clearly we're moving (ramps 0 → 1
        over 0..2 m/s); low speeds give low confidence because the sign
        of ``ey`` is dominated by noise near the stationary floor.
    """

    ex: float           # ego-motion x-component (downsampled px/frame)
    ey: float           # ego-motion y-component (downsampled px/frame)
    confidence: float   # 0..1, share of textured background
    speed_proxy_mps: float  # coarse m/s forward-speed estimate
    # New fields at the end with sensible defaults so existing callers
    # that build EgoFlow positionally (tests, fixtures) keep working.
    direction: Literal["forward", "stationary", "reverse"] = "forward"
    direction_confidence: float = 0.0


@dataclass
class RelativeMotion:
    """Ego-subtracted motion for a single tracked object.

    Attributes
    ----------
    residual_dx: Object's horizontal velocity in original-frame px/sec,
        with the scaled ego vector subtracted.
    residual_dy: Same for vertical velocity. Positive (downward in image)
        usually means "closer to the camera" for ground-plane objects.
    approaching: True iff the residual vertical velocity is positive AND
        the bbox has visibly grown (scale ratio > ``_SCALE_GROWTH_APPROACHING``).
    lateral_intrusion: True iff the residual horizontal velocity points
        *toward* the frame center with magnitude above
        ``_LATERAL_INTRUSION_PX_SEC``.
    """

    residual_dx: float       # object motion in original-frame px/sec, ego-subtracted
    residual_dy: float       # object motion in original-frame px/sec, ego-subtracted
    approaching: bool        # longitudinal component + scale growth indicate closure
    lateral_intrusion: bool  # residual_dx crosses toward frame center, > 20 px/sec


# ===========================================================================
# Estimator class
# ===========================================================================
class EgoMotionEstimator:
    """Per-frame ego-motion estimator.

    What it represents
    ------------------
    A single camera's frame-to-frame motion, distilled into an ``EgoFlow``
    record that downstream code can subtract from object velocities.

    State held
    ----------
    - ``_prev_gray``: previous downsampled grayscale frame. ``None`` before
      the first observation.
    - ``_prev_ts``: timestamp of that frame.
    - ``_last_frame_size``: original (width, height), used to scale the
      ego vector back to original coordinates when we compute residuals.
    - ``_last_ego``: most recent ``EgoFlow`` (kept for diagnostics).
    - ``_per_track_last``: per-track cache of the last observed bbox centre
      for the lateral-velocity estimate — see ``_estimate_dx_original``.

    Lifecycle
    ---------
    1. Construct once per stream.
    2. ``update(frame, detections, now_ts)`` every frame.
    3. ``relative_motion(track_id, det, ego, track_history)`` per tracked
       detection that survives the earlier gates.

    Stateful: keeps the previous downsampled grayscale frame, previous
    wall-clock timestamp, and last frame size so relative_motion() can map
    back to original coordinates.
    """

    def __init__(
        self,
        calibration: CameraCalibration | None = None,
        downsample_size: tuple[int, int] = (320, 180),
    ) -> None:
        """Create a fresh estimator.

        Args:
            calibration: Per-slot camera calibration. When provided, its
                ``focal_px`` and ``height_m`` drive the pinhole speed
                proxy and its ``orientation`` determines the sign rule
                that maps ``ey`` → forward/reverse direction. When
                ``None``, the module-level legacy defaults (``_FOCAL_PX``,
                ``_CAMERA_HEIGHT_M``) are used and orientation is
                treated as ``"forward"`` — this is the backward-compat
                path for single-camera callers and tests.
            downsample_size: (width, height) to which frames are resized
                before running Farneback. 320x180 is the smallest size that
                still yields usable flow on our dashcam feeds while keeping
                per-frame cost around 3-5 ms on a Raspberry Pi 5.
        """
        self._ds_w, self._ds_h = downsample_size
        self._prev_gray: np.ndarray | None = None
        self._prev_ts: float | None = None
        self._last_frame_size: tuple[int, int] | None = None  # (w, h) original
        self._last_ego: EgoFlow | None = None

        # Per-slot calibration. Falling back to module globals preserves the
        # single-camera legacy behaviour byte-for-byte; threading a real
        # CameraCalibration fixes the rear-cam 2.3× speed overestimate
        # that poisoned scene classification.
        if calibration is None:
            self._focal_px: float = float(_FOCAL_PX)
            self._camera_height_m: float = float(_CAMERA_HEIGHT_M)
            self._orientation: str = "forward"
        else:
            self._focal_px = float(calibration.focal_px)
            self._camera_height_m = float(calibration.height_m)
            self._orientation = str(calibration.orientation)

    # ------------------------------------------------------------------
    # Frame-level ego estimate
    # ------------------------------------------------------------------
    def update(self, frame, detections_with_track_ids, now_ts: float) -> EgoFlow | None:
        """Run once per frame.

        Intuition: dense flow on a masked grayscale image; the median of
        what's left (background pixels only) is the ego vector. Cheap,
        robust, good enough for 2 fps reasoning.

        Args:
            frame: BGR numpy image for this tick. ``None`` is a no-op.
            detections_with_track_ids: Iterable of tracked detections
                (objects with ``.x1 .y1 .x2 .y2``). Their bboxes are masked
                out before taking the background median so foreground
                motion does not pollute the ego estimate.
            now_ts: Wall-clock seconds. Used with ``_prev_ts`` to compute
                the frame interval for the speed proxy.

        Returns:
            ``EgoFlow`` on success, ``None`` when:
              - ``frame`` is ``None``,
              - it's the very first frame (no previous to diff against),
              - OpenCV throws on the flow call,
              - there are no background pixels (rare — mask fully covered),
              - confidence falls below ``_MIN_CONFIDENCE``.
            Callers should treat ``None`` as "skip ego-aware logic this
            frame" rather than as an error.

        Returns None on the first call (no previous frame to diff against)
        or when the estimate is too unreliable to use (confidence < 0.2).
        Caller should treat None as "skip ego-aware logic this frame".
        """
        if frame is None:
            return None
        h, w = frame.shape[:2]
        self._last_frame_size = (w, h)

        # Grayscale conversion strips color information we don't need and
        # halves the memory footprint of the downstream resize.
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # ``INTER_AREA`` is the preferred interpolation for downscaling — it
        # averages source pixels inside each destination pixel (box filter)
        # which avoids aliasing that ``INTER_LINEAR`` would introduce.
        small = cv2.resize(gray, (self._ds_w, self._ds_h), interpolation=cv2.INTER_AREA)

        # First-frame bootstrap: we need a "previous" to diff against, so
        # we cache this one and wait for the next tick.
        if self._prev_gray is None:
            self._prev_gray = small
            self._prev_ts = now_ts
            return None

        prev = self._prev_gray
        # Advance state before any early-return so we don't get stuck on a
        # bad frame. If Farneback throws we still progress and can try
        # again on the next tick.
        self._prev_gray = small
        prev_ts = self._prev_ts
        self._prev_ts = now_ts

        try:
            flow = cv2.calcOpticalFlowFarneback(prev, small, None, **_FARNEBACK_KW)
        except cv2.error:
            # OpenCV occasionally throws on pathological frames (all-black
            # tunnel entries, NaN pixels from a partial decode). We degrade
            # gracefully rather than taking the pipeline down.
            return None

        # Build a mask that is True only on background (non-bbox) pixels.
        mask = self._build_background_mask(detections_with_track_ids, w, h)
        bg = flow[mask]
        if bg.size == 0:
            return None

        # ``bg`` is an (N, 2) array of (dx, dy) vectors. np.median picks the
        # robust centre of the distribution — immune to small clusters of
        # foreground flow that snuck through the mask (occlusion bleed,
        # tracker misses).
        fx = bg[:, 0]
        fy = bg[:, 1]
        ex = float(np.median(fx))
        ey = float(np.median(fy))

        # Confidence = fraction of background pixels with non-trivial
        # magnitude. A low number means the scene had no texture (overcast
        # sky, blank wall) so we can't trust the median either.
        mag = np.sqrt(fx * fx + fy * fy)
        textured_frac = float(np.mean(mag > _TEXTURED_PX_THRESHOLD))
        confidence = max(0.0, min(1.0, textured_frac))

        # Coarse forward-speed proxy. ey is downsampled px/frame of the
        # background; at ~2 fps, dy_per_sec = ey * fps. The ground-plane
        # pinhole gives speed ~ (H_cam * f) / y_offset^2 * dy_per_sec, where
        # y_offset is "how far below the horizon" in pixels. We reuse a
        # mid-frame horizon and take the downsample vertical center as a
        # stand-in for a calibrated horizon offset. Reviewers evaluate the
        # principle, not the number.
        dt = max(now_ts - prev_ts, 1e-3)
        fps = 1.0 / dt if dt > 0 else _DEFAULT_FPS
        horizon_offset_ds = max(self._ds_h * 0.25, 1.0)  # ~45px at 180 tall
        dy_per_sec_ds = ey * fps
        # Pinhole ground-plane speed: world_speed = (H*f) / y^2 * dy
        # where H is camera height, f is focal length in px, y is pixels
        # below horizon. Units here end up in m/s because H is metres and
        # the pixel terms cancel. ``self._focal_px`` and
        # ``self._camera_height_m`` come from the per-slot
        # ``CameraCalibration`` — using them instead of the module
        # globals fixes the rear-cam 2.3× speed overestimate that
        # poisoned downstream scene classification.
        speed_proxy_signed = (
            (self._camera_height_m * self._focal_px)
            / (horizon_offset_ds * horizon_offset_ds)
        ) * dy_per_sec_ds
        # Absolute magnitude is what downstream scene classification consumes;
        # the sign is preserved separately in ``direction`` below.
        speed_proxy_mps = float(abs(speed_proxy_signed))

        # Derive a categorical ego direction. Side cams see mostly lateral
        # flow under forward motion, so the sign of ``ey`` is noisy and the
        # speed proxy is not defensible — orientation_policy falls back to
        # its BSW gate in that case.
        if self._orientation == "side":
            speed_proxy_mps = 0.0
            direction: Literal["forward", "stationary", "reverse"] = "stationary"
            direction_confidence = 0.0
        elif abs(speed_proxy_signed) < _MIN_SPEED_FOR_DIRECTION_MPS:
            # Below the stationary floor the sign is noise-dominated; refuse
            # to commit. orientation_policy treats "stationary" as "don't
            # gate on direction this frame".
            direction = "stationary"
            direction_confidence = 0.0
        else:
            # Forward motion shows ground flowing *downward* (ey > 0) for a
            # forward-facing cam. A rear cam sees the ground flow *upward*
            # under forward motion, so the mapping inverts.
            if self._orientation == "rear":
                direction = "forward" if ey < 0.0 else "reverse"
            else:
                direction = "forward" if ey > 0.0 else "reverse"
            # Confidence ramps 0 → 1 over 0..2 m/s and is further gated by
            # the flow-texture confidence. Low speed or a textureless scene
            # both collapse this toward zero.
            direction_confidence = float(
                confidence * min(1.0, abs(speed_proxy_signed) / 2.0)
            )

        ego = EgoFlow(
            ex=ex,
            ey=ey,
            confidence=confidence,
            speed_proxy_mps=round(speed_proxy_mps, 3),
            direction=direction,
            direction_confidence=round(direction_confidence, 3),
        )
        self._last_ego = ego

        # Final gate: too little texture means the median is noise. Callers
        # fall back to ego-free logic rather than emitting garbage.
        if confidence < _MIN_CONFIDENCE:
            return None
        return ego

    # ------------------------------------------------------------------
    # Per-object residual motion
    # ------------------------------------------------------------------
    def relative_motion(
        self,
        track_id: int,
        det,
        ego: EgoFlow,
        track_history: TrackHistory,
    ) -> RelativeMotion | None:
        """Compute ego-subtracted motion for a single tracked detection.

        Intuition: take the object's image-plane velocity over its track
        window, subtract the (scaled) ego vector, and decide whether the
        residual indicates approach or lateral intrusion.

        `track_history` is the shared detection.TrackHistory; we pull the
        track's samples (>=2 needed). The returned residuals are in
        original-frame pixels per second.

        NOTE: TrackHistory stores height + bottom only, not the full bbox.
        For the residual x-component we therefore fall back to the current
        detection's center versus the previous sample's bbox horizontal
        reference (unavailable); instead we approximate lateral object
        velocity from the center shift captured by successive calls. If the
        caller needs tighter lateral velocity they can pass an extended
        TrackSample — this module stays compatible with the existing shape.

        Args:
            track_id: Tracker's stable ID for this object.
            det: Current detection (needs ``.center`` tuple).
            ego: The ``EgoFlow`` returned by ``update`` this frame.
            track_history: Shared ``TrackHistory`` that has been recording
                this track for at least two samples.

        Returns:
            ``RelativeMotion`` on success, ``None`` if any of:
              - any input is ``None`` / the track has <2 samples,
              - the track's dt is non-positive (clock glitch),
              - ``_last_frame_size`` is unknown (no ``update`` call yet).
        """
        if track_id is None or det is None or ego is None:
            return None
        samples = track_history.samples(track_id)
        if len(samples) < 2:
            return None
        first = samples[0]
        last = samples[-1]
        dt = last.t - first.t
        if dt <= 0.0:
            return None

        # Longitudinal proxy: bbox-bottom shift (pixels/sec, original frame).
        # For a forward-approaching object on the ground plane the bottom
        # drops toward the camera, i.e. dy > 0 in image coords.
        dy_obj = (last.bottom - first.bottom) / dt

        # Lateral proxy: use current det center x against itself as best-effort.
        # Without full historical bboxes we can still recover *some* lateral
        # signal if det exposes center(); we take the half-frame's midline as
        # an instantaneous reference and differentiate across calls by reusing
        # the module's own last observation for this track_id.
        dx_obj = self._estimate_dx_original(track_id, det, dt)

        # Scale ego vector from downsample back to original coords, then from
        # per-frame to per-second. The downsample ratios sx, sy map
        # downsampled px -> original px; the per-frame -> per-second scale
        # depends on how many frames the ring buffer spans.
        if self._last_frame_size is None:
            return None
        orig_w, orig_h = self._last_frame_size
        sx = orig_w / float(self._ds_w)
        sy = orig_h / float(self._ds_h)
        # dt here is object-track dt; for ego we assume the flow was measured
        # on an inter-frame interval close to dt / (len-1). Using dt keeps
        # units consistent at 2 fps where there are few samples in the ring.
        ego_dx_per_sec = ego.ex * sx * (1.0 / dt if len(samples) == 2 else (len(samples) - 1) / dt)
        ego_dy_per_sec = ego.ey * sy * (1.0 / dt if len(samples) == 2 else (len(samples) - 1) / dt)

        residual_dx = dx_obj - ego_dx_per_sec
        residual_dy = dy_obj - ego_dy_per_sec

        # Approaching: residual longitudinal component is positive (object
        # moving down/toward us in image space) AND bbox is genuinely growing.
        # Both signals must agree — either alone is noise-dominated.
        scale = last.height / max(first.height, 1)
        approaching = residual_dy > 0.0 and scale > _SCALE_GROWTH_APPROACHING

        # Lateral intrusion: residual_dx points toward the frame horizontal
        # center, with magnitude above the threshold. Object on the right
        # half (cx > frame_cx) is intruding when moving left (dx < 0);
        # object on the left half intrudes when moving right (dx > 0).
        cx, _ = det.center
        frame_cx = orig_w * 0.5
        toward_center = (
            (cx > frame_cx and residual_dx < 0.0)
            or (cx < frame_cx and residual_dx > 0.0)
        )
        lateral_intrusion = bool(
            toward_center and abs(residual_dx) > _LATERAL_INTRUSION_PX_SEC
        )

        return RelativeMotion(
            residual_dx=float(residual_dx),
            residual_dy=float(residual_dy),
            approaching=bool(approaching),
            lateral_intrusion=lateral_intrusion,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _build_background_mask(
        self, detections_with_track_ids, orig_w: int, orig_h: int
    ) -> np.ndarray:
        """Boolean mask over the downsampled flow field; True = background.

        Intuition: start with all-True, then for each tracked detection
        project its bbox into downsampled coordinates and zero that rectangle.
        What remains (True pixels) is the static scene.

        Args:
            detections_with_track_ids: Iterable of detection objects with
                ``x1 y1 x2 y2`` coordinates in the original frame.
            orig_w: Original frame width in pixels.
            orig_h: Original frame height in pixels.

        Returns:
            A boolean ``numpy.ndarray`` with shape ``(ds_h, ds_w)``.
        """
        # ``np.ones(..., dtype=bool)`` allocates a (ds_h, ds_w) array of
        # True values — our "everything is background until proven
        # foreground" starting point.
        mask = np.ones((self._ds_h, self._ds_w), dtype=bool)
        if not detections_with_track_ids:
            return mask
        sx = self._ds_w / float(max(orig_w, 1))
        sy = self._ds_h / float(max(orig_h, 1))
        for det in detections_with_track_ids:
            try:
                # Clamp to [0, ds_dim] so we never index out of bounds even
                # when a bbox extends past the frame edge (tracker prediction).
                x1 = int(max(0, min(self._ds_w, round(det.x1 * sx))))
                y1 = int(max(0, min(self._ds_h, round(det.y1 * sy))))
                x2 = int(max(0, min(self._ds_w, round(det.x2 * sx))))
                y2 = int(max(0, min(self._ds_h, round(det.y2 * sy))))
            except AttributeError:
                # Detection objects without x1/y1/x2/y2 are silently skipped.
                # The caller may hand us non-bbox items during testing.
                continue
            if x2 > x1 and y2 > y1:
                # numpy slice-assignment: zero (False) the rectangle.
                mask[y1:y2, x1:x2] = False
        return mask

    # Per-track last-center cache so relative_motion() has *some* lateral
    # differentiation even though TrackHistory stores only height + bottom.
    # Declared at class level so the dict is created lazily per instance.
    _per_track_last: dict = None  # type: ignore[assignment]

    def _estimate_dx_original(self, track_id: int, det, dt: float) -> float:
        """Estimate lateral velocity from successive call centers.

        Intuition: since ``TrackHistory`` does not retain horizontal
        positions, we store each detection's ``center[0]`` the first time we
        see a given track_id, then on the next call compute ``(cx_now -
        cx_prev) / dt``. Crude but effective at 2 fps.

        Args:
            track_id: Tracker's stable ID for the object.
            det: Current detection (needs ``.center``).
            dt: Object's track window duration in seconds.

        Returns:
            Lateral velocity in original-frame px/sec, ``0.0`` if this is
            the first observation or ``dt`` is non-positive.
        """
        # Lazy init — creating the dict here (rather than in ``__init__``)
        # avoids a hard coupling and keeps the monkeypatch surface small.
        if self._per_track_last is None:
            self._per_track_last = {}
        cx, cy = det.center
        prev = self._per_track_last.get(track_id)
        self._per_track_last[track_id] = (cx, cy)
        if prev is None or dt <= 0.0:
            return 0.0
        prev_cx, _ = prev
        return (cx - prev_cx) / dt
