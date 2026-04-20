"""Detection and event logic — used by both batch (tools/analyze.py) and the
live server (road_safety/server.py).

Role in the pipeline
--------------------
This is stage 2 of perception. ``stream.py`` produces frames; this module
turns each frame into a list of ``Detection`` objects (bboxes + class +
confidence + track id), maintains a rolling history per track, and computes
the physical-world quantities (distance in metres, time-to-collision in
seconds, risk bucket) that downstream gates and the alerting layer consume.

Identity-persistent tracking via ByteTrack so downstream code reasons about
the *same* object across frames. This enables:

  1. Per-pair episode dedup — a single conflict between track_id=7 and
     track_id=12 emits once, not N times at the source FPS.
  2. Kinematic risk — TTC from bbox-scale growth and pair-wise closing rate;
     monocular distance from the pinhole / ground-plane prior. Risk is a
     physical quantity (seconds / metres), not a pixel count.

Conflict-detection methodology aligns with published SSAM / SAFE-UP / PET
research. TTC is published only after multi-gate validation:
  - pair-wise closing-rate TTC preferred over single-object scale-expansion
  - inter-object 3D distance (depth difference + lateral offset), not
    image-plane bbox proximity, gates vehicle-vehicle interactions
  - convergence-angle filter rejects parallel / same-direction traffic
  - sustained-growth requirement (monotonic, jitter-floor pixel delta,
    non-trivial track motion) before any TTC value is returned
  - per-pair confidence floor for vehicle-vehicle pairs

Graceful degradation: if the tracker returns no IDs (first frames, or
non-trackable inputs), detections still flow through with track_id=None and
downstream code degrades to per-frame behaviour.

Geometry crash-course (pinhole camera)
--------------------------------------
A pinhole camera projects 3D world points onto a 2D image plane. If ``f`` is
the focal length in pixels, ``H`` is an object's real height in metres, and
``h`` is its measured height in pixels on the image, then its distance
``Z`` from the camera is approximately:

    Z  =  f * H / h

That's the "known-height" prior. Intuition: a 1.7 m person who occupies 170
pixels vertically is twice as far away as one who occupies 340 pixels.

A second estimate comes from the ground plane. If the camera is mounted
``H_cam`` metres above the road and the road horizon sits at image row
``y_horizon``, then the bottom of any object resting on the ground at image
row ``y_bottom`` is at distance:

    Z  =  f * H_cam / (y_bottom - y_horizon)

Intuition: objects closer to the horizon line are farther away. This
collapses on occluded feet / elevated objects, which is why we take the
*more conservative* of the two estimates — we'd rather miss a close call
than hallucinate one.

Why trailing-window TTC (not instantaneous velocity)
----------------------------------------------------
YOLO bounding boxes jitter by a pixel or two frame-to-frame even on a
stationary object. A single-frame velocity estimate is dominated by that
jitter. Instead we fit a slope over the last N samples (~2 seconds at 2 fps):
monotonic growth + a minimum absolute pixel delta + a minimum center motion
across the window are all required before any TTC value is published. This
trades latency (TTC updates every ~2 s, not every frame) for robustness.
"""

# ``from __future__ import annotations`` makes type hints lazily evaluated.
# In practice this means ``list[X] | None`` works on older interpreters and
# that forward references don't need to be quoted.
from __future__ import annotations

# ----- IMPORTS -----
import math                             # stdlib: hypot, sqrt, isfinite
from collections import deque           # stdlib: double-ended queue with maxlen
                                        #   — a fixed-capacity ring buffer; when
                                        #   you append past maxlen, the oldest
                                        #   item is dropped. Perfect for
                                        #   rolling-window history.
from dataclasses import dataclass, field  # decorator for "struct-like" classes
                                          # (auto-generates __init__, __repr__,
                                          # __eq__ based on class annotations).
from pathlib import Path                # stdlib: object-oriented filesystem paths

from typing import Any                  # generic placeholder for ndarray-typed args

import cv2                              # OpenCV: drawing + image I/O
from ultralytics import YOLO            # YOLOv8 model wrapper (third-party)

# ----- CLASS TAXONOMY + CONFIDENCE / AREA GATES -----

# ``{...}`` literal syntax here creates a *set*, not a dict — a set is an
# unordered collection with O(1) membership tests. Used for fast
# ``cls in VEHICLE_CLASSES`` checks in the detection hot path.
VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}
PEDESTRIAN_CLASSES = {"person"}

# GATE: confidence floor for vehicle detections. Below this we assume the
# detection is noise (distant blob / partial object). This gate exists to
# kill the false-positive class of "YOLO-hallucinated vehicles" — e.g.
# billboards, trash cans, tree shadows that score 0.3-0.4.
CONF_THRESHOLD = 0.50         # default (vehicles) — tuned against cars/trucks

# GATE: when two vehicles are paired up for a close-interaction event, their
# *mean* confidence must clear this floor. Stricter than the per-detection
# floor because vehicle-vehicle pairs have historically been the biggest
# source of false alerts (perspective overlap looks like proximity).
VEHICLE_PAIR_CONF_FLOOR = 0.60

# GATE: minimum bbox area in square pixels. ~40x40 = 1600 > 1200 so a small
# distant car still passes. Kills tiny noise blobs.
MIN_BBOX_AREA = 1200          # default (vehicles) — a small distant car is ~40×40

# Class-specific detection floors. Pedestrians are inherently smaller on-screen
# (distant / elevated / dashcam views through foreground traffic — YOLOv8n
# on a Times Square feed returns persons at 0.17-0.35 confidence and
# 800-1500 px² bboxes). Applying vehicle-tuned thresholds wipes them out.
# Persons have their own downstream sanity checks (aspect ratio guard, episode
# sustained-risk model, pair-TTC gates) so a permissive detection floor does
# not translate to a permissive alert floor.
#
# Trade-off: lowering PERSON_CONF_THRESHOLD below ~0.25 with YOLOv8n admits
# occasional noise detections. Deployments that care about pedestrian recall
# should upgrade to YOLOv8s or YOLOv8m via ROAD_MODEL_PATH — at similar
# bbox area the larger models produce meaningfully higher person confidence.
PERSON_CONF_THRESHOLD = 0.25
PERSON_MIN_BBOX_AREA = 400

# ----- CAMERA CALIBRATION (SOURCE OF TRUTH: road_safety/config.py) -----

# All path and env-var values flow through ``road_safety/config.py`` — never
# compute paths with ``Path(__file__).parent`` in a module (project rule).
# The ``as _CFG_CAMERA_HEIGHT_M`` / ``noqa: E402`` dance is just to rename
# and silence a lint rule about imports not being at the file top.
from road_safety.config import (  # noqa: E402
    CAMERA_FOCAL_PX,
    CAMERA_HEIGHT_M as _CFG_CAMERA_HEIGHT_M,
    CAMERA_HORIZON_FRAC,
    DEFAULT_CAMERA_CALIBRATION,
    CameraCalibration,
    MODEL_PATH,
)
# Settings Console: hot-path snapshot reads. The store seeds itself from the
# same constants we define above, so a fresh boot is a no-op until the
# operator changes something. Each gate function below reads ``STORE.snapshot()``
# once and caches it for the duration of the call so all comparisons in one
# frame see the same config.
from road_safety.settings_store import STORE as _SETTINGS_STORE  # noqa: E402
# Tracker config filename — ByteTrack parameters (Kalman filter, IoU thresholds,
# track lost/retain timeouts). Ultralytics ships this file; we don't override it.
TRACKER_CFG = "bytetrack.yaml"

