"""Tests for request guards and public-thumbnail signed URLs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from road_safety.security import require_bearer_token


def _guard_app(token: str | None):
    app = FastAPI()

    @app.get("/protected")
    def protected(request: Request):
        require_bearer_token(
            request,
            token,
            realm="admin",
            env_var="ROAD_ADMIN_TOKEN",
        )
        return {"ok": True}

    return app


def _make_request(path: str, query: str = "", headers: dict[str, str] | None = None) -> Request:
    hdrs = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "headers": hdrs,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


class TestBearerGuard:
    def test_guard_disabled_without_token(self):
        client = TestClient(_guard_app(None))
        resp = client.get("/protected")
        assert resp.status_code == 503

    def test_guard_rejects_missing_header(self):
        client = TestClient(_guard_app("secret-token"))
        resp = client.get("/protected")
        assert resp.status_code == 401

    def test_guard_rejects_wrong_token(self):
        client = TestClient(_guard_app("secret-token"))
        resp = client.get("/protected", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 403

    def test_guard_accepts_valid_token(self):
        client = TestClient(_guard_app("secret-token"))
        resp = client.get("/protected", headers={"Authorization": "Bearer secret-token"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestSensitiveFeedback:
    @pytest.mark.asyncio
    async def test_feedback_uses_matched_vehicle_id(self, monkeypatch):
        import road_safety.server as server

        monkeypatch.setattr(server.audit, "log", MagicMock())
        monkeypatch.setattr(server.road_registry, "record_feedback", MagicMock())
        monkeypatch.setattr(
            server.state.drift,
            "compute",
            MagicMock(return_value=SimpleNamespace(alert_triggered=False)),
        )

        await server._on_feedback(
            {"event_id": "evt_1", "verdict": "tp", "note": None},
            {"event_id": "evt_1", "vehicle_id": "vehicle_02"},
        )

        server.road_registry.record_feedback.assert_called_once_with(
            "evt_1",
            "tp",
            "vehicle_02",
        )


class TestPublicThumbnailSigning:
    def test_valid_thumb_request_accepts_when_guard_disabled(self, monkeypatch):
        import road_safety.server as server

        monkeypatch.setattr(server, "PUBLIC_THUMBS_REQUIRE_TOKEN", False)
        req = _make_request("/thumbnails/e_public.jpg")
        assert server._valid_thumb_request("e_public.jpg", req) is True

    def test_valid_thumb_request_rejects_missing_query_when_enabled(self, monkeypatch):
        import road_safety.server as server

        monkeypatch.setattr(server, "PUBLIC_THUMBS_REQUIRE_TOKEN", True)
        monkeypatch.setattr(server, "THUMB_SIGNING_SECRET", "thumb-secret")
        req = _make_request("/thumbnails/e_public.jpg")
        assert server._valid_thumb_request("e_public.jpg", req) is False

    def test_valid_thumb_request_accepts_valid_signature(self, monkeypatch):
        import road_safety.server as server

        now = 1_700_000_000
        exp = now + 120
        monkeypatch.setattr(server, "PUBLIC_THUMBS_REQUIRE_TOKEN", True)
        monkeypatch.setattr(server, "THUMB_SIGNING_SECRET", "thumb-secret")
        monkeypatch.setattr(server.time, "time", lambda: now)
        token = server._thumb_token("e_public.jpg", exp)
        req = _make_request(
            "/thumbnails/e_public.jpg",
            query=f"exp={exp}&token={token}",
        )
        assert server._valid_thumb_request("e_public.jpg", req) is True

    def test_thumbnail_public_denies_invalid_token(self, monkeypatch, _isolate_data_dir):
        import road_safety.server as server

        name = "evt_public.jpg"
        thumbs_dir = _isolate_data_dir / "thumbnails"
        (thumbs_dir / name).write_bytes(b"jpeg")

        monkeypatch.setattr(server, "THUMBS_DIR", thumbs_dir)
        monkeypatch.setattr(server, "PUBLIC_THUMBS_REQUIRE_TOKEN", True)
        monkeypatch.setattr(server, "THUMB_SIGNING_SECRET", "thumb-secret")
        monkeypatch.setattr(server.audit, "log", MagicMock())

        req = _make_request(f"/thumbnails/{name}", query="exp=1&token=bad")
        with pytest.raises(HTTPException) as exc:
            server.thumbnail(name, req)

        assert exc.value.status_code == 403
        server.audit.log.assert_called_once()

    def test_thumbnail_public_allows_valid_token(self, monkeypatch, _isolate_data_dir):
        import road_safety.server as server

        now = 1_700_000_000
        exp = now + 60
        name = "evt_public.jpg"
        thumbs_dir = _isolate_data_dir / "thumbnails"
        (thumbs_dir / name).write_bytes(b"jpeg")

        monkeypatch.setattr(server, "THUMBS_DIR", thumbs_dir)
        monkeypatch.setattr(server, "PUBLIC_THUMBS_REQUIRE_TOKEN", True)
        monkeypatch.setattr(server, "THUMB_SIGNING_SECRET", "thumb-secret")
        monkeypatch.setattr(server.time, "time", lambda: now)
        token = server._thumb_token(name, exp)

        req = _make_request(
            f"/thumbnails/{name}",
            query=f"exp={exp}&token={token}",
        )
        resp = server.thumbnail(name, req)
        assert isinstance(resp, FileResponse)
