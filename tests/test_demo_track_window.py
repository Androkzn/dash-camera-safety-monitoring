"""Tests for road_safety.services.demo_track.load_track_for_window.

Synthesises GPX-shaped input rather than reading the bundled file so the
behaviour is exercised deterministically regardless of what ships in
``resourses/``.
"""

from pathlib import Path

from road_safety.services import demo_track

_GPX_NS = "http://www.topografix.com/GPX/1/1"


def _write_gpx(path: Path, segments: list[list[tuple[float, float, str]]]) -> None:
    """Write a GPX 1.1 file containing ``segments`` of ``(lat, lng, iso_utc)`` tuples.

    Each inner list becomes one ``<trkseg>``; empty segments are still
    emitted (mirroring real-world GPS dropouts) but contain no ``<trkpt>``.
    """
    parts = [
        "<?xml version='1.0' encoding='utf-8'?>",
        f'<gpx xmlns="{_GPX_NS}" version="1.1" creator="test">',
        "<trk>",
    ]
    for seg in segments:
        parts.append("<trkseg>")
        for lat, lng, t in seg:
            parts.append(
                f'<trkpt lat="{lat}" lon="{lng}"><time>{t}</time></trkpt>'
            )
        parts.append("</trkseg>")
    parts.append("</trk></gpx>")
    path.write_text("\n".join(parts))


def test_window_slices_and_rebases_t_sec(tmp_path: Path, monkeypatch):
    track_file = tmp_path / "track.gpx"
    _write_gpx(
        track_file,
        [
            [
                # wallclock 22:00 → inside window
                (49.10, -122.80, "2026-04-19T22:00:00.000Z"),
                # wallclock 22:05 → inside window
                (49.11, -122.81, "2026-04-19T22:05:00.000Z"),
                # wallclock 22:30 → inside window (at the boundary)
                (49.12, -122.82, "2026-04-19T22:30:00.000Z"),
                # wallclock 22:59 → outside window (video ends at 22:30)
                (49.13, -122.83, "2026-04-19T22:59:00.000Z"),
            ],
        ],
    )
    monkeypatch.setattr(demo_track, "_TRACK_FILE", track_file)

    # Video: starts 22:00 UTC, duration 30 min → window [22:00, 22:30].
    payload = demo_track.load_track_for_window(
        start_iso_utc="2026-04-19T22:00:00Z",
        duration_sec=30 * 60,
    )

    assert payload["ok"] is True
    # 3 waypoints inside the window; the 22:59 one is dropped.
    assert len(payload["points"]) == 3
    # t_sec is re-based to the video: first waypoint at 22:00 → t_sec = 0.
    assert payload["points"][0]["t_sec"] == 0
    # Second at 22:05 → 300s after video start.
    assert payload["points"][1]["t_sec"] == 300
    # Third at 22:30 → 1800s after video start, exactly at the boundary.
    assert payload["points"][2]["t_sec"] == 1800
    assert payload["total_duration_sec"] == 1800


def test_window_falls_back_to_nearest_segment(tmp_path: Path, monkeypatch):
    """When the exact window has no waypoints, use the nearest segment
    and re-time its points across the video duration."""
    track_file = tmp_path / "track.gpx"
    _write_gpx(
        track_file,
        [
            # Segment ends 50 min before the video — nearest preceding.
            [
                (49.10, -122.80, "2026-04-19T21:00:00.000Z"),
                (49.11, -122.81, "2026-04-19T21:05:00.000Z"),
                (49.12, -122.82, "2026-04-19T21:10:00.000Z"),
            ],
        ],
    )
    monkeypatch.setattr(demo_track, "_TRACK_FILE", track_file)

    payload = demo_track.load_track_for_window(
        start_iso_utc="2026-04-19T22:00:00Z",
        duration_sec=600,
    )

    assert payload["ok"] is True
    assert payload["sync_mode"] == "nearest"
    assert payload["fallback_segment"]["point_count"] == 3
    # Points re-timed linearly across [0, duration_sec]:
    # offsets 0→5→10 min map to t_sec 0→300→600.
    assert [p["t_sec"] for p in payload["points"]] == [0.0, 300.0, 600.0]


def test_window_returns_ok_false_when_no_trkpt_at_all(tmp_path: Path, monkeypatch):
    """If the file has no trkpt elements anywhere, even the fallback
    can't produce points."""
    track_file = tmp_path / "track.gpx"
    _write_gpx(track_file, [[]])  # one empty segment, no waypoints
    monkeypatch.setattr(demo_track, "_TRACK_FILE", track_file)

    payload = demo_track.load_track_for_window(
        start_iso_utc="2026-04-19T22:00:00Z",
        duration_sec=300,
    )

    assert payload["ok"] is False
    assert payload["sync_mode"] == "none"
    assert payload["points"] == []


def test_window_exact_mode_when_overlap_exists(tmp_path: Path, monkeypatch):
    """If the window has any overlapping waypoints, don't fall back."""
    track_file = tmp_path / "track.gpx"
    _write_gpx(
        track_file,
        [[(49.1, -122.8, "2026-04-19T22:05:00.000Z")]],
    )
    monkeypatch.setattr(demo_track, "_TRACK_FILE", track_file)

    payload = demo_track.load_track_for_window(
        start_iso_utc="2026-04-19T22:00:00Z", duration_sec=600,
    )

    assert payload["ok"] is True
    assert payload["sync_mode"] == "exact"
    assert "fallback_segment" not in payload


