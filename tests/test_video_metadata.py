"""Tests for road_safety.services.video_metadata.

We avoid invoking the real ffprobe subprocess by monkeypatching
``subprocess.run``. The cache tests use a tmp_path file so the mtime is
real and the cache key is meaningful.
"""

import json
import subprocess
from pathlib import Path

from road_safety.services import video_metadata


def _fake_probe_output(
    *,
    creation_time: str | None = "2026-04-19T22:41:03.000000Z",
    duration: float = 653.85,
    width: int = 3840,
    height: int = 2160,
    fps: str = "60000/1001",
    codec: str = "h264",
) -> str:
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": codec,
                "width": width,
                "height": height,
                "avg_frame_rate": fps,
                "tags": {"creation_time": creation_time} if creation_time else {},
            }
        ],
        "format": {
            "duration": str(duration),
            "tags": {"creation_time": creation_time} if creation_time else {},
        },
    }
    return json.dumps(payload)


def _make_fake_run(stdout: str, returncode: int = 0):
    def _run(_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=_args, returncode=returncode, stdout=stdout, stderr=""
        )
    return _run


def test_probe_returns_parsed_metadata(tmp_path: Path, monkeypatch):
    video_metadata.reset_cache_for_tests()
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"not-a-real-video")  # just needs to exist

    monkeypatch.setattr(video_metadata, "_find_ffprobe", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(subprocess, "run", _make_fake_run(_fake_probe_output()))

    meta = video_metadata.probe(video)

    assert meta is not None
    assert meta.creation_time == "2026-04-19T22:41:03.000000Z"
    assert meta.duration_sec == 653.85
    assert meta.width == 3840
    assert meta.height == 2160
    assert 59 < meta.fps < 60  # 60000/1001 ≈ 59.94
    assert meta.codec == "h264"


def test_probe_returns_none_when_file_missing(tmp_path: Path, monkeypatch):
    video_metadata.reset_cache_for_tests()
    # Don't create the file.
    monkeypatch.setattr(video_metadata, "_find_ffprobe", lambda: "/usr/bin/ffprobe")
    # subprocess.run must never be reached when the file is missing.
    monkeypatch.setattr(subprocess, "run", _make_fake_run(""))

    meta = video_metadata.probe(tmp_path / "nope.mp4")

    assert meta is None


def test_probe_returns_none_when_ffprobe_unavailable(tmp_path: Path, monkeypatch):
    video_metadata.reset_cache_for_tests()
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")

    monkeypatch.setattr(video_metadata, "_find_ffprobe", lambda: None)

    assert video_metadata.probe(video) is None


def test_probe_returns_none_when_ffprobe_fails(tmp_path: Path, monkeypatch):
    video_metadata.reset_cache_for_tests()
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")

    monkeypatch.setattr(video_metadata, "_find_ffprobe", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(subprocess, "run", _make_fake_run("", returncode=1))

    assert video_metadata.probe(video) is None


def test_probe_cache_hit_skips_subprocess(tmp_path: Path, monkeypatch):
    video_metadata.reset_cache_for_tests()
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")

    calls = {"n": 0}

    def _counting_run(_args, **_kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(
            args=_args, returncode=0, stdout=_fake_probe_output(), stderr=""
        )

    monkeypatch.setattr(video_metadata, "_find_ffprobe", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(subprocess, "run", _counting_run)

    first = video_metadata.probe(video)
    second = video_metadata.probe(video)

    assert first is not None
    assert first == second
    assert calls["n"] == 1  # second call hit the cache


def test_probe_falls_back_to_format_creation_time(tmp_path: Path, monkeypatch):
    """When the stream lacks creation_time, use the format-level tag."""
    video_metadata.reset_cache_for_tests()
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"x")

    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30/1",
                "tags": {},  # no creation_time here
            }
        ],
        "format": {
            "duration": "120.0",
            "tags": {"creation_time": "2026-04-19T22:41:03Z"},
        },
    }

    monkeypatch.setattr(video_metadata, "_find_ffprobe", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(subprocess, "run", _make_fake_run(json.dumps(payload)))

    meta = video_metadata.probe(video)
    assert meta is not None
    assert meta.creation_time == "2026-04-19T22:41:03Z"
