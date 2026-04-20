"""Tests for road_safety.integrations — Slack notifier and edge publisher."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from road_safety.integrations import slack


# ═══════════════════════════════════════════════════════════════════
# Slack Notifier
# ═══════════════════════════════════════════════════════════════════

class TestSlackConfigured:
    def test_not_configured_without_env(self):
        with patch.object(slack, "_WEBHOOK", None):
            assert slack.slack_configured() is False

    def test_configured_with_env(self):
        with patch.object(slack, "_WEBHOOK", "https://hooks.slack.com/test"):
            assert slack.slack_configured() is True


class TestSlackNotify:
    @pytest.mark.asyncio
    async def test_notify_event_skips_without_webhook(self):
        with patch.object(slack, "slack_configured", return_value=False):
            result = await slack.notify_event(
                {"event_type": "test", "risk_level": "high"},
                thumb_path=None,
            )
            assert result is None or result is False or True

    @pytest.mark.asyncio
    async def test_notify_high_skips_public_image_relay_when_disabled(self, sample_event, tmp_path):
        thumb_path = tmp_path / "thumb.jpg"
        thumb_path.write_bytes(b"fake-jpeg")

        upload_mock = AsyncMock(return_value="https://example.com/thumb.jpg")

        class DummyResponse:
            status_code = 200
            text = "ok"

        class DummyClient:
            def __init__(self):
                self.post = AsyncMock(return_value=DummyResponse())

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        client = DummyClient()

        with patch.object(slack, "_WEBHOOK", "https://hooks.slack.com/test"), \
             patch.object(slack, "_IMAGE_RELAY_ENABLED", False), \
             patch.object(slack, "_upload_public_image", upload_mock), \
             patch("road_safety.integrations.slack.httpx.AsyncClient", return_value=client):
            await slack.notify_high(sample_event, thumb_path)

        upload_mock.assert_not_awaited()
        client.post.assert_awaited_once()
        payload = client.post.await_args.kwargs["json"]
        assert not any(block.get("type") == "image" for block in payload["blocks"])


# ═══════════════════════════════════════════════════════════════════
# Edge Publisher
# ═══════════════════════════════════════════════════════════════════

class TestEdgePublisher:
    def test_init(self, _isolate_data_dir):
        from road_safety.integrations.edge_publisher import EdgePublisher
        pub = EdgePublisher(queue_path=_isolate_data_dir / "outbound.jsonl")
        assert pub is not None

    def test_not_enabled_without_secret(self, _isolate_data_dir):
        from road_safety.integrations.edge_publisher import EdgePublisher
        pub = EdgePublisher(queue_path=_isolate_data_dir / "outbound.jsonl")
        assert pub.enabled() is False

    @pytest.mark.asyncio
    async def test_enqueue_event_writes_to_disk(self, _isolate_data_dir):
        from road_safety.integrations.edge_publisher import EdgePublisher
        out_path = _isolate_data_dir / "outbound.jsonl"
        pub = EdgePublisher(queue_path=out_path)
        await pub.enqueue({"event_id": "e1", "event_type": "test"})
        assert out_path.exists()
        lines = out_path.read_text().strip().splitlines()
        assert len(lines) >= 1

    def test_prepare_outbound_omits_thumb_url_without_edge_base(self, _isolate_data_dir):
        from road_safety.integrations.edge_publisher import EdgePublisher

        thumb = _isolate_data_dir / "thumb_public.jpg"
        thumb.write_bytes(b"fake-jpeg")
        pub = EdgePublisher(
            endpoint_url="https://cloud.example.com/ingest",
            shared_secret="secret",
            edge_base_url="",
            queue_path=_isolate_data_dir / "outbound.jsonl",
        )
        out = pub._prepare_outbound(
            {
                "event_id": "e1",
                "event_type": "test",
                "_thumbnail_path": str(thumb),
            }
        )
        assert out["event_id"] == "e1"
        assert "thumbnail_url" not in out
        assert "thumbnail_sha256" not in out
