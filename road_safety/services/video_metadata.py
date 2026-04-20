"""Video metadata extractor — thin wrapper around ``ffprobe``.

Role
----
Pull container-level metadata (creation time, duration, resolution,
codec) out of a video file so callers can align it with a GPS track
recorded over the same wallclock window. We intentionally do NOT try
to extract timed-metadata tracks (``com.apple.quicktime.location.ISO6709``
etc.) — in practice the sample footage shipped with this repo has been
transcoded and stripped of those tracks, so the value isn't there.

Why ffprobe instead of a Python MP4 parser
------------------------------------------
``ffprobe`` ships with every opencv install the project already depends
on, handles every container the rest of the pipeline can read, and its
JSON output is stable. A pure-Python MP4 atom walker would add a new
dep for no gain.

Cache
-----
Module-level dict keyed on the absolute path + mtime. The files are
on-disk assets that only change when the operator swaps them; re-probing
on every request would spawn a subprocess for nothing.
"""

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VideoMetadata:
    """Everything we can usefully read out of a transcoded MP4 container."""

    path: str
    creation_time: Optional[str]  # ISO-8601 UTC, e.g. "2026-04-19T22:41:03Z"
    duration_sec: float
    width: int
    height: int
    fps: float
    codec: Optional[str]

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "creation_time": self.creation_time,
            "duration_sec": self.duration_sec,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "codec": self.codec,
        }


# (abs_path, mtime_ns) → VideoMetadata. The mtime key means the cache
# auto-invalidates if the operator replaces the file on disk.
_CACHE: dict[tuple[str, int], VideoMetadata] = {}


def _find_ffprobe() -> Optional[str]:
    """Locate the ``ffprobe`` binary. Returns ``None`` if it's not on PATH."""
    return shutil.which("ffprobe")


def _parse_fps(rate: str) -> float:
    """Parse an ffprobe rate string like ``"60000/1001"`` into a float fps."""
    if "/" in rate:
        num_s, den_s = rate.split("/", 1)
        try:
            num = float(num_s)
            den = float(den_s)
        except ValueError:
            return 0.0
        return num / den if den > 0 else 0.0
    try:
        return float(rate)
    except ValueError:
        return 0.0


def probe(path: Path) -> Optional[VideoMetadata]:
    """Probe ``path`` and return its metadata. ``None`` on any failure.

    Failure modes (all logged, none raised):
      - file missing
      - ffprobe not installed
      - ffprobe exits non-zero
      - JSON output unexpectedly shaped

    Callers should treat ``None`` as "no usable metadata" and fall back
    gracefully (e.g. disable the video-synced track endpoint).
    """
    if not path.exists():
        log.warning("video_metadata: file not found: %s", path)
        return None

    mtime = path.stat().st_mtime_ns
    key = (str(path.resolve()), mtime)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    ffprobe = _find_ffprobe()
    if ffprobe is None:
        log.warning("video_metadata: ffprobe not on PATH — cannot probe %s", path.name)
        return None

    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("video_metadata: ffprobe invocation failed for %s: %s", path.name, exc)
        return None

    if proc.returncode != 0:
        log.warning(
            "video_metadata: ffprobe exit=%d for %s: %s",
            proc.returncode, path.name, proc.stderr.strip()[:200],
        )
        return None

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        log.warning("video_metadata: ffprobe JSON parse failed for %s: %s", path.name, exc)
        return None

    streams = data.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    fmt = data.get("format") or {}

    # creation_time may live on the stream, the format, or neither.
    creation = None
    if video_stream is not None:
        creation = (video_stream.get("tags") or {}).get("creation_time")
    if creation is None:
        creation = (fmt.get("tags") or {}).get("creation_time")

    try:
        duration = float(fmt.get("duration") or (video_stream or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    width = int((video_stream or {}).get("width") or 0)
    height = int((video_stream or {}).get("height") or 0)
    fps = _parse_fps(str((video_stream or {}).get("avg_frame_rate") or "0"))
    codec = (video_stream or {}).get("codec_name")

    meta = VideoMetadata(
        path=str(path),
        creation_time=creation,
        duration_sec=duration,
        width=width,
        height=height,
        fps=fps,
        codec=codec,
    )
    _CACHE[key] = meta
    log.info(
        "video_metadata: probed %s creation=%s duration=%.1fs %dx%d@%.2ffps codec=%s",
        path.name, creation, duration, width, height, fps, codec,
    )
    return meta


def reset_cache_for_tests() -> None:
    """Drop the probe cache. Exposed for tests only."""
    _CACHE.clear()