# Pinhole/ground-plane approximation — values come from per-camera config
# (ROAD_CAMERA_FOCAL_PX, ROAD_CAMERA_HEIGHT_M). Wrong values here bias every
# distance/speed signal downstream, so treat as deployment config.
#
# Intuition: FOCAL_PX is the camera's "magnification" in pixels — a longer
# lens yields more pixels per metre of real object. CAMERA_HEIGHT_M is how
# high the dashcam is mounted off the ground. See module docstring for the
# pinhole formula.
CAMERA_HEIGHT_M = _CFG_CAMERA_HEIGHT_M
FOCAL_PX = CAMERA_FOCAL_PX

# Typical real-world heights (m) used to back out distance from bbox height
# when the ground-plane assumption fails (e.g. occluded feet). These are
# population averages; a mis-identified truck classified as "car" will have
# its distance estimate biased accordingly — which is fine because the
# conservative max() below blunts the error.
TYPICAL_HEIGHT_M = {
    "person": 1.7,
    "car": 1.5,
    "truck": 3.0,
    "bus": 3.2,
    "motorcycle": 1.3,
}

# ----- RISK THRESHOLDS (physical units) -----

# Risk thresholds in *physical* units.  Calibrated for *observation/analytics*
# cameras (SSAM / SAFE-UP / PET research), NOT in-vehicle FCW. Tightened to
# reduce false positives: only genuinely imminent collisions trigger high.
#
# 0.5 s is essentially "already colliding" — at highway speeds that's a
# single car length. 1.0 s is the SSAM "serious conflict" threshold. These
# gates exist to kill the false-positive class of "distant approaching
# traffic that is not actually a near-miss".
TTC_HIGH_SEC = 0.5
TTC_MED_SEC = 1.0
DIST_HIGH_M = 2.0       # within arm's reach
DIST_MED_M = 5.0        # roughly 1 vehicle length

# Trailing-frames ring per track for TTC (need ≥ 4 samples for sustained-growth).
# At 2 fps, 12 samples = 6 seconds of history — enough to see a 2 s window
# move across the buffer while keeping some lookback for context.
TRACK_HISTORY_LEN = 12

# Minimum scale-expansion ratio to consider an object "approaching".
# At 2fps, bbox jitter of ±2px on a 50px box is ±4%, so we need >10%
# growth to distinguish real approach from noise. This gate exists to kill
# the false-positive class of "stationary-object-with-jitter" TTC alerts.
MIN_SCALE_GROWTH = 1.10

# Sustained-evidence gates for TTC. A single-frame TTC < 1 s on a stationary
# object is almost always bbox jitter, not a real conflict. TTC is only
# returned after all of the following hold over the trailing window:
#   - bbox height grows MONOTONICALLY (one tracker dip allowed)
#   - absolute height delta exceeds the pixel-jitter floor
#   - track centre moves more than sub-pixel jitter across the window
#   - for pair-TTC: distance decreases monotonically and at least one track
#     shows non-trivial motion
#
# Collectively these gates exist to kill the false-positive class of
# "bbox jitter amplified through small denominators" — the single biggest
# source of noise TTC in early versions of this system.
TTC_REQUIRED_SAMPLES = 4                     # ≥ 4 samples = ≥ 2 s window at 2 fps
TTC_MIN_HEIGHT_DELTA_PX = 6                  # 6 px > typical 1-2 px bbox jitter
PAIR_TTC_REQUIRED_SAMPLES = 4
PAIR_TTC_MIN_DISTANCE_REDUCTION_PX = 8.0     # pair centres must actually get closer
TTC_MIN_TRACK_MOTION_PX = 4.0                # sub-pixel jitter floor

# Minimum closing rate (px/s) for pair-TTC to fire. Filters noise from
# bbox center jitter at low frame rates. Raised from 1.0 → 4.0 px/s after
# alert-fatigue postmortem: 1 px/s is essentially the jitter floor at 2 fps.
MIN_CLOSING_RATE_PX = 4.0

# Depth-aware proximity gate for vehicle-vehicle interactions. Two cars
# more than this far apart in 3D depth are NOT a "close interaction" even if
# their bboxes overlap in the image plane (perspective overlap is not collision risk).
# This gate exists to kill the false-positive class of "cars in different
# lanes/depths that happen to overlap in the 2D image".
VEHICLE_INTER_DISTANCE_GATE_M = 8.0

# Speed-aware risk floor. When ego vehicle is essentially stationary
# (red light, parking, traffic jam) and no track is actively approaching,
# we cap risk at 'medium' regardless of TTC — close-quarters-low-speed is
# normal, not a conflict. Gate exists to kill the false-positive class of
# "traffic-jam proximity" alerts.
LOW_SPEED_FLOOR_MPS = 2.0

# Convergence angle: pair must be closing at > this angle from parallel.
# cos(45°) ≈ 0.71 — only nearly head-on or crossing trajectories pass.
# Gate exists to kill the false-positive class of "same-direction following
# traffic" — two cars in the same lane moving the same direction trigger
# scale-expansion TTC but are not in conflict.
CONVERGE_COS_MAX = 0.35  # cos(70°) — strict, rejects most parallel traffic


# ----- DATACLASSES: Detection + TrackSample -----

# ``@dataclass`` is a Python decorator that auto-generates ``__init__``,
# ``__repr__``, and ``__eq__`` from the class's type-annotated attributes.
# Think of it as a "struct" — a passive record type with named fields.
# The fields below all become constructor parameters in the order listed.
@dataclass
class Detection:
    """One YOLO detection on one frame.

    A ``Detection`` is a pure value object — it has no behaviour beyond a
    few computed properties. All coordinates are in image-pixel space with
    origin at the top-left (OpenCV convention: x grows right, y grows
    *down*).

    Fields:
        cls: YOLO class label (e.g. ``"car"``, ``"person"``).
        conf: Detector confidence in [0, 1]. Higher = YOLO more certain.
        x1, y1: Top-left bbox corner in pixels.
        x2, y2: Bottom-right bbox corner in pixels.
        track_id: ByteTrack identity across frames. ``None`` when the
            tracker hasn't seen enough frames to assign one, or when the
            caller opted out of persistent tracking.

    Used by: every other function in this file, plus ``server.py`` and
    ``services/llm.py`` for event enrichment.
    """
    cls: str
    conf: float
    x1: int
    y1: int
    x2: int
    y2: int
    track_id: int | None = None

    # ``@property`` exposes a computed value as if it were a plain attribute:
    # ``det.center`` not ``det.center()``. Used for cheap derived quantities
    # that don't need to be stored.
    @property
    def center(self) -> tuple[float, float]:
        """(cx, cy) centre point of the bbox in pixels."""
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    @property
    def width(self) -> int:
        """Bbox width in pixels. ``max(..., 1)`` avoids divide-by-zero downstream."""
        return max(self.x2 - self.x1, 1)

    @property
    def height(self) -> int:
        """Bbox height in pixels. ``max(..., 1)`` avoids divide-by-zero downstream."""
        return max(self.y2 - self.y1, 1)

    @property
    def bottom(self) -> int:
        """Bottom edge y-coordinate — the object's foot on the image, used by the
        ground-plane distance prior."""
        return self.y2


