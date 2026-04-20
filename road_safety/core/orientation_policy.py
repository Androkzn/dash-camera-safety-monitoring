"""Per-camera-orientation event gates — the dispatcher that turns raw
`find_interactions` candidates into SAE J3063 event families.

Role in the pipeline
--------------------
`road_safety/server.py::_run_loop` runs a single detection + TTC stack for
every stream slot, regardless of where that camera points. That stack was
originally tuned for a forward dashcam, so wiring it verbatim into a
rear-cam or side-cam slot fires "pedestrian in path" on a person walking
the sidewalk next to a moving vehicle, or "vehicle close interaction" on
parked cars the ego car drives past. Those are not incidents; those are
the normal geometry of the other camera angles.

This module is the *policy brain*: it takes the raw candidate event
together with the camera's `CameraCalibration.orientation` and decides
whether the event should be emitted at all, and if so which SAE J3063
family it belongs to. Industry references:

    - SAE J3063 — taxonomy of crash-avoidance features; gives us the
      names (FCW, BSW, RCW, RCTA) the dashboards and audit log use.
    - ISO 17387 — lane-change decision aid systems; specifies the
      dwell-time requirement that keeps BSW from firing on pedestrians
      who walk past a side window in one or two frames.
    - ISO 22840 — rear-view low-speed manoeuvre; the gear-state
      precondition for RCW / RCTA. We don't have a real gear signal on
      the demo rig, so we approximate "ego is reversing" from the signed
      ego-flow direction computed in `egomotion.py`.

Gate layout (match the existing detection.py style: one gate per concern):

    * `is_reversing(ego)` — ego direction gate for rear-cam events.
    * `in_blind_zone(det, w, h, orientation)` — spatial ROI gate for BSW.
    * `blind_zone_dwell_sec(tid, history, w, h, orientation)` — ISO 17387
      timing gate; guards against transients.
    * `classify_event(...)` — dispatches on orientation and returns a
      `PolicyDecision`. The caller (server.py) consults `decision.emit`
      and, when true, uses `decision.taxonomy` + `decision.display_event_type`
      in the emitted event payload.

Testability: every helper takes plain dataclasses and primitives so the
unit tests in `tests/` can exercise it without booting the FastAPI app
or loading YOLO weights.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

# TYPE_CHECKING keeps these imports out of the runtime graph. The file is
# imported early by server.py and we don't want a circular import via
# egomotion.py (which pulls TrackHistory from detection.py, which pulls
# config.py, which is the same bottom-of-graph module we also live on).
if TYPE_CHECKING:  # pragma: no cover - hints only
    from road_safety.config import CameraCalibration
    from road_safety.core.detection import Detection, TrackHistory
    from road_safety.core.egomotion import EgoFlow


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SAE J3063 event family taxonomy
# ─────────────────────────────────────────────────────────────────────────────
# Short literal union so linters + IDEs surface typos (e.g. "BSA" vs "BSW").
# Extend only when adding a new *front-end-visible* family; internal variants
# go on `display_event_type` instead.
EventTaxonomy = Literal["FCW", "BSW", "RCW", "RCTA", "NONE"]
# FCW  — Forward Collision Warning (and pedestrian-in-path). Forward cams.
# BSW  — Blind Spot Warning. Side cams, object persistent in blind zone.
# RCW  — Reverse Collision Warning. Rear cams, ego reversing.
# RCTA — Rear Cross-Traffic Alert. Rear cams, ego reversing, lateral approach.
# NONE — Explicitly suppressed (e.g. forward-TTC fired on a side cam).


@dataclass(frozen=True)
class PolicyDecision:
    """Per-candidate-event decision returned by `classify_event`.

    A frozen dataclass because this value crosses between the perception
    loop and the event-emit path — freezing it makes accidental mutation
    impossible and lets the decision be logged / audited as-is.

    Attributes:
        emit: Should the caller actually emit an event for this candidate?
            `False` is a deliberate suppression, not an error.
        taxonomy: The SAE J3063 family to tag the event with.
        reason: Short human-readable string — shown in audit log, tooltip,
            and debug dumps. Kept terse so it fits in a Slack embed.
        display_event_type: Optional override for the emitted event's
            `event_type` field. `None` means "keep the raw internal type".
            Set to an orientation-aware string (e.g. "blind_spot_pedestrian")
            when the raw type ("pedestrian_proximity") would mis-describe
            the incident on this camera.
    """

    emit: bool
    taxonomy: EventTaxonomy
    reason: str
    display_event_type: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Configuration knobs (module-level, tunable; no env plumbing yet)
# ─────────────────────────────────────────────────────────────────────────────

# ISO 17387: an object must persist in the blind zone for at least this long
# before BSW fires. Guards against transients — a pedestrian who appears in a
# single frame of a side cam at 2 fps is not "in the blind spot", they're
# passing through. 0.4 s sits inside the 300–500 ms band the standard
# recommends and matches what Mobileye / Samsara ship in fleet product.
BSW_DWELL_SEC: float = 0.4

# Blind-zone ROI, expressed as fractions of frame width / height. The box
# defines the rectangle inside which a detection center must land to count
# as "in the adjacent lane" for a side-mounted camera.
#
# NOTE on left vs right: `CameraCalibration.orientation == "side"` does not
# distinguish left-mount from right-mount. Without a slot_id hint threaded
# through `classify_event`, we keep the ROI symmetric around the horizontal
# image center — a conservative approximation that works for both installs
# at the cost of a slightly smaller blind zone than a side-specific ROI
# would give. Upgrade to a per-side ROI when the signature grows a hint.
BSW_ZONE_FRAC_X: float = 0.55          # width of the central band
BSW_ZONE_FRAC_Y_TOP: float = 0.25      # top edge (skip the sky)
BSW_ZONE_FRAC_Y_BOTTOM: float = 0.95   # bottom edge (skip the car body strip)

# Rear-cam reverse gate: RCW / RCTA only fire when ego appears to be moving
# backward. Without a real gear signal, we infer from `EgoFlow.direction`
# (produced by `egomotion.py`). When `False`, the rear cam behaves like any
# other cam — useful for tuning / debugging, never in production.
RCW_REQUIRE_REVERSING: bool = True

# Minimum `EgoFlow.confidence` before we trust the direction label. Below
# this we play safe *per orientation* — see `classify_event`. Tuned to the
# same floor `egomotion.py` uses internally (`_MIN_CONFIDENCE = 0.2`) plus
# a small headroom so we don't act on borderline estimates.
EGO_DIRECTION_MIN_CONFIDENCE: float = 0.25

# RCTA lateral-dominance heuristic. If the secondary track's |dx/dt| exceeds
# |dy/dt| by this factor, we call it cross-traffic. 1.2 is a small margin
# so near-diagonal approaches still register as longitudinal (RCW) by
# default — RCTA is the more specific label, RCW is the fallback.
RCTA_LATERAL_DOMINANCE: float = 1.2


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers — each gate is importable + unit-testable in isolation.
# ─────────────────────────────────────────────────────────────────────────────


def is_reversing(ego: Optional["EgoFlow"]) -> bool:
    """True iff ego-motion direction estimate is `reverse` with enough confidence.

    The rear-cam reverse gate. We intentionally fail-closed when `ego` is
    `None` (too little texture this frame) or when the confidence is below
    `EGO_DIRECTION_MIN_CONFIDENCE` — emitting a rear-cam event while we
    can't even tell which way the car is going is a recipe for alert
    fatigue, and missing an event is the cheaper failure mode on the rear
    axis (collisions while reversing are rare vs. forward).

    Args:
        ego: Latest `EgoFlow` snapshot from `EgoMotionEstimator.update`, or
            `None` when the estimator declined to publish this frame.

    Returns:
        `True` only when `ego is not None` AND `ego.confidence` clears the
        floor AND `ego.direction == "reverse"`. Anything else — including
        the (expected) case where `EgoFlow` hasn't grown a `direction`
        field yet in an older build — returns `False`.
    """
    if ego is None:
        return False
    # `EgoFlow.direction` is being added in a parallel refactor (agent B).
    # Until that lands we safely degrade to "not reversing" via `getattr`
    # so this module imports cleanly against the current dataclass.
    direction = getattr(ego, "direction", None)
    confidence = getattr(ego, "confidence", 0.0)
    if direction != "reverse":
        return False
    return confidence >= EGO_DIRECTION_MIN_CONFIDENCE


def in_blind_zone(det, frame_w: int, frame_h: int, orientation: str) -> bool:
    """True iff the detection's bbox center lies inside the side-cam blind zone.

    The spatial half of the BSW gate. Only side cameras have a blind zone
    in this model — forward / rear orientations get a flat `False` so any
    forward-TTC event that leaks into this helper through a bad call site
    doesn't accidentally become a BSW.

    The ROI is a central horizontal band of the frame: vertically bounded
    by `BSW_ZONE_FRAC_Y_TOP` / `BSW_ZONE_FRAC_Y_BOTTOM` (skip the sky and
    the ego car body), horizontally symmetric around image-center with
    total width `BSW_ZONE_FRAC_X`. The symmetric choice is deliberate:
    `CameraCalibration.orientation == "side"` doesn't tell us left-mount
    from right-mount, so we use a zone that works for both — at the cost
    of a slightly narrower blind zone than a side-specific ROI. Upgrade
    the call when a side hint is plumbed through.

    Args:
        det: A detection-like object exposing a `.center` → `(cx, cy)`
            tuple in pixels (the same contract the rest of this module
            uses — both `Detection` and test stubs satisfy it).
        frame_w: Frame width in pixels.
        frame_h: Frame height in pixels.
        orientation: The camera's `CameraCalibration.orientation`.

    Returns:
        `True` when the bbox center is inside the zone AND `orientation`
        is `"side"`. `False` for `"forward"` / `"rear"` / unknown values.
    """
    if orientation != "side":
        return False
    if frame_w <= 0 or frame_h <= 0:
        # Defensive: a zero-size frame would make the zone undefined.
        return False
    try:
        cx, cy = det.center
    except (AttributeError, TypeError, ValueError):
        return False

    # Symmetric band around image-center.
    half = BSW_ZONE_FRAC_X / 2.0
    x_lo = frame_w * (0.5 - half)
    x_hi = frame_w * (0.5 + half)
    y_lo = frame_h * BSW_ZONE_FRAC_Y_TOP
    y_hi = frame_h * BSW_ZONE_FRAC_Y_BOTTOM
    return (x_lo <= cx <= x_hi) and (y_lo <= cy <= y_hi)


def blind_zone_dwell_sec(
    track_id: int,
    history,
    frame_w: int,
    frame_h: int,
    orientation: str,
) -> float:
    """Return seconds the track has continuously been in-zone.

    The timing half of the BSW gate (ISO 17387). We walk
    `history.samples(track_id)` backwards from newest to oldest; the dwell
    is the span from the latest sample back to the first sample that was
    still in-zone. The moment we hit a sample that sits outside the zone
    we stop — so a track that left and re-entered only reports the most
    recent in-zone streak, which is the right semantic for "continuously".

    Limitation — x-coordinate approximation:
        `TrackSample` stores `bottom` and `height` but not `cx`. We
        therefore approximate each historical sample's x-center with the
        *current* detection's x-center (via the latest sample's absence of
        x). This means the dwell currently measures vertical persistence
        only — a track that slid laterally out of the horizontal band
        between two frames will still be counted as in-zone. When
        `TrackSample` is extended with `cx`, upgrade this helper to read
        the stored value instead.

    Args:
        track_id: The track to measure.
        history: A `TrackHistory`-like object exposing `.samples(track_id)`
            → list of items with `.t`, `.bottom`, `.height`.
        frame_w: Frame width in pixels (for zone geometry).
        frame_h: Frame height in pixels (for zone geometry).
        orientation: Camera orientation. Returns `0.0` for anything other
            than `"side"`.

    Returns:
        Seconds of continuous in-zone presence, `0.0` when no history, no
        in-zone latest sample, or a non-side orientation.
    """
    if orientation != "side":
        return 0.0
    try:
        samples = history.samples(track_id)
    except AttributeError:
        return 0.0
    if not samples:
        return 0.0

    # Build a stand-in detection for the x-test. Since TrackSample has no
    # cx, we anchor x at image-center — the conservative choice given the
    # zone is a horizontally symmetric band. A track whose bbox bottom
    # falls in the vertical band will then be considered in-zone on the
    # x-axis too. Limitation documented in the docstring.
    y_lo = frame_h * BSW_ZONE_FRAC_Y_TOP
    y_hi = frame_h * BSW_ZONE_FRAC_Y_BOTTOM

    latest = samples[-1]
    # If the latest sample is out of the vertical band, there's no active
    # dwell streak — return 0.0 so BSW is suppressed until the track
    # re-enters and stays for another BSW_DWELL_SEC.
    if not (y_lo <= latest.bottom <= y_hi):
        return 0.0

    # Walk backwards, find the oldest contiguous in-zone sample.
    start_t = latest.t
    for sample in reversed(samples):
        if y_lo <= sample.bottom <= y_hi:
            start_t = sample.t
            continue
        break

    dwell = latest.t - start_t
    return max(0.0, dwell)


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers — RCTA lateral/longitudinal heuristic.
# ─────────────────────────────────────────────────────────────────────────────


def _lateral_dominant(history_samples) -> bool:
    """True when the trailing window's lateral motion dominates longitudinal.

    Tiny internal helper so `classify_event` stays readable. We use the
    full trailing window endpoints (not a per-step derivative) because at
    our 2 fps cadence per-step derivatives are dominated by bbox jitter —
    same reasoning as `estimate_pair_ttc` uses for its endpoint-to-endpoint
    closing-rate calculation.

    Args:
        history_samples: A list of `TrackSample`-like objects with `.t`,
            `.cx`, `.cy`. Fewer than 2 samples → `False` (nothing to
            differentiate across).

    Returns:
        `True` iff `|dx| > |dy| * RCTA_LATERAL_DOMINANCE` over the window.
    """
    if len(history_samples) < 2:
        return False
    first = history_samples[0]
    last = history_samples[-1]
    dt = last.t - first.t
    if dt <= 0:
        return False
    dx = abs(last.cx - first.cx) / dt
    dy = abs(last.cy - first.cy) / dt
    # Strict > so a perfectly diagonal 45° approach falls to RCW, the
    # more conservative (longitudinal) label.
    return dx > dy * RCTA_LATERAL_DOMINANCE


# ─────────────────────────────────────────────────────────────────────────────
# Main dispatch
# ─────────────────────────────────────────────────────────────────────────────


def classify_event(
    *,
    calibration: "CameraCalibration",
    event_type: str,
    primary,
    secondary,
    frame_w: int,
    frame_h: int,
    ego: Optional["EgoFlow"],
    track_history,
) -> PolicyDecision:
    """Decide whether to emit, and under which SAE taxonomy.

    The dispatcher is a simple branch on `calibration.orientation`; each
    branch composes the gates defined above. We deliberately avoid any
    multi-orientation "smart" logic — a wrong guess in this dispatch
    layer shows up in the incident queue as a mis-labelled alert, which
    is harder to debug than the obvious per-orientation mapping below.

    Forward cam:
        Always emit. `taxonomy="FCW"`, `display_event_type=None` (keeps
        the raw internal type from `find_interactions`). This preserves
        the pre-existing behaviour byte-for-byte for forward-facing
        installs — no regression risk for the demo vehicle's front cam.

    Rear cam:
        Emit only when `is_reversing(ego)` reports True. When emitting,
        pick `"RCTA"` if the secondary track's motion over the trailing
        window is lateral-dominant (cross-traffic), else `"RCW"`.
        `display_event_type` is mapped to a front-end-friendly string so
        the admin UI can render a different icon for rear events without
        re-deriving the taxonomy client-side.

    Side cam:
        Emit only when the detection's bbox center is inside the blind
        zone AND the track has been continuously in-zone for at least
        `BSW_DWELL_SEC`. Ego direction is treated as a soft precondition:
        we suppress BSW when ego is *confidently* reversing (a reversing
        car cares about what's behind, not beside) but *allow* BSW when
        ego is stationary / forward / low-confidence — the parked-with-
        engine-running scenario still needs adjacent-lane awareness.

    Args:
        calibration: Resolved per-slot camera calibration.
        event_type: Raw internal event type from `find_interactions`
            (e.g. `"pedestrian_proximity"`, `"vehicle_close_interaction"`).
        primary, secondary: Detections involved. For single-object events
            the caller may pass the same detection for both.
        frame_w, frame_h: Image dimensions in pixels.
        ego: Latest `EgoFlow` (may be `None`).
        track_history: Shared `TrackHistory` feeding the dwell gate.

    Returns:
        A `PolicyDecision`. `emit=False` means "suppress silently" and
        carries a `reason` string for the audit log / debug UI. `emit=True`
        carries the final taxonomy and, when the orientation is not
        forward, a `display_event_type` override.
    """
    orientation = getattr(calibration, "orientation", "forward")

    # Forward cam: preserve existing behaviour. The full detection.py
    # gate stack has already vetted this candidate; the policy layer
    # adds nothing except the SAE label.
    if orientation == "forward":
        decision = PolicyDecision(
            emit=True,
            taxonomy="FCW",
            reason="forward orientation: standard FCW pipeline",
            display_event_type=None,
        )
        log.debug(
            "orientation_policy: forward event emit (type=%s, primary_track=%s)",
            event_type,
            getattr(primary, "track_id", None),
        )
        return decision

    # Rear cam: ISO 22840 reverse precondition.
    if orientation == "rear":
        if RCW_REQUIRE_REVERSING and not is_reversing(ego):
            decision = PolicyDecision(
                emit=False,
                taxonomy="NONE",
                reason="rear-cam suppressed: ego not reversing",
            )
            log.debug(
                "orientation_policy: rear suppress (type=%s, ego=%s)",
                event_type,
                "None" if ego is None else f"dir={getattr(ego, 'direction', '?')}",
            )
            return decision

        # Reversing — pick RCTA if the secondary's trailing motion is
        # lateral-dominant, else RCW. `find_interactions` may pass the
        # same detection for primary+secondary on single-object events;
        # in that case we still compute the heuristic on whatever track
        # we have — a `secondary is primary` call just reads the same
        # history twice.
        secondary_samples = []
        if getattr(secondary, "track_id", None) is not None:
            try:
                secondary_samples = track_history.samples(secondary.track_id)
            except AttributeError:
                secondary_samples = []

        if _lateral_dominant(secondary_samples):
            taxonomy: EventTaxonomy = "RCTA"
            display = "rear_cross_traffic"
            reason = "rear-cam reversing: lateral-dominant motion → RCTA"
        else:
            taxonomy = "RCW"
            display = "reverse_collision_risk"
            reason = "rear-cam reversing: longitudinal motion → RCW"

        decision = PolicyDecision(
            emit=True,
            taxonomy=taxonomy,
            reason=reason,
            display_event_type=display,
        )
        log.debug(
            "orientation_policy: rear emit taxonomy=%s display=%s",
            taxonomy, display,
        )
        return decision

    # Side cam: ISO 17387 spatial + dwell. FCW-style TTC alerts on a side
    # cam are almost always a pedestrian walking past the window — we
    # route everything through the blind-spot model instead.
    if orientation == "side":
        # Ego precondition: only actively suppress when we're confident ego
        # is reversing. Stationary / forward / low-confidence all pass
        # through to the spatial + dwell gates below.
        if is_reversing(ego):
            decision = PolicyDecision(
                emit=False,
                taxonomy="NONE",
                reason="side-cam suppressed: ego reversing (rear events own this)",
            )
            log.debug("orientation_policy: side suppress (reversing)")
            return decision

        if not in_blind_zone(secondary, frame_w, frame_h, orientation):
            decision = PolicyDecision(
                emit=False,
                taxonomy="NONE",
                reason="side-cam suppressed: not in blind zone",
            )
            log.debug(
                "orientation_policy: side suppress (out-of-zone, type=%s)",
                event_type,
            )
            return decision

        # Dwell measured against the secondary track — that's the object
        # being assessed for blind-spot presence. Primary may well be the
        # ego vehicle's own body edge (pedestrian_proximity pairs a
        # pedestrian against a vehicle; on a side cam the "vehicle" can
        # be the ego body on the near edge).
        sec_track_id = getattr(secondary, "track_id", None)
        dwell = 0.0
        if sec_track_id is not None:
            dwell = blind_zone_dwell_sec(
                sec_track_id, track_history, frame_w, frame_h, orientation,
            )
        if not math.isfinite(dwell) or dwell < BSW_DWELL_SEC:
            decision = PolicyDecision(
                emit=False,
                taxonomy="NONE",
                reason=f"side-cam suppressed: dwell too short ({dwell:.2f}s < {BSW_DWELL_SEC:.2f}s)",
            )
            log.debug(
                "orientation_policy: side suppress (dwell=%.2fs < %.2fs)",
                dwell, BSW_DWELL_SEC,
            )
            return decision

        # BSW variant depends on what's in the blind spot.
        sec_cls = getattr(secondary, "cls", "")
        if sec_cls == "person":
            display = "blind_spot_pedestrian"
        else:
            display = "blind_spot_vehicle"

        decision = PolicyDecision(
            emit=True,
            taxonomy="BSW",
            reason=f"side-cam BSW: {sec_cls or 'object'} in blind zone {dwell:.2f}s",
            display_event_type=display,
        )
        log.debug(
            "orientation_policy: side emit BSW (cls=%s, dwell=%.2fs)",
            sec_cls, dwell,
        )
        return decision

    # Unknown orientation string — fail-closed with a debug log so an
    # operator typo in `ROAD_CAMERA_ORIENTATION__<SLOT>` is visible.
    log.debug(
        "orientation_policy: unknown orientation=%r, suppressing event type=%s",
        orientation, event_type,
    )
    return PolicyDecision(
        emit=False,
        taxonomy="NONE",
        reason=f"unknown orientation: {orientation!r}",
    )