def test_window_rejects_invalid_start_iso(tmp_path: Path, monkeypatch):
    track_file = tmp_path / "track.gpx"
    _write_gpx(track_file, [])
    monkeypatch.setattr(demo_track, "_TRACK_FILE", track_file)

    payload = demo_track.load_track_for_window(start_iso_utc="not-a-date", duration_sec=60)

    assert payload["ok"] is False
    assert "invalid window" in payload["error"]


def test_window_rejects_non_positive_duration(tmp_path: Path, monkeypatch):
    track_file = tmp_path / "track.gpx"
    _write_gpx(track_file, [])
    monkeypatch.setattr(demo_track, "_TRACK_FILE", track_file)

    payload = demo_track.load_track_for_window(
        start_iso_utc="2026-04-19T22:00:00Z", duration_sec=0,
    )

    assert payload["ok"] is False


def test_window_emits_speed_mps_with_noise_clamp(tmp_path: Path, monkeypatch):
    """Each emitted point carries a smoothed speed_mps; stationary jitter
    is clamped to 0 and a real motion edge surfaces the expected speed."""
    track_file = tmp_path / "track.gpx"
    _write_gpx(
        track_file,
        [
            [
                # 30 s of "stationary" with sub-metre jitter — should clamp to 0.
                (49.0000000, -122.0, "2026-04-19T22:00:00.000Z"),
                (49.0000020, -122.0, "2026-04-19T22:00:10.000Z"),
                (49.0000010, -122.0, "2026-04-19T22:00:20.000Z"),
                # Then a real ~111 m hop in 10 s ≈ 11.1 m/s.
                (49.0010000, -122.0, "2026-04-19T22:00:30.000Z"),
            ],
        ],
    )
    monkeypatch.setattr(demo_track, "_TRACK_FILE", track_file)

    payload = demo_track.load_track_for_window(
        start_iso_utc="2026-04-19T22:00:00Z", duration_sec=60,
    )

    assert payload["ok"] is True
    speeds = [p["speed_mps"] for p in payload["points"]]
    assert len(speeds) == 4
    # First three are stationary jitter — clamped to 0 by the noise floor.
    assert speeds[0] == 0.0
    assert speeds[1] == 0.0
    assert speeds[2] == 0.0
    # Final point: ~11.1 m/s, allow generous tolerance for haversine.
    assert 9.0 < speeds[3] < 13.0


def test_nearest_segment_speed_uses_real_timestamps(tmp_path: Path, monkeypatch):
    """When the fallback re-times points across the video duration, speed
    must still reflect the *real* GPS deltas — otherwise stretching a
    short slice across a long video would underreport speed."""
    track_file = tmp_path / "track.gpx"
    _write_gpx(
        track_file,
        [
            # 30 s of real driving (~111 m hops at 1 hop / 10 s ≈ 11 m/s),
            # ending an hour before the video so the fallback path fires.
            [
                (49.000, -122.0, "2026-04-19T21:00:00.000Z"),
                (49.001, -122.0, "2026-04-19T21:00:10.000Z"),
                (49.002, -122.0, "2026-04-19T21:00:20.000Z"),
                (49.003, -122.0, "2026-04-19T21:00:30.000Z"),
            ],
        ],
    )
    monkeypatch.setattr(demo_track, "_TRACK_FILE", track_file)

    # Stretch the 30 s segment across a 600 s video.
    payload = demo_track.load_track_for_window(
        start_iso_utc="2026-04-19T22:00:00Z", duration_sec=600,
    )

    assert payload["ok"] is True
    assert payload["sync_mode"] == "nearest"
    # If speed had been derived from the stretched t_sec, it would be 20×
    # smaller — well under 1 m/s. We expect ~11 m/s from the real deltas.
    moving = [p["speed_mps"] for p in payload["points"][1:]]
    assert all(s > 5.0 for s in moving), moving


def test_window_skips_segments_outside_fast_path(tmp_path: Path, monkeypatch):
    """Segments whose entire span sits outside the window shouldn't leak points."""
    track_file = tmp_path / "track.gpx"
    _write_gpx(
        track_file,
        [
            # segment fully before the window
            [(49.0, -122.0, "2026-04-19T10:05:00.000Z")],
            # segment overlapping the window (one waypoint inside, one outside)
            [
                (49.10, -122.80, "2026-04-19T22:05:00.000Z"),
                (49.15, -122.85, "2026-04-19T22:55:00.000Z"),
            ],
            # segment fully after the window
            [(49.9, -122.9, "2026-04-20T00:00:00.000Z")],
        ],
    )
    monkeypatch.setattr(demo_track, "_TRACK_FILE", track_file)

    # Window: 22:00 → 22:30. Only the 22:05 waypoint qualifies.
    payload = demo_track.load_track_for_window(
        start_iso_utc="2026-04-19T22:00:00Z", duration_sec=30 * 60,
    )

    assert payload["ok"] is True
    assert len(payload["points"]) == 1
    assert payload["points"][0]["t_sec"] == 300
    assert payload["points"][0]["lat"] == 49.10