@dataclass
class TrackSample:
    """One snapshot of a single track at one point in time.

    A rolling window of these (see ``TrackHistory``) is what TTC is computed
    from. We store only the fields TTC math actually needs — not the full
    ``Detection`` — to keep the ring buffers small.

    Fields:
        t: Wall-clock timestamp (seconds since epoch) of the source frame.
        height: Bbox height in pixels. Used by scale-expansion TTC.
        bottom: Bbox bottom y-coordinate. Used by ground-plane distance.
        cx, cy: Bbox centre in pixels. Used by pair-closing-rate TTC and by
            the convergence-angle filter.
    """
    t: float
    height: int
    bottom: int
    cx: float = 0.0
    cy: float = 0.0


# ----- TRACK HISTORY (rolling windows per track) -----

class TrackHistory:
    """Per-track rolling window of ``TrackSample`` — backs all TTC estimation.

    State
    -----
      ``_tracks``: dict mapping ``track_id -> deque[TrackSample]``. Each
          deque is bounded to ``_maxlen``; when it fills, the oldest sample
          is dropped automatically. This is how we get a "last N frames"
          trailing window for free.

    Lifecycle
    ---------
    One ``TrackHistory`` instance lives for the lifetime of the server /
    analyze run. ``update()`` is called once per detection per frame;
    ``samples()`` is called whenever a TTC estimate is needed;
    ``prune()`` is called periodically to reclaim memory from dead tracks.
    """

    def __init__(self, maxlen: int | None = None):
        """Create an empty track history with the given ring-buffer capacity.

        Args:
            maxlen: Number of samples to keep per track. At 2 fps and
                maxlen=12 this is 6 seconds of history. When ``None`` the
                value is read from the settings store so warm_reload edits
                of ``TRACK_HISTORY_LEN`` take effect on the next
                :meth:`update` call.
        """
        # Type hint on the attribute: a dict from int to deque-of-TrackSample.
        # These hints are documentation; Python does not enforce them at runtime.
        self._tracks: dict[int, deque[TrackSample]] = {}
        if maxlen is None:
            maxlen = int(
                _SETTINGS_STORE.snapshot().get("TRACK_HISTORY_LEN", TRACK_HISTORY_LEN)
            )
        self._maxlen = maxlen
        # Settings Console: register a warm_reload subscriber that resizes
        # every per-track deque in place. ``deque.maxlen`` is read-only, so
        # we rebuild each one preserving the most-recent ``new_max`` items.
        try:
            _SETTINGS_STORE.register_subscriber_for(
                ["TRACK_HISTORY_LEN"],
                self._on_track_history_len_change,
                name=f"TrackHistory@{id(self):x}.resize",
            )
        except Exception:
            # Subscriber is best-effort — not registering it just means the
            # store change won't auto-resize this instance. Tests that swap
            # the store out from under us hit this path.
            pass

    def _on_track_history_len_change(self, before, after) -> None:
        new_max = int(after.get("TRACK_HISTORY_LEN", self._maxlen))
        if new_max == self._maxlen:
            return
        self._maxlen = new_max
        for tid, dq in list(self._tracks.items()):
            self._tracks[tid] = deque(list(dq)[-new_max:], maxlen=new_max)

    def update(self, det: Detection, t: float) -> None:
        """Append a new sample for ``det.track_id`` (no-op if track_id is None).

        Args:
            det: Fresh detection from this frame.
            t: Frame timestamp in seconds.
        """
        if det.track_id is None:
            # Tracker hasn't assigned an ID yet (first few frames) or
            # persistent tracking was disabled. Silently skip — downstream
            # code degrades to per-frame behaviour.
            return
        cx, cy = det.center
        # ``setdefault`` returns the value for this key if present, else
        # inserts the supplied default and returns it. Saves a branch.
        # ``deque(maxlen=...)`` is the fixed-size ring buffer: append past
        # maxlen and the oldest item falls off.
        dq = self._tracks.setdefault(det.track_id, deque(maxlen=self._maxlen))
        dq.append(TrackSample(t=t, height=det.height, bottom=det.bottom, cx=cx, cy=cy))

    def samples(self, track_id: int | None) -> list[TrackSample]:
        """Return the trailing window for ``track_id`` as a fresh list.

        Args:
            track_id: Track to look up. ``None`` yields ``[]``.

        Returns:
            A copy (list) of the current samples. Returns an empty list for
            unknown tracks or ``None`` id — safe to iterate unconditionally.
        """
        if track_id is None:
            return []
        # ``.get(k, ())`` returns the empty tuple if key missing — avoids
        # KeyError and gives us an iterable to pass to ``list()``.
        return list(self._tracks.get(track_id, ()))

    def prune(self, live_ids: set[int], now: float, stale_sec: float = 10.0) -> None:
        """Drop tracks that are no longer live and haven't been seen in ``stale_sec``.

        Without pruning, ``_tracks`` grows unboundedly as new track IDs are
        assigned over hours of operation.

        Args:
            live_ids: Set of track IDs observed on the most recent frame.
            now: Current timestamp in seconds.
            stale_sec: Grace period before dropping a disappeared track. We
                don't drop immediately because ByteTrack can re-acquire a
                briefly occluded track; 10 s is a comfortable re-id window.
        """
        # List comprehension: `[expr for tid, dq in ... if condition]`.
        # Reads as: "make a list of tids where both the dq is empty OR the
        # track hasn't appeared recently, AND the tid isn't live now".
        dead = [
            tid for tid, dq in self._tracks.items()
            if tid not in live_ids and (not dq or now - dq[-1].t > stale_sec)
        ]
        for tid in dead:
            self._tracks.pop(tid, None)


# ----- MODEL LOADING + DISTANCE/GEOMETRY HELPERS -----

def load_model(path: str = MODEL_PATH) -> YOLO:
    """Load a YOLOv8 model from disk (or downloads from ultralytics on first run).

    Selects the best available accelerator at load time:
      * Apple Silicon (M-series): ``mps`` — typically 3-6x faster than CPU
        for YOLOv8s at 640x360.
      * NVIDIA: ``cuda``.
      * Otherwise: CPU.
    Override via ``ROAD_YOLO_DEVICE`` (``cpu`` | ``mps`` | ``cuda`` | ``cuda:0`` …)
    for explicit pinning or to disable MPS if it misbehaves on a given chip.

    Args:
        path: Path to a .pt weights file or a built-in name like ``yolov8n.pt``.
            Defaults to ``MODEL_PATH`` from config (honours ``ROAD_MODEL_PATH``).

    Returns:
        An ``ultralytics.YOLO`` instance ready for ``.track()`` / ``.__call__()``.
    """
    import logging
    import os

    log = logging.getLogger(__name__)
    model = YOLO(path)

    requested = (os.environ.get("ROAD_YOLO_DEVICE") or "").strip().lower()
    device: str | None = None
    try:
        import torch  # type: ignore

        if requested:
            device = requested
        elif torch.cuda.is_available():
            device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"

        if device and device != "cpu":
            model.to(device)
        log.info("YOLO model loaded on device=%s (path=%s)", device, path)
    except Exception as exc:
        # Never block startup on accelerator failure — fall back to CPU and
        # log loudly so the operator can investigate.
        log.warning(
            "YOLO accelerator selection failed (requested=%r), falling back to CPU: %s",
            requested or "auto",
            exc,
        )

    return model


