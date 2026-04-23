"""Demo GPS track — parses the bundled GPX file into a flat, loopable path
for the frontend map overlay.

Role:
    The demo dashcam MP4 replays forever (see ``StreamReader`` loop mode).
    We want an accompanying map tile that shows the "vehicle" moving along
    a plausible route while the MP4 plays. This module parses a GPX file
    (iPhone "Location Tracker" export, single ``<trk>`` with one or more
    ``<trkseg>`` children, each ``<trkpt>`` carrying ``lat``/``lon`` attrs
    and a ``<time>`` child in ISO-8601 UTC) into a single ordered list of
    ``(lat, lng, t_sec)`` waypoints and caches the result so every request
    reads from memory instead of re-parsing.

Why GPX (and a flat list):
    The iPhone Location Tracker app exports per-point absolute timestamps,
    which is exactly what ``load_track_for_window`` needs to align the map
    marker with a specific MP4's wallclock window. The loader walks every
    ``<trkseg>`` in the file and concatenates its points into one
    continuous loop, offsetting later segments so the total duration grows
    monotonically. That lets the frontend animate a single polyline and
    interpolate position by ``wallclock_sec % total_duration_sec``.

No ``from __future__ import annotations`` — we rely on the 3.10+ union
syntax at runtime (module is imported once at server boot).
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from road_safety.config import PROJECT_ROOT

log = logging.getLogger(__name__)

# Fixed location relative to the project root. If the operator deletes the
# file the endpoint returns ``{ok: false}`` rather than crashing the app.
_TRACK_FILE = PROJECT_ROOT / "resourses" / "log-tracker-merged.gpx"

# GPX 1.1 default namespace. ElementTree needs the Clark-notation prefix
# on every tag lookup (``{http://...}trkpt``) — we build the matchers
# once up front.
_GPX_NS = "http://www.topografix.com/GPX/1/1"
_Q_TRKSEG = f"{{{_GPX_NS}}}trkseg"
_Q_TRKPT = f"{{{_GPX_NS}}}trkpt"
_Q_TIME = f"{{{_GPX_NS}}}time"


@dataclass(frozen=True)
class TrackPoint:
    """One waypoint on the demo loop — immutable for cache safety."""

    lat: float
    lng: float
    # Seconds since the start of the (concatenated) loop. Monotonically
    # non-decreasing within a cached track.
    t_sec: float
    # Trailing-window-smoothed ground speed in m/s, derived from haversine
    # over the *real* GPS timestamps (not the re-based ``t_sec``). 0.0 for
    # the first point of any segment and for sub-noise-floor jitter.
    speed_mps: float = 0.0


# Module-level cache. Populated by ``load_track()`` on first call; later
# calls are O(1). The server only parses once per process — the file
# doesn't change at runtime and a ~200 KB XML re-parse on every
# request is wasteful when the map polls every second or two.
_CACHE: dict[str, Any] | None = None
# Parallel cache of the raw TrackPoint list — kept alongside ``_CACHE`` so
# perception-side consumers (the FPS controller) can look up speed without
# paying for JSON round-trips.
_POINTS_CACHE: list[TrackPoint] | None = None


_EARTH_RADIUS_M = 6_371_000.0
# Trailing-window length for speed smoothing. The GPX is logged at ~1 Hz and
# stationary GPS jitter shows up as 0.1–0.5 m/s of phantom motion; a 5 s
# average + sub-floor clamp removes both without lagging genuine
# accelerations meaningfully.
_SPEED_WINDOW_SEC = 5.0
_SPEED_NOISE_FLOOR_MPS = 0.5


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres between two WGS-84 coordinates."""
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2.0) ** 2
    )
    return 2.0 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _compute_speeds(
    seg: list[tuple[float, float, datetime]],
    window_sec: float = _SPEED_WINDOW_SEC,
    noise_floor_mps: float = _SPEED_NOISE_FLOOR_MPS,
) -> list[float]:
    """Per-point speed in m/s for one segment.

    Uses a *trailing* window: speed at index ``i`` is total distance
    travelled in the last ``window_sec`` divided by elapsed time. Trailing
    (not centred) so the value at point ``i`` only depends on past data —
    matches how a vehicle speedometer behaves and avoids leaking future
    waypoints into the current sample.

    First point of a segment has no prior data and is always 0. When the
    window contains only the current point (sparse data, gaps > window),
    the helper falls back to the immediately previous point so we never
    silently emit 0 m/s on a real motion edge.

    Speeds below ``noise_floor_mps`` are clamped to 0 — stationary GPS
    drift would otherwise produce a perpetual ~0.3 m/s "creep".
    """
    n = len(seg)
    if n <= 1:
        return [0.0] * n

    cum_dist = [0.0] * n
    cum_t = [0.0] * n
    t0 = seg[0][2].timestamp()
    for i in range(1, n):
        cum_dist[i] = cum_dist[i - 1] + _haversine_m(
            seg[i - 1][0], seg[i - 1][1], seg[i][0], seg[i][1],
        )
        cum_t[i] = seg[i][2].timestamp() - t0

    speeds = [0.0] * n
    j = 0  # left edge of the trailing window — slides forward monotonically.
    for i in range(n):
        target_lo = cum_t[i] - window_sec
        while j < i and cum_t[j] < target_lo:
            j += 1
        # If the window collapsed to just point i (gap > window_sec), back
        # up one point so we still get a meaningful speed estimate.
        lo = j if j < i else max(0, i - 1)
        dt = cum_t[i] - cum_t[lo]
        if dt <= 0:
            continue
        s = (cum_dist[i] - cum_dist[lo]) / dt
        if s < noise_floor_mps:
            s = 0.0
        speeds[i] = s
    return speeds


