"""Shared test fixtures for the Road Safety test suite."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Redirect DATA_DIR / THUMBS_DIR to a temp directory for every test."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "thumbnails").mkdir()
    (data / "corpus").mkdir()
    (data / "active_learning" / "pending").mkdir(parents=True)

    monkeypatch.setattr("road_safety.config.DATA_DIR", data)
    monkeypatch.setattr("road_safety.config.THUMBS_DIR", data / "thumbnails")
    monkeypatch.setattr("road_safety.config.CORPUS_DIR", data / "corpus")
    yield data


@pytest.fixture()
def sample_detection():
    """A minimal Detection object for unit tests."""
    from road_safety.core.detection import Detection
    return Detection(cls="person", conf=0.85, x1=100, y1=200, x2=160, y2=400, track_id=7)


@pytest.fixture()
def sample_vehicle():
    from road_safety.core.detection import Detection
    return Detection(cls="car", conf=0.92, x1=300, y1=250, x2=500, y2=450, track_id=12)


@pytest.fixture()
def sample_event():
    """A complete event dict matching the shape produced by server.py."""
    return {
        "event_id": "evt_1700000000000_0001",
        "vehicle_id": "vehicle_01",
        "road_id": "road_test",
        "driver_id": "driver_01",
        "video_id": "live_stream",
        "timestamp_sec": 12.5,
        "wall_time": "2026-04-15T10:00:00Z",
        "event_type": "pedestrian_proximity",
        "risk_level": "high",
        "confidence": 0.82,
        "objects": ["car", "person"],
        "track_ids": [7, 12],
        "episode_duration_sec": 1.5,
        "ttc_sec": 1.2,
        "distance_m": 2.8,
        "distance_px": 45.0,
        "summary": "Person and car within 45px (risk=high).",
        "narration": None,
        "thumbnail": "thumbnails/evt_1700000000000_0001_public.jpg",
    }


@pytest.fixture()
def mock_yolo_model():
    """A MagicMock standing in for the YOLO model so we never load weights in tests."""
    model = MagicMock()
    model.track.return_value = [MagicMock(
        names={0: "person", 2: "car"},
        boxes=None,
    )]
    model.return_value = [MagicMock(
        names={0: "person", 2: "car"},
        boxes=None,
    )]
    return model