def bbox_edge_distance(a: Detection, b: Detection) -> float:
    """Pixel distance between the *closest edges* of two bboxes (zero if overlapping).

    Args:
        a, b: Two detections.

    Returns:
        Non-negative pixel distance. Zero when the bboxes overlap in both axes.

    Intuition: we don't want centre-to-centre distance because two large
    overlapping bboxes would have a huge centre distance but actually be
    touching. Edge-to-edge answers "how close are they in the image plane".
    """
    # For each axis, ``max(a.lo - b.hi, b.lo - a.hi, 0)`` gives the gap:
    # positive if they don't overlap on that axis, zero if they do.
    dx = max(a.x1 - b.x2, b.x1 - a.x2, 0)
    dy = max(a.y1 - b.y2, b.y1 - a.y2, 0)
    # Pythagoras: sqrt(dx^2 + dy^2). ``** 0.5`` is Python's sqrt operator.
    return (dx * dx + dy * dy) ** 0.5


def estimate_distance_m(
    det: Detection,
    frame_h: int,
    *,
    focal_px: float | None = None,
    height_m: float | None = None,
    horizon_frac: float | None = None,
    offset_m: float = 0.0,
    skip_ground_plane: bool = False,
) -> float | None:
    """Monocular distance estimate for a single detection (depth from camera).

    Strategy — take the more reliable of two priors:
      (a) Ground plane: if the bbox bottom is below the horizon (assumed
          mid-frame), distance = f * H_camera / (y_bottom - y_horizon).
          Skipped when ``skip_ground_plane=True``: side-window cameras do
          not look down the road plane, and feeding the formula a bbox
          below an arbitrary horizon line returns garbage. Side cams
          rely on the known-height prior alone.
      (b) Known-height prior: distance = f * H_real / bbox_height_px.

    We return the larger (more conservative) of the two — preferring to
    under-alert on distance rather than hallucinate a close call.

    Args:
        det: The detection whose distance to estimate.
        frame_h: Height of the full image in pixels (used to locate the horizon).
        focal_px: Per-call override for the camera's focal length in pixels.
            Falls back to the global ``FOCAL_PX`` when ``None``. Pass the
            slot's calibration here to get accurate numbers on a multi-
            camera install (iPhone 0.5× ultra-wide ≈ 260 px, 1× ≈ 600 px).
        height_m: Per-call override for camera mount height in metres.
        horizon_frac: Per-call override for horizon row as fraction of frame
            height (0 = top, 1 = bottom, 0.5 = centre).
        offset_m: Distance in metres from the camera along its optical axis
            to the nearest edge of the ego car. Subtracted from the raw
            pinhole range so the returned number is "gap to my bumper" on
            a front cam. Clamped at 0. (TTC is invariant under this
            constant shift — it's the derivative of distance, not the
            distance itself — so this only affects the absolute reading.)
        skip_ground_plane: When ``True``, suppress the ground-plane prior
            and rely on known-height alone. Set this for any camera whose
            optical axis does NOT run along the road (side-window cams).

    Returns:
        Estimated distance in metres, rounded to 2 decimals. ``None`` when
        neither prior produces a plausible value — e.g. object above the
        horizon (flying?) or a ridiculous >200 m number that would break
        downstream math.

    Intuition: each prior has a different failure mode. Ground plane breaks
    when feet are occluded (elevated camera + foreground clutter) or when
    the camera looks across traffic (side cams). Known height breaks for
    objects at an unusual pose (e.g. a person lying down). Taking the max
    under-reports risk when the priors disagree — the safer choice for a
    system that alerts humans.
    """
    # Resolve per-call overrides with safe fallbacks. Positional ``or`` falls
    # through on ``None`` here (we don't pass 0.0 as a valid focal length).
    f_px = FOCAL_PX if focal_px is None else focal_px
    h_m = CAMERA_HEIGHT_M if height_m is None else height_m
    h_frac = CAMERA_HORIZON_FRAC if horizon_frac is None else horizon_frac

    known_h = TYPICAL_HEIGHT_M.get(det.cls)
    horizon_y = frame_h * h_frac

    dist_height: float | None = None
    # Guard ``det.height > 4``: bbox heights below a few pixels produce
    # absurd distances (division by near-zero pixel count).
    if known_h is not None and det.height > 4:
        dist_height = f_px * known_h / det.height

    dist_ground: float | None = None
    if not skip_ground_plane:
        dy = det.bottom - horizon_y
        # Ground prior only applies below the horizon (object's feet below
        # it). A negative or tiny dy means the object straddles / sits above
        # the horizon.
        if dy > 4:
            dist_ground = f_px * h_m / dy

    # List comprehension with a filter: keep only plausible (0.5 < d < 200 m)
    # values. A tuple ``(x, y)`` is iterated the same way as a list.
    candidates = [d for d in (dist_height, dist_ground) if d is not None and 0.5 < d < 200]
    if not candidates:
        return None
    raw = max(candidates)
    # Apply the camera→car-edge offset so operators read "metres between my
    # bumper and that obstacle", not "metres to the camera glass". Clamp at
    # zero so a close-in object doesn't report a negative distance.
    adjusted = raw - offset_m
    return round(max(0.0, adjusted), 2)