def _parse_iso_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime. ``None`` on failure."""
    if not isinstance(value, str) or not value:
        return None
    # Normalise the trailing Z — fromisoformat only accepts it from 3.11,
    # and we still support 3.10.
    cleaned = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_trkpt(elem: ET.Element) -> tuple[float, float, datetime] | None:
    """Return ``(lat, lng, time_utc)`` for a ``<trkpt>`` element.

    Returns ``None`` if ``lat``/``lon`` are missing/malformed or the
    ``<time>`` child is absent/unparseable — callers drop bad points
    rather than crash.
    """
    lat_s = elem.get("lat")
    lng_s = elem.get("lon")
    if lat_s is None or lng_s is None:
        return None
    try:
        lat = float(lat_s)
        lng = float(lng_s)
    except ValueError:
        return None
    time_el = elem.find(_Q_TIME)
    if time_el is None or not time_el.text:
        return None
    t = _parse_iso_utc(time_el.text)
    if t is None:
        return None
    return lat, lng, t


def _iter_segments(root: ET.Element) -> list[list[tuple[float, float, datetime]]]:
    """Flatten the GPX tree into a list of segments, each a list of
    ``(lat, lng, time_utc)`` tuples sorted by time.

    Empty segments are dropped. A segment preserves its own timestamps so
    callers can still do wallclock windowing; for the loopable view we
    rebase to seconds-from-start.
    """
    segs: list[list[tuple[float, float, datetime]]] = []
    for seg_el in root.iter(_Q_TRKSEG):
        pts: list[tuple[float, float, datetime]] = []
        for trkpt in seg_el.iter(_Q_TRKPT):
            parsed = _parse_trkpt(trkpt)
            if parsed is not None:
                pts.append(parsed)
        if pts:
            pts.sort(key=lambda p: p[2])
            segs.append(pts)
    return segs


def _read_gpx(path: Path) -> list[list[tuple[float, float, datetime]]] | None:
    """Read and segment a GPX file. Returns ``None`` on I/O or parse error."""
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        log.warning("demo track load failed: %s", exc)
        return None
    return _iter_segments(tree.getroot())


def _flatten_loop(segments: list[list[tuple[float, float, datetime]]]) -> list[TrackPoint]:
    """Concatenate every segment into one loop, rebasing times to seconds-from-start.

    Between segments we insert a +1s gap so zero-length "teleport" edges
    don't appear in the frontend interpolator when a recording has a
    dropout.
    """
    pts: list[TrackPoint] = []
    segment_base_sec = 0.0
    for seg in segments:
        if not seg:
            continue
        seg_start_ts = seg[0][2].timestamp()
        speeds = _compute_speeds(seg)
        seg_max = 0.0
        for (lat, lng, t), spd in zip(seg, speeds):
            t_sec = segment_base_sec + (t.timestamp() - seg_start_ts)
            pts.append(TrackPoint(lat=lat, lng=lng, t_sec=t_sec, speed_mps=spd))
            if t_sec > seg_max:
                seg_max = t_sec
        segment_base_sec = seg_max + 1.0
    return pts


def _build_cache() -> dict[str, Any]:
    """Read, parse, and freeze the track + basic aggregates.

    The returned dict is the exact shape the ``/api/demo/track`` endpoint
    returns (plus a Python ``points`` list of ``TrackPoint`` — which FastAPI
    serialises as dicts because of the ``dataclass`` annotation).
    """
    if not _TRACK_FILE.exists():
        log.warning("demo track file missing: %s", _TRACK_FILE)
        return {
            "ok": False,
            "error": f"track file not found: {_TRACK_FILE.name}",
            "points": [],
            "total_duration_sec": 0.0,
            "bounds": None,
        }

    segments = _read_gpx(_TRACK_FILE)
    if segments is None:
        return {
            "ok": False,
            "error": "failed to parse track",
            "points": [],
            "total_duration_sec": 0.0,
            "bounds": None,
        }

    points = _flatten_loop(segments)
    if not points:
        return {
            "ok": False,
            "error": "no usable trkpt waypoints in track file",
            "points": [],
            "total_duration_sec": 0.0,
            "bounds": None,
        }

    total = points[-1].t_sec if points else 0.0
    lats = [p.lat for p in points]
    lngs = [p.lng for p in points]
    bounds = {
        "south": min(lats),
        "west": min(lngs),
        "north": max(lats),
        "east": max(lngs),
    }

    log.info(
        "demo track loaded: %d points spanning %.1fs (bounds=%s)",
        len(points),
        total,
        bounds,
    )
    global _POINTS_CACHE
    _POINTS_CACHE = points
    return {
        "ok": True,
        "points": [
            {
                "lat": p.lat,
                "lng": p.lng,
                "t_sec": p.t_sec,
                "speed_mps": p.speed_mps,
            }
            for p in points
        ],
        "total_duration_sec": total,
        "bounds": bounds,
    }


def load_track() -> dict[str, Any]:
    """Return the cached, JSON-serialisable track payload.

    Callers should treat the returned dict as read-only — it's shared by
    every request. Mutating it would corrupt future responses.
    """
    global _CACHE
    if _CACHE is None:
        _CACHE = _build_cache()
    return _CACHE


def speed_mps_at(t_sec: float) -> float | None:
    """Return the GPS speed at ``t_sec`` into the loop, or None if unavailable.

    ``t_sec`` is interpreted against the *looped* demo track — the same
    clock the frontend map uses for its marker. Out-of-range values are
    wrapped by ``% total_duration_sec`` so a caller can feed either a
    playback head that already loops (StreamReader) or a wallclock-mod
    value without branching.

    Linear interpolation between adjacent ``TrackPoint``\\s. ``speed_mps``
    is already trailing-window-smoothed inside ``_compute_speeds`` so
    interpolation here cannot accidentally un-smooth a real signal.
    """
    if _CACHE is None:
        # Trigger cache population on first call.
        load_track()
    if not _POINTS_CACHE:
        return None
    total = _POINTS_CACHE[-1].t_sec if _POINTS_CACHE else 0.0
    if total <= 0.0:
        return None
    # Wrap into the loop domain. A caller feeding a playback head that
    # already loops will still land in range here (modulo is a no-op).
    t = t_sec % total
    pts = _POINTS_CACHE
    # Binary search for the segment containing ``t``. Linear scan would
    # also work (tracks are ~hundreds of points) but bisect keeps this
    # call cheap enough to use on the hot path.
    import bisect
    idx = bisect.bisect_left([p.t_sec for p in pts], t)
    if idx <= 0:
        return pts[0].speed_mps
    if idx >= len(pts):
        return pts[-1].speed_mps
    prev, nxt = pts[idx - 1], pts[idx]
    span = max(1e-6, nxt.t_sec - prev.t_sec)
    alpha = (t - prev.t_sec) / span
    return prev.speed_mps + alpha * (nxt.speed_mps - prev.speed_mps)


def reset_cache_for_tests() -> None:
    """Drop the in-memory cache so a test can re-parse after monkeypatching.

    Also clears the parallel raw-points cache used by ``speed_mps_at``.
    Exposed solely for tests; production code should never need to call
    this (the track file doesn't change at runtime).
    """
    global _CACHE, _POINTS_CACHE
    _CACHE = None
    _POINTS_CACHE = None


# ---------------------------------------------------------------------------
# Video-synced variant — slice the track to the window of a real recording
# ---------------------------------------------------------------------------


def _points_in_window(
    segments: list[list[tuple[float, float, datetime]]],
    window_start: datetime,
    window_end: datetime,
) -> list[TrackPoint]:
    """Return waypoints that fall inside ``[window_start, window_end]``.

    Each waypoint's ``t_sec`` is re-based so ``t_sec == 0`` means
    ``window_start`` (i.e. the beginning of the video). Waypoints outside
    the window are dropped. Segments whose entire wallclock span sits
    outside the window are skipped cheaply.
    """
    pts: list[TrackPoint] = []
    ws = window_start.timestamp()
    we = window_end.timestamp()
    for seg in segments:
        if not seg:
            continue
        seg_start = seg[0][2]
        seg_end = seg[-1][2]
        # Fast reject: segment entirely outside the window.
        if seg_end < window_start or seg_start > window_end:
            continue
        # Speeds are computed across the *entire* segment so the trailing
        # window can see prior context even when the early points fall
        # outside the requested window.
        speeds = _compute_speeds(seg)
        for (lat, lng, t), spd in zip(seg, speeds):
            ts = t.timestamp()
            if ts < ws or ts > we:
                continue
            pts.append(TrackPoint(lat=lat, lng=lng, t_sec=ts - ws, speed_mps=spd))
    pts.sort(key=lambda p: p.t_sec)
    return pts


def _nearest_segment_points(
    segments: list[list[tuple[float, float, datetime]]],
    window_start: datetime,
    duration_sec: float,
) -> tuple[list[TrackPoint], dict[str, Any] | None]:
    """Fallback: pick the segment closest to ``window_start`` and linearly
    re-time its waypoints across ``[0, duration_sec]``.

    Why: The GPS export isn't guaranteed to cover the exact wallclock
    window of a video (e.g. GPS dropout, or the export was trimmed).
    Rather than showing an empty map, we pick the nearest segment —
    preferring ones that *end* before the video starts (most recent
    recorded history) — and stretch its points across the video's
    playback duration. The animation preserves relative waypoint pacing
    (a 30 s gap in the source is still a gap in the output, just scaled).

    Returns ``(points, meta)`` where ``meta`` is a small dict describing
    the chosen segment, suitable for embedding in the response so the
    frontend can show "approximate" UI affordances.
    """
    if duration_sec <= 0:
        return [], None

    window_ts = window_start.timestamp()
    best: tuple[float, int, list[tuple[float, float, datetime]]] | None = None

    for seg in segments:
        if not seg:
            continue
        seg_start = seg[0][2]
        seg_end = seg[-1][2]
        # Distance = seconds between the segment's end and the window start.
        # Past segments are preferred; future segments get a small penalty.
        if seg_end <= window_start:
            distance_sec = window_ts - seg_end.timestamp()
        else:
            distance_sec = max(0.0, seg_start.timestamp() - window_ts) + 1.0
        # Tie-break: prefer segments with more waypoints (richer signal)
        # by using -count as the secondary sort key.
        candidate = (distance_sec, -len(seg), seg)
        if best is None or candidate < best:
            best = candidate

    if best is None:
        return [], None

    _, neg_count, seg = best
    seg_start = seg[0][2]
    seg_end = seg[-1][2]
    # Re-time: map each point's offset-within-segment onto [0, duration_sec].
    offsets = [p[2].timestamp() - seg_start.timestamp() for p in seg]
    lo = min(offsets)
    hi = max(offsets)
    span = hi - lo
    # Speeds are derived from the *real* GPS timestamps. We deliberately do
    # not recompute them off the stretched ``t_sec`` values — a 30 s slice
    # stretched to 600 s of video would otherwise underreport speed 20×.
    speeds = _compute_speeds(seg)
    out: list[TrackPoint] = []
    if span <= 0:
        # Single-waypoint segment: drop a stationary point at t=0 and t=duration
        # so the interpolator still has two points to work with.
        lat, lng, _ = seg[0]
        out.append(TrackPoint(lat=lat, lng=lng, t_sec=0.0, speed_mps=0.0))
        out.append(TrackPoint(lat=lat, lng=lng, t_sec=duration_sec, speed_mps=0.0))
    else:
        for off, (lat, lng, _), spd in zip(offsets, seg, speeds):
            frac = (off - lo) / span
            out.append(
                TrackPoint(lat=lat, lng=lng, t_sec=frac * duration_sec, speed_mps=spd),
            )
        out.sort(key=lambda p: p.t_sec)

    meta = {
        "segment_start": seg_start.isoformat(),
        "segment_end": seg_end.isoformat(),
        "point_count": -neg_count,
    }
    return out, meta


def load_track_for_window(
    start_iso_utc: str,
    duration_sec: float,
) -> dict[str, Any]:
    """Return a track payload scoped to ``[start_iso_utc, +duration_sec]``.

    Unlike :func:`load_track` (which flattens the whole track into a
    loopable path), this variant re-bases ``t_sec`` so ``0`` is the
    video's first frame. The frontend can then drive the map marker
    directly from video playback time — no wallclock compression needed.

    Returned shape matches :func:`load_track` so the same frontend hook
    can consume it.

    No caching: the inputs (start/duration) are per-video, and parsing
    the GPX a handful of times at server boot is negligible.
    """
    window_start = _parse_iso_utc(start_iso_utc)
    if window_start is None or duration_sec <= 0:
        return {
            "ok": False,
            "error": f"invalid window: start={start_iso_utc!r} duration={duration_sec}",
            "points": [],
            "total_duration_sec": 0.0,
            "bounds": None,
        }
    window_end = datetime.fromtimestamp(
        window_start.timestamp() + duration_sec, tz=timezone.utc,
    )

    if not _TRACK_FILE.exists():
        log.warning("demo track file missing: %s", _TRACK_FILE)
        return {
            "ok": False,
            "error": f"track file not found: {_TRACK_FILE.name}",
            "points": [],
            "total_duration_sec": 0.0,
            "bounds": None,
        }
    segments = _read_gpx(_TRACK_FILE)
    if segments is None:
        return {
            "ok": False,
            "error": "failed to parse track",
            "points": [],
            "total_duration_sec": 0.0,
            "bounds": None,
        }

    points = _points_in_window(segments, window_start, window_end)
    sync_mode = "exact"
    fallback_meta: dict[str, Any] | None = None
    if not points:
        # No overlap between the video's wallclock window and any
        # recorded segment. Fall back to the nearest segment, re-timed
        # to fit the video duration. Response still renders a map.
        points, fallback_meta = _nearest_segment_points(
            segments, window_start, duration_sec,
        )
        sync_mode = "nearest"
        if not points:
            return {
                "ok": False,
                "error": (
                    "no trkpt waypoints in window "
                    f"[{window_start.isoformat()}, {window_end.isoformat()}] "
                    "and no nearest-segment fallback available"
                ),
                "points": [],
                "total_duration_sec": duration_sec,
                "bounds": None,
                "sync_mode": "none",
            }
        log.info(
            "demo track window empty; using nearest segment %s (%d points)",
            fallback_meta.get("segment_start") if fallback_meta else "?",
            len(points),
        )
    else:
        log.info(
            "demo track windowed: %d points in [%s, %s]",
            len(points),
            window_start.isoformat(),
            window_end.isoformat(),
        )

    lats = [p.lat for p in points]
    lngs = [p.lng for p in points]
    response: dict[str, Any] = {
        "ok": True,
        "points": [
            {
                "lat": p.lat,
                "lng": p.lng,
                "t_sec": p.t_sec,
                "speed_mps": p.speed_mps,
            }
            for p in points
        ],
        "total_duration_sec": duration_sec,
        "bounds": {
            "south": min(lats),
            "west": min(lngs),
            "north": max(lats),
            "east": max(lngs),
        },
        "sync_mode": sync_mode,
    }
    if fallback_meta is not None:
        response["fallback_segment"] = fallback_meta
    return response
