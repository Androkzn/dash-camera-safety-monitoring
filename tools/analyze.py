"""
Safety event extraction — batch mode.

Reads a video file, samples frames, runs YOLO, emits structured safety events
with thumbnails. Shares core detection logic with the live server (detection.py).

Usage:
    python analyze.py data/input.mp4
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import cv2

from road_safety.core.detection import (
    TrackHistory,
    build_event_summary,
    classify_risk,
    detect_frame,
    estimate_distance_m,
    estimate_ttc_sec,
    find_interactions,
    load_model,
)
from road_safety.services.redact import public_thumbnail_name, write_thumbnails
from road_safety.config import DATA_DIR
THUMBS_DIR = DATA_DIR / "thumbnails"
EVENTS_PATH = DATA_DIR / "events.json"
SUMMARY_PATH = DATA_DIR / "summary.json"

SAMPLE_FPS = 2
EVENT_MERGE_WINDOW_SEC = 2.0


def merge_events(raw: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for ev in sorted(raw, key=lambda e: e["timestamp_sec"]):
        if merged:
            last = merged[-1]
            same_type = last["event_type"] == ev["event_type"]
            close_in_time = ev["timestamp_sec"] - last["timestamp_sec"] <= EVENT_MERGE_WINDOW_SEC
            if same_type and close_in_time:
                if ev["_distance"] < last["_distance"]:
                    merged[-1] = ev
                continue
        merged.append(ev)
    for ev in merged:
        ev.pop("_distance", None)
    return merged


def build_summary(video_id: str, duration_sec: float, events: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    for ev in events:
        by_type[ev["event_type"]] = by_type.get(ev["event_type"], 0) + 1
        by_risk[ev["risk_level"]] = by_risk.get(ev["risk_level"], 0) + 1

    if by_risk.get("high", 0) >= 2:
        trip_class = "risky"
    elif by_risk.get("high", 0) + by_risk.get("medium", 0) >= 2:
        trip_class = "moderate"
    else:
        trip_class = "safe"

    parts = [f"{count} {etype.replace('_', ' ')}" for etype, count in by_type.items()]
    narrative = (
        f"This trip contained {len(events)} safety event(s): " + ", ".join(parts) + "."
        if events
        else "No safety events detected in this trip."
    )

    avg_conf = round(sum(e["confidence"] for e in events) / len(events), 3) if events else 0.0

    return {
        "video_id": video_id,
        "duration_sec": round(duration_sec, 2),
        "trip_classification": trip_class,
        "event_count": len(events),
        "events_by_type": by_type,
        "events_by_risk": by_risk,
        "avg_confidence": avg_conf,
        "narrative": narrative,
    }


def analyze(video_path: Path) -> None:
    if not video_path.exists():
        sys.exit(f"Video not found: {video_path}")

    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    for old in THUMBS_DIR.glob("*.jpg"):
        old.unlink()
    for old in THUMBS_DIR.glob("*_public.jpg"):
        old.unlink()

    video_id = video_path.stem
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps
    step = max(int(fps / SAMPLE_FPS), 1)

    print(f"Video: {video_id}  fps={fps:.1f}  frames={total_frames}  duration={duration_sec:.1f}s")
    print(f"Sampling every {step} frames (~{SAMPLE_FPS} fps)")

    model = load_model()
    track_history = TrackHistory()

    raw_events: list[dict] = []
    frame_idx = 0
    processed = 0
    started = time.time()

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % step != 0:
            frame_idx += 1
            continue

        timestamp = frame_idx / fps
        detections = detect_frame(model, frame)
        frame_h = frame.shape[0]
        live_ids: set[int] = set()
        for det in detections:
            if det.track_id is not None:
                live_ids.add(det.track_id)
                track_history.update(det, timestamp)
        track_history.prune(live_ids, timestamp)

        for event_type, a, b, distance in find_interactions(detections):
            ttc = None
            for sub in (a, b):
                cand = estimate_ttc_sec(track_history.samples(sub.track_id))
                if cand is not None and (ttc is None or cand < ttc):
                    ttc = cand
            dist_m = None
            for sub in (a, b):
                cand = estimate_distance_m(sub, frame_h)
                if cand is not None and (dist_m is None or cand < dist_m):
                    dist_m = cand
            risk = classify_risk(ttc, dist_m, distance)
            if event_type == "pedestrian_proximity" and risk == "low":
                continue
            event_id = f"evt_{len(raw_events):04d}"
            internal_name = f"{event_id}.jpg"
            public_name = public_thumbnail_name(internal_name)
            write_thumbnails(
                frame, detections, a, b,
                THUMBS_DIR / internal_name,
                THUMBS_DIR / public_name,
            )
            raw_events.append(
                {
                    "event_id": event_id,
                    "video_id": video_id,
                    "timestamp_sec": round(timestamp, 2),
                    "event_type": event_type,
                    "risk_level": risk,
                    "confidence": round(min(a.conf, b.conf), 3),
                    "objects": sorted({a.cls, b.cls}),
                    "track_ids": [t for t in (a.track_id, b.track_id) if t is not None],
                    "ttc_sec": ttc,
                    "distance_m": dist_m,
                    "distance_px": round(distance, 1),
                    "summary": build_event_summary(
                        event_type, a, b, distance, risk,
                        ttc_sec=ttc, distance_m=dist_m,
                    ),
                    "thumbnail": f"thumbnails/{public_name}",
                    "_distance": distance,
                }
            )
        processed += 1
        frame_idx += 1

    cap.release()

    events = merge_events(raw_events)
    for i, ev in enumerate(events):
        ev["event_id"] = f"evt_{i:04d}"

    EVENTS_PATH.write_text(json.dumps(events, indent=2))
    summary = build_summary(video_id, duration_sec, events)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))

    elapsed = time.time() - started
    print(
        f"Done. Processed {processed} frames in {elapsed:.1f}s "
        f"({processed / max(elapsed, 1e-6):.1f} fps). "
        f"Wrote {len(events)} event(s)."
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python analyze.py <video_path>")
    analyze(Path(sys.argv[1]))