def estimate_distances_batch(
    detections: list[Detection],
    frame_h: int,
    frame: Any | None = None,
    *,
    calibration: CameraCalibration | None = None,
) -> list[float | None]:
    """Compute ego → object distance for every detection in a frame.

    Strategy depends on ``config.DEPTH_MODEL``:

    * ``off``      — returns a list of ``None`` (skip distance compute).
    * ``pinhole``  — default; per-detection pinhole + ground-plane only.
    * ``neural``   — run the neural depth map once for the frame, use the
                      pinhole estimate to scale its arbitrary units into
                      metres, then median-pool each bbox. Falls back to
                      pinhole when the neural model can't load.
    * ``fused``    — run both and keep the larger (more conservative) value.

    Args:
        detections: YOLO detections for this frame.
        frame_h: Frame height in pixels.
        frame: Raw BGR frame; needed only for the ``neural`` / ``fused``
            paths. ``None`` skips the neural branch entirely.
        calibration: Optional per-slot camera intrinsics from
            ``config.camera_calibration_for()``. When supplied, overrides
            the global ``FOCAL_PX`` / ``CAMERA_HEIGHT_M`` /
            ``CAMERA_HORIZON_FRAC`` and subtracts ``bumper_offset_m`` from
            the output so the returned distances are "gap to my car's
            nearest edge" instead of "gap to the camera glass".

    Batching matters because the neural model runs once per FRAME, not
    once per detection — a 16-detection frame pays one inference, not 16.
    """
    from road_safety.config import DEPTH_MODEL
    if DEPTH_MODEL == "off":
        return [None] * len(detections)

    # Pull per-slot intrinsics off the frozen dataclass. When no calibration
    # is threaded through (legacy callers, batch tools) we pass ``None`` to
    # every override and ``estimate_distance_m`` falls back to the globals.
    if calibration is None:
        focal_px = None
        height_m = None
        horizon_frac = None
        offset_m = 0.0
        skip_ground = False
    else:
        focal_px = calibration.focal_px
        height_m = calibration.height_m
        horizon_frac = calibration.horizon_frac
        offset_m = calibration.bumper_offset_m
        # Side-window cams: the ground-plane prior is invalid (the road
        # is not below the optical axis), so we suppress it entirely.
        # Forward / rear cams keep both priors and pick the conservative
        # max as before.
        skip_ground = calibration.orientation == "side"

    pinhole = [
        estimate_distance_m(
            d, frame_h,
            focal_px=focal_px,
            height_m=height_m,
            horizon_frac=horizon_frac,
            offset_m=offset_m,
            skip_ground_plane=skip_ground,
        )
        for d in detections
    ]
    if DEPTH_MODEL == "pinhole" or frame is None:
        return pinhole

    # Defer the import so projects that never enable neural depth don't
    # pay its import cost (torch.hub, numpy allocations, etc).
    from road_safety.core.depth_neural import (
        bbox_depth,
        estimate_relative_depth,
    )

    depth_map = estimate_relative_depth(frame)
    if depth_map is None:
        # Neural path unavailable — degrade silently to pinhole.
        return pinhole

    # Calibrate the relative-depth units to metres. Pick the median ratio
    # of ``pinhole_metres / relative_depth_units`` across detections that
    # have a valid pinhole reading — that's the per-frame scale factor.
    raw_depths: list[float | None] = [
        bbox_depth(depth_map, d.x1, d.y1, d.x2, d.y2) for d in detections
    ]
    ratios = [
        p / r for p, r in zip(pinhole, raw_depths)
        if p is not None and r is not None and r > 1e-6
    ]
    if not ratios:
        # Nothing to calibrate with — fall back to pinhole.
        return pinhole
    scale = sorted(ratios)[len(ratios) // 2]  # median

    out: list[float | None] = []
    for pin, raw in zip(pinhole, raw_depths):
        neural_m = raw * scale if raw is not None else None
        if DEPTH_MODEL == "neural":
            out.append(round(neural_m, 2) if neural_m is not None else pin)
        else:  # fused: pick the more conservative (larger) of the two
            cands = [v for v in (pin, neural_m) if v is not None]
            out.append(round(max(cands), 2) if cands else None)
    return out


def estimate_inter_distance_m(
    a: Detection,
    b: Detection,
    frame_h: int,
    *,
    calibration: CameraCalibration | None = None,
) -> float | None:
    """Rough inter-object distance from monocular depth difference + lateral offset.

    More meaningful than single-object depth for conflict assessment: two cars
    at 15 m depth but in different lanes are not interacting.

    Args:
        a, b: Two detections to measure between.
        frame_h: Image height in pixels.
        calibration: Per-slot camera intrinsics. When supplied the per-
            object depth estimates use the slot's focal/height/horizon and
            apply the slot's bumper offset; lateral pixel→metre conversion
            uses the slot's focal length too. Defaults to the global
            calibration so legacy callers keep working.

    Returns:
        Approximate 3D distance in metres, or ``None`` when either per-object
        depth estimate is missing / the result is implausible.

    Intuition: imagine laying out the two objects on a flat overhead map.
    "Depth difference" is how far apart they are in the direction the camera
    is looking. "Lateral offset" is how far apart they are side-to-side.
    Combining them with Pythagoras gives the straight-line distance between
    them on that overhead map.
    """
    if calibration is None:
        focal_px = None
        height_m = None
        horizon_frac = None
        offset_m = 0.0
        skip_ground = False
        focal_for_lateral = FOCAL_PX
    else:
        focal_px = calibration.focal_px
        height_m = calibration.height_m
        horizon_frac = calibration.horizon_frac
        offset_m = calibration.bumper_offset_m
        skip_ground = calibration.orientation == "side"
        focal_for_lateral = calibration.focal_px

    da = estimate_distance_m(
        a, frame_h,
        focal_px=focal_px, height_m=height_m, horizon_frac=horizon_frac,
        offset_m=offset_m, skip_ground_plane=skip_ground,
    )
    db = estimate_distance_m(
        b, frame_h,
        focal_px=focal_px, height_m=height_m, horizon_frac=horizon_frac,
        offset_m=offset_m, skip_ground_plane=skip_ground,
    )
    if da is None or db is None:
        return None
    depth_diff = abs(da - db)

    # Convert pixel x-separation to metres using the pinhole formula
    # reversed: ``metres = pixels * depth / focal``. We use the *average*
    # depth because lateral offset is approximately the same at both objects
    # when they're near each other.
    avg_depth = (da + db) / 2.0
    acx, acy = a.center
    bcx, bcy = b.center
    lateral_px = abs(acx - bcx)
    lateral_m = lateral_px * avg_depth / focal_for_lateral if avg_depth > 0 else 0

    # Pythagoras in 2D (overhead map): total = sqrt(depth^2 + lateral^2).
    inter = math.sqrt(depth_diff ** 2 + lateral_m ** 2)
    # Ternary: ``value if cond else other``. Filters absurd values the same
    # way estimate_distance_m does.
    return round(inter, 2) if 0.3 < inter < 200 else None


# ----- TTC (TIME-TO-COLLISION) MATH -----

def _is_monotonic_increasing(seq: list[float]) -> bool:
    """True if seq is monotonically non-decreasing, allowing one transient
    dip to absorb tracker bbox jitter. Stricter than 'last > first': a
    zigzag pattern is rejected even if the endpoints satisfy the inequality.

    Args:
        seq: A list of numbers (bbox heights over time, typically).

    Returns:
        ``False`` if fewer than 2 samples or if more than one "dip" (value
        that is less than the previous one) occurs. Otherwise ``True``.

    Intuition: "is this sequence basically going up, ignoring a single hiccup?".
    We tolerate one dip because bbox heights jitter by ~1 px frame-to-frame —
    requiring strict monotonicity would reject real approaches.
    ``zip(seq, seq[1:])`` is the idiomatic way to iterate adjacent pairs:
    pairs up each element with the next one.
    """
    if len(seq) < 2:
        return False
    dips = 0
    for prev, curr in zip(seq, seq[1:]):
        if curr < prev:
            dips += 1
            if dips > 1:
                return False
    return True


def _is_monotonic_decreasing(seq: list[float]) -> bool:
    """Mirror of ``_is_monotonic_increasing`` — tolerates one bump upward.

    Used to require that a pair of tracks' centre-distance is steadily
    shrinking (they're converging) before publishing a pair-TTC.
    """
    if len(seq) < 2:
        return False
    bumps = 0
    for prev, curr in zip(seq, seq[1:]):
        if curr > prev:
            bumps += 1
            if bumps > 1:
                return False
    return True


def estimate_ttc_sec(history: list[TrackSample]) -> float | None:
    """Time-to-collision from bbox-scale growth (scale-expansion TTC).

    Sustained-evidence gates (a 1-pixel bbox jitter on a stationary object
    can otherwise produce TTC < 1 s from noise alone):

      Gate 1: ≥ TTC_REQUIRED_SAMPLES samples, ≥ 1.5 s window.
      Gate 2: bbox heights MONOTONICALLY increasing (zigzag rejected).
      Gate 3: absolute height delta exceeds TTC_MIN_HEIGHT_DELTA_PX (jitter floor).
      Gate 4: track shows non-trivial center motion (rejects stationary jitter).
      Gate 5: full-window scale ratio exceeds MIN_SCALE_GROWTH.

    Only after all five gates pass is TTC = Δt / (scale - 1) returned.
    Ego-motion-invariant in the longitudinal axis.

    Args:
        history: Trailing window of samples for a single track (from
            ``TrackHistory.samples``).

    Returns:
        Time-to-collision in seconds (clipped to (0, 30]) — or ``None`` if
        *any* gate fails, including insufficient samples / too-short window /
        non-monotonic growth / jitter-level delta / stationary track /
        insufficient scale growth / non-finite or nonsensical result.

    Intuition: if an object's apparent size doubles in 1 second, it will
    reach you in 1 more second (at constant approach speed). Mathematically:
    ``TTC = dt / (scale - 1)`` where scale is (new_height / old_height).
    All the gates exist because on jittery inputs this formula happily
    produces TTC=0.3s on a stationary parked car.
    """
    if len(history) < TTC_REQUIRED_SAMPLES:
        return None
    # ``history[-TTC_REQUIRED_SAMPLES:]`` is Python slice syntax for
    # "the last N elements" — equivalent to ``history[len-N:len]``.
    window = history[-TTC_REQUIRED_SAMPLES:]
    first, last = window[0], window[-1]
    dt = last.t - first.t
    # Gate 1 (window duration): at 2 fps, 4 samples could span only ~1.5 s.
    # Under this we don't have enough time to tell growth from jitter.
    if dt < 1.5:
        return None

    # Gate 2: monotonic growth — single noisy frame can't fire TTC alone.
    # The list comprehension ``[float(s.height) for s in window]`` builds
    # a list of heights, cast to float so downstream comparisons are consistent.
    if not _is_monotonic_increasing([float(s.height) for s in window]):
        return None

    # Gate 3: absolute pixel delta must clear jitter floor.
    if (last.height - first.height) < TTC_MIN_HEIGHT_DELTA_PX:
        return None

    # Gate 4: track must move across the window — sub-pixel jitter on a
    # stationary bbox is not a conflict.
    # ``math.hypot(dx, dy)`` = sqrt(dx² + dy²), numerically stabler than
    # hand-rolling Pythagoras.
    motion_px = math.hypot(last.cx - first.cx, last.cy - first.cy)
    if motion_px < TTC_MIN_TRACK_MOTION_PX:
        return None

    # Gate 5: scale ratio.
    # ``max(first.height, 1)`` guards divide-by-zero (bbox of zero height).
    scale = last.height / max(first.height, 1)
    _msg = float(_SETTINGS_STORE.snapshot().get("MIN_SCALE_GROWTH", MIN_SCALE_GROWTH))
    if scale <= _msg:
        return None

    # The actual TTC formula. ``scale - 1.0`` is the fractional growth per
    # window duration dt; ``dt / (scale-1)`` is how long it takes to double
    # again at the same rate — equivalent to TTC at constant approach.
    ttc = dt / (scale - 1.0)
    # Sanity: reject infinity, NaN, negative, or "30+ seconds away" (not
    # actionable, downstream treats this as no-risk).
    if not math.isfinite(ttc) or ttc <= 0 or ttc > 30:
        return None
    return round(ttc, 2)


def estimate_pair_ttc(
    hist_a: list[TrackSample], hist_b: list[TrackSample],
) -> float | None:
    """Pair-wise TTC from the closing rate of two tracks' centres (SSAM method).

    Sustained-evidence gates:
      Gate 1: ≥ PAIR_TTC_REQUIRED_SAMPLES samples per track, ≥ 1.5 s overlap.
      Gate 2: pair-distance MONOTONICALLY decreasing across the window.
      Gate 3: total distance reduction exceeds PAIR_TTC_MIN_DISTANCE_REDUCTION_PX.
      Gate 4: at least ONE track shows non-trivial motion (two stationary
              bboxes with center jitter cannot have a real closing rate).
      Gate 5: closing_rate > MIN_CLOSING_RATE_PX.

    Returns TTC = D / (-dD/dt) only when all five gates pass.

    Args:
        hist_a: Trailing samples for the first track.
        hist_b: Trailing samples for the second track.

    Returns:
        Pair time-to-collision in seconds, or ``None`` if any gate fails.

    Intuition: forget each object's individual motion — just watch the
    *distance between them*. If the gap is shrinking at 20 px/s and the
    current gap is 40 px, they meet in 2 seconds. This is preferred over
    scale-expansion TTC because it naturally rejects same-direction
    traffic (no gap shrinkage) and works for pedestrian-vehicle crossings
    where scale expansion is ambiguous.
    """
    if len(hist_a) < PAIR_TTC_REQUIRED_SAMPLES or len(hist_b) < PAIR_TTC_REQUIRED_SAMPLES:
        return None

    # Align windows to the same trailing length so per-step distances make sense.
    n = min(len(hist_a), len(hist_b), PAIR_TTC_REQUIRED_SAMPLES)
    wa = hist_a[-n:]
    wb = hist_b[-n:]

    a0, a1 = wa[0], wa[-1]
    b0, b1 = wb[0], wb[-1]
    # Use the SHORTER of the two per-track windows so we don't claim
    # temporal coverage we don't actually have.
    dt = min(a1.t - a0.t, b1.t - b0.t)
    if dt < 1.5:
        return None

    # Gate 2 + 3: monotonic decrease + minimum total reduction.
    # ``zip(wa, wb)`` pairs up samples at matching positions in each window.
    distances = [math.hypot(sa.cx - sb.cx, sa.cy - sb.cy) for sa, sb in zip(wa, wb)]
    if not _is_monotonic_decreasing(distances):
        return None
    if (distances[0] - distances[-1]) < PAIR_TTC_MIN_DISTANCE_REDUCTION_PX:
        return None

    # Gate 4: at least one track must show real motion. Two parked cars'
    # centers will always jitter a few pixels — that's not a closing rate.
    motion_a = math.hypot(a1.cx - a0.cx, a1.cy - a0.cy)
    motion_b = math.hypot(b1.cx - b0.cx, b1.cy - b0.cy)
    if motion_a < TTC_MIN_TRACK_MOTION_PX and motion_b < TTC_MIN_TRACK_MOTION_PX:
        return None

    # Average closing rate over the window. Robust to frame-to-frame jitter
    # because we're measuring endpoint-to-endpoint, not instantaneous slope.
    closing_rate = (distances[0] - distances[-1]) / dt
    if closing_rate <= MIN_CLOSING_RATE_PX:
        return None
    # Already closed: TTC is meaningless (or they've collided).
    if distances[-1] <= 0:
        return None

    # D / (closing rate) = seconds until D reaches zero.
    ttc = distances[-1] / closing_rate
    if not math.isfinite(ttc) or ttc <= 0 or ttc > 30:
        return None
    return round(ttc, 2)


def tracks_converging(hist_a: list[TrackSample], hist_b: list[TrackSample]) -> bool:
    """Return True if two tracks' velocity vectors are converging (not parallel).

    Rejects same-direction, same-lane "following" traffic which produces low TTC
    from scale growth but is not a conflict. Requires ≥5 samples to avoid
    triggering on noisy short tracks.

    Args:
        hist_a, hist_b: Trailing samples for each track.

    Returns:
        ``True`` when the angle between the two tracks' velocity vectors is
        steep enough (dot-product of unit vectors < ``CONVERGE_COS_MAX``).
        Default-safe: insufficient data or near-stationary tracks → ``False``.

    Intuition: dot product of two unit vectors equals the cosine of the
    angle between them. Two cars moving in exactly the same direction have
    cos=1.0; perpendicular cars cos=0; head-on cos=-1. A threshold around
    cos(70°) ≈ 0.35 says "must be at least 70 degrees off parallel to count".
    """
    if len(hist_a) < 5 or len(hist_b) < 5:
        return False  # insufficient data → default to safe (reject)

    a0, a1 = hist_a[0], hist_a[-1]
    b0, b1 = hist_b[0], hist_b[-1]

    # Velocity vectors in pixel space (displacement over the full window).
    # Tuples are immutable lists — lightweight and hashable.
    va = (a1.cx - a0.cx, a1.cy - a0.cy)
    vb = (b1.cx - b0.cx, b1.cy - b0.cy)

    # ``*va`` unpacks the 2-tuple as positional args to hypot(x, y).
    mag_a = math.hypot(*va)
    mag_b = math.hypot(*vb)
    # A track that barely moved has no meaningful direction. 3 px over 6
    # seconds (our default window at 2 fps × 12 samples) is sub-pixel noise.
    if mag_a < 3.0 or mag_b < 3.0:
        return False  # nearly stationary objects rarely collide at intersections

    # Dot product of unit vectors = cos(angle). Negative values mean the
    # vectors are opposing (head-on) — comfortably below CONVERGE_COS_MAX.
    dot = (va[0] * vb[0] + va[1] * vb[1]) / (mag_a * mag_b)
    return dot < CONVERGE_COS_MAX


# ----- RISK CLASSIFICATION + YOLO INFERENCE WRAPPER -----

def classify_risk(ttc_sec: float | None, distance_m: float | None, fallback_px: float) -> str:
    """Physical-unit risk classification with graceful pixel fallback.

    Priority: TTC > distance_m > pixel heuristic. Any signal triggering the
    'high' band wins; the output is the worst (most severe) of the signals.

    Args:
        ttc_sec: Time-to-collision in seconds, or ``None`` if unavailable.
        distance_m: Inter-object distance in metres, or ``None``.
        fallback_px: Edge-to-edge bbox distance in pixels — used only when
            both physical signals are absent (e.g. camera not calibrated).

    Returns:
        One of ``"high"``, ``"medium"``, ``"low"``. Never raises; missing
        inputs simply fall back to the next tier.

    Intuition: we bucket into three tiers because operators cannot act on a
    continuous 0.0-1.0 score in real-time. High → immediate Slack ping.
    Medium → dashboard highlight. Low → logged but silent.
    """
    # Accumulate every tier this event qualifies for; then pick the worst.
    # Simpler than nested if/elif chains and handles the "TTC says medium,
    # distance says high" case correctly (worst-signal wins).
    cfg = _SETTINGS_STORE.snapshot()
    ttc_high = float(cfg.get("TTC_HIGH_SEC", TTC_HIGH_SEC))
    ttc_med = float(cfg.get("TTC_MED_SEC", TTC_MED_SEC))
    dist_high = float(cfg.get("DIST_HIGH_M", DIST_HIGH_M))
    dist_med = float(cfg.get("DIST_MED_M", DIST_MED_M))
    levels = []
    if ttc_sec is not None:
        if ttc_sec <= ttc_high:
            levels.append("high")
        elif ttc_sec <= ttc_med:
            levels.append("medium")
    if distance_m is not None:
        if distance_m <= dist_high:
            levels.append("high")
        elif distance_m <= dist_med:
            levels.append("medium")
    # Pixel fallback: only consulted when *both* physical signals are absent
    # (e.g. brand-new track with no history, or camera calibration missing).
    # Numeric cutoffs are per-camera noisy but better than silent "low".
    if ttc_sec is None and distance_m is None:
        if fallback_px <= 20:
            levels.append("high")
        elif fallback_px <= 80:
            levels.append("medium")

    if "high" in levels:
        return "high"
    if "medium" in levels:
        return "medium"
    return "low"


def detect_frame(model: YOLO, frame, persistent: bool = True) -> list[Detection]:
    """Run YOLO tracking on a frame and return filtered Detection objects.

    ``persistent=True`` keeps track IDs stable across calls — required for TTC
    and per-pair dedup. Set False only for isolated single-frame calls.

    Args:
        model: A loaded YOLO instance from ``load_model``.
        frame: The image as a numpy ndarray (H, W, 3) BGR — same format that
            ``StreamReader`` produces.
        persistent: When True, uses ByteTrack for stable track IDs across
            frames (the normal mode). When False, runs detection only —
            all returned ``Detection.track_id`` values will be ``None``.

    Returns:
        A list of ``Detection`` objects, possibly empty. Filtered to:
          - known classes (vehicles + pedestrians)
          - class-specific confidence ≥ floor
          - class-specific bbox area ≥ floor
          - persons with plausible aspect ratio (rejects wide "person" blobs
            that are usually occlusion artefacts or mislabelled objects)

    Intuition: this is the ingest funnel. Everything downstream assumes
    these filters have already run, so adding a new class or loosening a
    floor here has wide blast radius — run ``tests/test_core.py``.
    """
    if persistent:
        # ``model.track(...)`` keeps per-call state between invocations.
        # ``[0]`` unwraps the batch — we pass one frame, get one result.
        results = model.track(
            frame, persist=True, tracker=TRACKER_CFG, verbose=False
        )[0]
    else:
        results = model(frame, verbose=False)[0]

    # ``results.names`` is a dict ``{class_int: class_name}`` from the model.
    names = results.names
    # Explicit type hint on the local list so new contributors see the shape.
    out: list[Detection] = []
    boxes = results.boxes
    if boxes is None:
        return out

    cfg = _SETTINGS_STORE.snapshot()
    veh_conf_floor = float(cfg.get("CONF_THRESHOLD", CONF_THRESHOLD))
    person_conf_floor = float(cfg.get("PERSON_CONF_THRESHOLD", PERSON_CONF_THRESHOLD))
    veh_area_floor = int(cfg.get("MIN_BBOX_AREA", MIN_BBOX_AREA))

    # ByteTrack assigns IDs *after* a few confirmed sightings. On the first
    # frame(s) ids is None — we fall back to a list of Nones so the zip
    # below still pairs up correctly.
    ids = boxes.id.int().tolist() if boxes.id is not None else [None] * len(boxes)
    for box, tid in zip(boxes, ids):
        cls = names[int(box.cls)]
        # Skip anything that isn't a vehicle or pedestrian (e.g. traffic
        # lights, backpacks). We only model conflict between those two groups.
        if cls not in VEHICLE_CLASSES and cls not in PEDESTRIAN_CLASSES:
            continue
        conf = float(box.conf)
        # Class-specific confidence floor: persons are smaller + harder for
        # YOLOv8n and legitimately score lower than vehicles.
        conf_floor = person_conf_floor if cls in PEDESTRIAN_CLASSES else veh_conf_floor
        if conf < conf_floor:
            continue
        x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
        w, h = x2 - x1, y2 - y1
        # Class-specific bbox-area floor: a distant pedestrian is legitimately
        # smaller on-screen than the smallest car we care about.
        area_floor = PERSON_MIN_BBOX_AREA if cls in PEDESTRIAN_CLASSES else veh_area_floor
        if w * h < area_floor:
            continue
        # GATE: person aspect ratio. A human standing upright is tall and
        # narrow (w/h ~ 0.3-0.5). Wide "person" bboxes (w/h > 0.7) are
        # almost always occlusion bleed (person + adjacent object merged)
        # or YOLO misclassifying a reclining object as a person. Rejecting
        # them kills a specific class of weird false-positive from crowded
        # urban scenes.
        if cls == "person" and h > 0 and w / h > 0.7:
            continue
        out.append(Detection(cls=cls, conf=conf, x1=x1, y1=y1, x2=x2, y2=y2, track_id=tid))
    return out


# ----- INTERACTION FINDER + ANNOTATION + EVENT SUMMARY -----

def find_interactions(
    detections: list[Detection],
) -> list[tuple[str, Detection, Detection, float]]:
    """Generate candidate (type, a, b, pixel_dist) tuples for every pair worth considering.

    Args:
        detections: All Detection objects from the current frame.

    Returns:
        A list of 4-tuples ``(event_type, primary, secondary, edge_pixel_distance)``.
        Event types:
          - ``"pedestrian_proximity"`` — any ped within 120 px of any vehicle.
          - ``"vehicle_close_interaction"`` — any two vehicles within 30 px
            of each other AND mean confidence ≥ ``VEHICLE_PAIR_CONF_FLOOR``.

    Intuition: this is a *coarse* gate. The output is not alerts — it's the
    set of pairs worth running the expensive downstream math on (TTC,
    inter-distance, convergence, etc.). The 120 px / 30 px thresholds are
    generous on purpose: we'd rather evaluate a pair and reject it than
    miss a real conflict.

    Complexity: O(peds × vehicles) + O(vehicles²). At typical dashcam scene
    scales (<30 objects) this is negligible. Consider spatial hashing if
    detection counts grow.
    """
    # List comprehensions to split detections by class — cheaper than two
    # passes with append() and more idiomatic.
    pedestrians = [d for d in detections if d.cls in PEDESTRIAN_CLASSES]
    vehicles = [d for d in detections if d.cls in VEHICLE_CLASSES]
    candidates = []

    # Pedestrian × vehicle cross product. The 120 px cutoff is coarse
    # because ped/vehicle conflicts often need some breathing room (the
    # person may not be right next to the car yet but is crossing in).
    for ped in pedestrians:
        for veh in vehicles:
            dist = bbox_edge_distance(ped, veh)
            if dist <= 120:
                candidates.append(("pedestrian_proximity", ped, veh, dist))

    # Vehicle × vehicle pairs (each unordered pair exactly once via i+1:).
    # ``enumerate`` yields ``(index, item)`` tuples.
    for i, a in enumerate(vehicles):
        for b in vehicles[i + 1 :]:
            # Pair confidence floor — stricter than per-detection.
            # Kills the false-positive class of "two low-conf vehicle
            # blobs that happen to overlap".
            mean_conf = (a.conf + b.conf) / 2.0
            _floor = float(_SETTINGS_STORE.snapshot().get("VEHICLE_PAIR_CONF_FLOOR", VEHICLE_PAIR_CONF_FLOOR))
            if mean_conf < _floor:
                continue
            dist = bbox_edge_distance(a, b)
            # 30 px is tight because vehicle-vehicle actual collisions show
            # overlapping bboxes — we want real near-contact, not "in the
            # same part of the frame".
            if dist <= 30:
                candidates.append(("vehicle_close_interaction", a, b, dist))

    return candidates


def draw_thumbnail(frame, primary: Detection, secondary: Detection, path: Path) -> None:
    """Annotate a frame with two bboxes + labels and save it to disk.

    Args:
        frame: The source BGR image (numpy ndarray).
        primary: Drawn in red — the "focus" object of the event.
        secondary: Drawn in yellow — the other party.
        path: Where to save the annotated JPEG/PNG (format inferred from ext).

    Returns:
        ``None``. Side effect is a file written at ``path``.

    Note:
        This draws on a *copy* of the frame (``frame.copy()``) so the
        original numpy buffer is not mutated — important because the caller
        may still be using it for other things.
    """
    annotated = frame.copy()
    # Iterate over (detection, BGR colour) pairs. OpenCV uses BGR, not RGB:
    # (0, 0, 255) = pure red; (0, 200, 255) = amber.
    for det, color in [(primary, (0, 0, 255)), (secondary, (0, 200, 255))]:
        cv2.rectangle(annotated, (det.x1, det.y1), (det.x2, det.y2), color, 2)
        # f-string: ``{det.conf:.2f}`` formats as 2-decimal float.
        label = f"{det.cls} {det.conf:.2f}"
        if det.track_id is not None:
            label = f"#{det.track_id} {label}"
        cv2.putText(
            annotated,
            label,
            # ``max(det.y1 - 6, 12)``: put the label just above the bbox, but
            # clamp to y=12 so it doesn't render off the top of the image.
            (det.x1, max(det.y1 - 6, 12)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,         # font scale
            color,
            1,           # thickness
            cv2.LINE_AA, # anti-aliased strokes
        )
    # ``cv2.imwrite`` requires str paths, not pathlib.Path.
    cv2.imwrite(str(path), annotated)


_ORIENTATION_LOCATION_PHRASES: dict[str, str] = {
    # Used as the prefix / location hint in the templated summary. Keeps the
    # fallback prose honest about WHERE the risk is when the LLM narrator is
    # disabled. Forward cams stay silent on location — the existing demo
    # copy ("Car and person ~3.2m apart") already implies "ahead".
    "forward": "",
    "rear": "behind (reversing): ",
    "side": "in blind spot: ",
}


def build_event_summary(
    event_type: str,
    a: Detection,
    b: Detection,
    distance: float,
    risk: str,
    ttc_sec: float | None = None,
    distance_m: float | None = None,
    camera_orientation: str | None = None,
    event_taxonomy: str | None = None,
) -> str:
    """Render a short human-readable sentence describing an interaction event.

    Args:
        event_type: Interaction type string (currently unused in output, but
            reserved for future templating variants).
        a, b: The two detections involved.
        distance: Edge-to-edge pixel distance (fallback when metres missing).
        risk: Risk bucket string (``"high"`` / ``"medium"`` / ``"low"``).
        ttc_sec: Optional time-to-collision in seconds.
        distance_m: Optional inter-object distance in metres.
        camera_orientation: ``"forward"``/``"rear"``/``"side"``. Prepends a
            location hint (``"behind (reversing): ..."`` / ``"in blind spot: ..."``)
            so the templated summary reads correctly on side / rear cams.
            ``None`` (legacy callers) stays silent on location, preserving
            the original forward-cam phrasing byte-for-byte.
        event_taxonomy: Optional SAE J3063 family label (``"FCW"`` / ``"BSW"``
            / ``"RCW"`` / ``"RCTA"``). Reserved for future templating; not
            currently rendered in the string.

    Returns:
        A one-line human summary like
        ``"Car and person ~3.2m apart TTC 0.8s (risk=high)."``.

    Intuition: this is what an operator sees in the incident queue and
    what gets templated into Slack messages / audit log entries. Metric
    units are preferred over pixels when available — pixels mean nothing
    to a human reader.
    """
    # Start with the classes; ``.title()`` capitalises the first letter.
    # Build a list of fragments, then join with spaces at the end — cheaper
    # than string concatenation in a loop.
    parts = [f"{a.cls.title()} and {b.cls}"]
    if distance_m is not None:
        parts.append(f"~{distance_m:.1f}m apart")
    else:
        parts.append(f"within {int(distance)}px")
    if ttc_sec is not None:
        parts.append(f"TTC {ttc_sec:.1f}s")
    parts.append(f"(risk={risk}).")
    body = " ".join(parts)
    prefix = _ORIENTATION_LOCATION_PHRASES.get(camera_orientation or "forward", "")
    return f"{prefix}{body}" if prefix else body
