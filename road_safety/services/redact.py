"""Pre-egress PII redaction.

Role
----
Every thumbnail that leaves the host (Slack, optional image relay, outbound
webhook, LLM vision call to anyone other than the internal ALPR pass) must
go through ``redact_for_egress()`` first. License plates are PII under
GDPR Art. 4 and enumerated PI under CCPA; faces are biometric PII under
most regimes.

Dual-thumbnail design (CORE INVARIANT)
--------------------------------------
Each event produces TWO thumbnail files on disk:
  - ``{event_id}.jpg``        - internal, UNREDACTED, local disk only. Used
                                for the internal ALPR vision call and for
                                DSAR retrieval by operators holding an
                                ``X-DSAR-Token``.
  - ``{event_id}_public.jpg`` - redacted (faces and plates blurred). Safe
                                for egress to Slack, the cloud receiver,
                                SSE subscribers, and any outbound webhook.

Any shared channel MUST emit only the ``_public`` variant. The
``write_thumbnails()`` helper below writes both at once; callers should
reference the paths via ``public_thumbnail_name()`` so they never
accidentally grab the internal copy.

Redaction heuristics
--------------------
  - Plate boxes: we don't ship a dedicated plate detector, so plates are
    approximated as the LOWER-MIDDLE strip of each vehicle bbox with a
    horizontal inset. This over-blurs slightly (may blur bumper stickers,
    tail lights, etc.). That is the correct failure mode: in privacy,
    false-redact > false-leak.
  - Face boxes: upper ~35% of every ``person`` bbox.
  - Plate TEXT (from the Anthropic ALPR pass) never leaves this process in
    raw form; ``hash_plate`` emits a salted hash so downstream consumers
    can correlate "same vehicle seen again" without storing the plate.

Python idioms in this file
--------------------------
- ``hashlib.sha256(bytes).hexdigest()`` : cryptographic hash used with a
  per-deployment salt so hashes don't correlate across operators.
- ``cv2.GaussianBlur`` : separable-kernel blur from OpenCV. The kernel
  size must be ODD (``cv2`` enforces this) - see ``_blur_roi``.
- ``cv2.imwrite`` : writes a BGR numpy array to disk as JPEG/PNG/etc.
- ``frame.copy()`` : deep-copy of a numpy image array - needed because
  we must not mutate the caller's frame.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import cv2

from road_safety.config import PLATE_SALT
from road_safety.core.detection import Detection, PEDESTRIAN_CLASSES, VEHICLE_CLASSES

# ``PLATE_SALT`` comes from env via ``road_safety.config``; per-deployment
# so hashes don't correlate across operators (you can't merge two
# customers' plate-hash streams into an identifying dataset).
_PLATE_SALT = PLATE_SALT

# -----------------------------------------------------------------------------
# GEOMETRIC BAND CONSTANTS
# Expressed as fractions of the detection bbox height/width so the bands
# scale automatically with detection size. These numbers were picked from
# empirical sampling of dashcam frames; adjust with care - the goal is to
# always OVER-cover real plates/faces, never under-cover.
# -----------------------------------------------------------------------------
# Class-coloured palette for the "context" (non-pair) detections drawn
# in the public thumbnail. Mirrors the live admin tile's colour map in
# server.py::_render_annotated_frame so reviewers see the same colours
# whether they're looking at the live feed or the post-hoc thumbnail.
# OpenCV is BGR.
_CONTEXT_COLOR_MAP: dict[str, tuple[int, int, int]] = {
    "person": (0, 220, 100),
    "car": (255, 160, 0),
    "truck": (255, 100, 0),
    "bus": (200, 80, 200),
    "motorcycle": (0, 180, 255),
    "bicycle": (0, 180, 255),
}

FACE_BAND_TOP = 0.00     # face = top ~35% of person bbox
FACE_BAND_BOTTOM = 0.35
PLATE_BAND_TOP = 0.55    # plate = lower-middle strip of vehicle bbox
PLATE_BAND_BOTTOM = 0.95
PLATE_BAND_X_INSET = 0.15  # crop horizontal margins - skips wheel wells


def _blur_roi(frame, x1: int, y1: int, x2: int, y2: int, ksize: int = 41) -> None:
    """Apply a Gaussian blur IN PLACE to a rectangular region of ``frame``.

    ``frame`` is a numpy array (HxWx3 BGR); slicing it with ``[y1:y2,
    x1:x2]`` returns a VIEW into the same memory, so the assignment
    ``frame[...] = cv2.GaussianBlur(...)`` writes the blur back into the
    original image.

    Args
    ----
    frame : numpy.ndarray
        Image to modify. Mutated.
    x1, y1, x2, y2 : int
        Region in pixel coords. Clamped to image bounds.
    ksize : int
        Gaussian kernel size. Larger = more blur. Must be ODD for OpenCV -
        we add 1 if the caller passed an even number. 41 is the default
        strong blur; callers pass 31 (faces) or 25 (plates) which are
        still firmly unreadable while preserving surrounding context.

    Returns None. Silently no-ops for regions smaller than 4x4 px.
    """
    h, w = frame.shape[:2]
    # Clamp coords into image bounds so we never slice past the edges.
    x1 = max(0, min(x1, w - 1))
    x2 = max(0, min(x2, w))
    y1 = max(0, min(y1, h - 1))
    y2 = max(0, min(y2, h))
    if x2 - x1 < 4 or y2 - y1 < 4:
        # Too small to usefully blur - skip rather than crash on cv2.
        return
    roi = frame[y1:y2, x1:x2]
    # OpenCV requires an odd kernel size. Round up if necessary.
    k = ksize if ksize % 2 == 1 else ksize + 1
    frame[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (k, k), 0)


def _face_band(det: Detection) -> tuple[int, int, int, int]:
    """Compute the face redaction rectangle for a ``person`` detection.

    Spans the full width of the bbox and the upper ``FACE_BAND_BOTTOM``
    fraction of its height. Returns ``(x1, y1, x2, y2)``.
    """
    h = det.height
    return (
        det.x1,
        det.y1 + int(h * FACE_BAND_TOP),
        det.x2,
        det.y1 + int(h * FACE_BAND_BOTTOM),
    )


def _plate_band(det: Detection) -> tuple[int, int, int, int]:
    """Compute the plate redaction rectangle for a vehicle detection.

    A horizontal strip across the lower-middle of the bbox, inset from
    the sides so we don't blur wheels and tail lights unnecessarily.
    Deliberately over-covers real plate positions - the correct failure
    mode is to blur too much, not too little.
    """
    h = det.height
    w = det.width
    xi = int(w * PLATE_BAND_X_INSET)
    return (
        det.x1 + xi,
        det.y1 + int(h * PLATE_BAND_TOP),
        det.x2 - xi,
        det.y1 + int(h * PLATE_BAND_BOTTOM),
    )


def redact_for_egress(frame, detections: list[Detection]):
    """Return a blurred COPY of ``frame`` safe for third-party egress.

    Walks every detection and blurs the appropriate region:
      - pedestrians -> face band (ksize=31, strong blur)
      - vehicles    -> plate band (ksize=25, strong blur)

    Args
    ----
    frame : numpy.ndarray
        Original frame. NOT mutated - we work on ``frame.copy()`` so the
        caller keeps an untouched reference for internal use.
    detections : list[Detection]
        ALL detections in the frame - not just the primary/secondary
        pair. Every vehicle and pedestrian visible gets redacted, not
        only the ones implicated in the current event.

    Returns
    -------
    numpy.ndarray
        A new array safe to imwrite and ship off-host.
    """
    out = frame.copy()
    for det in detections:
        if det.cls in PEDESTRIAN_CLASSES:
            x1, y1, x2, y2 = _face_band(det)
            # ksize=31 for faces: slightly smaller than plates because
            # face bands are wider and we want full unreadability without
            # smearing into skin-tone neighbours.
            _blur_roi(out, x1, y1, x2, y2, ksize=31)
        elif det.cls in VEHICLE_CLASSES:
            x1, y1, x2, y2 = _plate_band(det)
            # ksize=25 for plates: tight enough that the blur artifact
            # doesn't bleed into headlights/bumpers that downstream
            # reviewers may need to see for context.
            _blur_roi(out, x1, y1, x2, y2, ksize=25)
    return out


def write_thumbnails(
    frame,
    detections: list[Detection],
    primary: Detection,
    secondary: Detection,
    internal_path: Path,
    public_path: Path,
) -> None:
    """Write both the internal (with bboxes) and egress-safe (redacted) thumbs.

    =========================================================================
    !!!  DUAL-THUMBNAIL INVARIANT - READ BEFORE USING THESE OUTPUTS  !!!
    -------------------------------------------------------------------------
    After this function returns, the event has TWO on-disk artefacts:

      internal_path  ->  UNREDACTED thumbnail with primary/secondary
                         bboxes drawn. Stays on the local host. ONLY
                         readers: (a) the internal ALPR vision call in
                         ``enrich_event``, (b) operators holding a valid
                         ``X-DSAR-Token`` accessing ``/api/thumbs/...``.

      public_path    ->  REDACTED thumbnail (faces + plates blurred) with
                         bboxes redrawn on top of the blurred copy. This
                         is the ONLY variant permitted on shared channels:
                         SSE, Slack, cloud receiver, dashboards, outbound
                         webhooks, email, anywhere.

    If you are writing a new code path that shares an image anywhere
    beyond the host process, reference ``public_path`` / use
    ``public_thumbnail_name()``. Never pass ``internal_path`` to a
    shared channel.
    =========================================================================

    Ordering rule
    -------------
    The redaction step operates on the RAW frame, BEFORE any annotation.
    Do not draw labels and then redact - labels can contain PII (e.g.
    plate text from an ALPR overlay) and once burned into the pixels no
    Gaussian blur will scrub them.

    Args
    ----
    frame : numpy.ndarray
        Raw frame from the capture loop.
    detections : list[Detection]
        All detections; everything that is a person or vehicle gets
        redacted (not only the ``primary``/``secondary`` pair).
    primary, secondary : Detection
        The two tracks implicated in the event; rendered with red and
        yellow bboxes on both thumbs so the reviewer / model can tell
        which vehicles the event is about.
    internal_path, public_path : Path
        File paths to write. Parent directories must already exist.
    """
    from road_safety.core.detection import draw_thumbnail

    # INTERNAL: full-fidelity, bboxes drawn by the detection module's own
    # annotator (which uses its own label format).
    draw_thumbnail(frame, primary, secondary, internal_path)

    # PUBLIC: blur first on the raw frame, then draw fresh (label-free)
    # bboxes so the reviewer still knows which cars are implicated.
    redacted = redact_for_egress(frame, detections)
    # Identify the pair via object identity so we can draw "everyone else"
    # in a thinner, neutral colour underneath the highlighted pair. Without
    # this all non-pair detections were invisible in the public thumb,
    # which made every event look like "two objects in an empty scene"
    # even when the camera saw a busy intersection.
    pair_ids = {id(primary), id(secondary)}
    others = [d for d in detections if id(d) not in pair_ids]
    for det in others:
        # Class-coloured box for context detections. Same palette the live
        # admin tile uses (_render_annotated_frame). 2-px thickness because
        # 1-px lines disappear into the background once JPEG compression
        # smudges them on a 1920x1080 thumbnail viewed at typical scale.
        color = _CONTEXT_COLOR_MAP.get(det.cls, (180, 180, 180))
        cv2.rectangle(redacted, (det.x1, det.y1), (det.x2, det.y2), color, 2)
    for det, color in [(primary, (0, 0, 255)), (secondary, (0, 200, 255))]:
        # OpenCV uses BGR: (0, 0, 255) = red for primary,
        # (0, 200, 255) = amber for secondary. 3-px thickness keeps the
        # pair visually dominant over the 2-px context boxes above.
        cv2.rectangle(redacted, (det.x1, det.y1), (det.x2, det.y2), color, 3)
    # ``cv2.imwrite`` picks encoding by extension. JPEG quality defaults
    # apply; no per-path override here - the on-disk size is dominated by
    # content, not the quality setting.
    cv2.imwrite(str(public_path), redacted)


def hash_plate(plate_text: str | None) -> str | None:
    """Stable salted hash for plate correlation without plate retention.

    Lets downstream consumers detect repeat offenders ('we've seen this hash
    3 times in 20 min') without storing the actual plate string anywhere.
    Salt is per-deployment so hashes don't correlate across operators.

    Algorithm
    ---------
    1. Normalize: strip spaces, uppercase. "ab 123" == "AB123".
    2. Concat ``{salt}:{normalized}``, UTF-8 encode.
    3. SHA-256, take hex digest.
    4. Prefix with ``plate_`` and truncate to 16 hex chars (64 bits of
       entropy - enough for cross-event correlation within a fleet and
       cheap to store/transmit; we are NOT trying to resist a full
       cryptographic pre-image attack, only to avoid retaining the plate).

    Args
    ----
    plate_text : str | None
        Raw plate string, or None.

    Returns
    -------
    str | None
        ``"plate_<16hex>"`` on success, ``None`` if input was falsy.
    """
    if not plate_text:
        return None
    normalized = plate_text.replace(" ", "").upper()
    # ``hashlib.sha256(bytes).hexdigest()`` -> 64-char hex string. We slice
    # to the first 16 chars for a compact identifier.
    digest = hashlib.sha256(f"{_PLATE_SALT}:{normalized}".encode("utf-8")).hexdigest()
    return f"plate_{digest[:16]}"


def public_thumbnail_name(internal_name: str) -> str:
    """Return the public (redacted) filename given an internal filename.

    Example: ``evt_0001.jpg`` -> ``evt_0001_public.jpg``

    Centralizing the naming convention here prevents call sites from
    inventing their own variant paths and accidentally routing an
    internal thumb through a shared channel. Any code that needs the
    "safe to share" filename must go through here.
    """
    # ``str.rpartition(sep)`` splits from the RIGHT: returns
    # ``(before, sep, after)``. If ``sep`` is absent both ``before`` and
    # ``sep`` are empty - we handle that fallback explicitly.
    stem, _, ext = internal_name.rpartition(".")
    return f"{stem}_public.{ext}" if stem else f"{internal_name}_public"
