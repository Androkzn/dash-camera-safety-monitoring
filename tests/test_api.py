"""Tests for road_safety.api — feedback routes (unit-level, no live server)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def feedback_app(_isolate_data_dir):
    """Build a minimal FastAPI app with feedback routes mounted."""
    app = FastAPI()

    with patch("road_safety.api.feedback._DATA_DIR", _isolate_data_dir), \
         patch("road_safety.api.feedback._FEEDBACK_PATH", _isolate_data_dir / "feedback.jsonl"), \
         patch("road_safety.api.feedback._EVENTS_PATH", _isolate_data_dir / "events.json"):
        from road_safety.api.feedback import mount
        mount(app)
        yield TestClient(app), _isolate_data_dir


class TestFeedbackRoutes:
    def test_get_feedback_empty(self, feedback_app):
        client, data_dir = feedback_app
        resp = client.get("/api/feedback")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_post_feedback_tp(self, feedback_app):
        client, data_dir = feedback_app
        events_path = data_dir / "events.json"
        events_path.write_text(json.dumps([{"event_id": "e1", "risk_level": "high"}]))

        resp = client.post("/api/feedback", json={"event_id": "e1", "verdict": "tp"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True

        get_resp = client.get("/api/feedback")
        records = get_resp.json()["items"]
        assert len(records) == 1
        assert records[0]["verdict"] == "tp"

    def test_post_feedback_fp(self, feedback_app):
        client, data_dir = feedback_app
        events_path = data_dir / "events.json"
        events_path.write_text(json.dumps([{"event_id": "e2", "risk_level": "medium"}]))

        resp = client.post("/api/feedback", json={"event_id": "e2", "verdict": "fp"})
        assert resp.status_code == 200

    def test_post_feedback_invalid_verdict(self, feedback_app):
        client, _ = feedback_app
        resp = client.post("/api/feedback", json={"event_id": "e1", "verdict": "wrong"})
        assert resp.status_code in (400, 422)

    def test_coaching_queue(self, feedback_app):
        client, data_dir = feedback_app
        events_path = data_dir / "events.json"
        events_path.write_text(json.dumps([
            {"event_id": "e1", "risk_level": "medium", "event_type": "pedestrian_proximity"},
            {"event_id": "e2", "risk_level": "high", "event_type": "tailgating"},
            {"event_id": "e3", "risk_level": "low", "event_type": "tailgating"},
        ]))
        resp = client.get("/api/coaching_queue")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict)
        assert "items" in body or "count" in body or isinstance(body, list)
        items = body.get("items", body) if isinstance(body, dict) else body
        assert isinstance(items, list)
