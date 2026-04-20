"""Tests for the Settings Console FastAPI router.

Covers: bearer auth enforcement, validation 422 shape, ``If-Match`` 409,
apply-rate-limit 429, ticket exchange + single-use consumption, template
CRUD via HTTP, baseline + impact reads.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from road_safety.api.settings import (
    MIN_CHANGE_INTERVAL_SEC,
    _last_apply_at,
    mount as mount_settings_routes,
)
from road_safety.services import settings_db
from road_safety.services.impact import ImpactMonitor
from road_safety.settings_store import SettingsStore


@pytest.fixture()
def fresh_store(monkeypatch):
    """Replace the module-level STORE singleton with a fresh instance."""
    new_store = SettingsStore()
    import road_safety.api.settings as api_mod

    monkeypatch.setattr(api_mod, "STORE", new_store)
    monkeypatch.setattr("road_safety.settings_store.STORE", new_store)
    yield new_store


@pytest.fixture()
def admin_token(monkeypatch):
    token = "test-admin-token"
    monkeypatch.setattr("road_safety.config.ADMIN_TOKEN", token)
    monkeypatch.setattr("road_safety.api.settings.ADMIN_TOKEN", token)
    return token


@pytest.fixture()
def settings_client(tmp_path, fresh_store, admin_token, monkeypatch):
    """Build a minimal FastAPI app with the settings router mounted."""
    settings_db._reset_for_tests(tmp_path / "settings.db")
    # Reset the per-actor apply cooldown.
    _last_apply_at.clear()

    app = FastAPI()
    mon = ImpactMonitor(events_source=lambda: [])
    mount_settings_routes(app, impact_monitor=mon, impact_subscribers=[])
    client = TestClient(app)
    yield client, admin_token, mon
    settings_db._reset_for_tests(None)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------
def test_unauthenticated_read_returns_401(settings_client):
    client, _, _ = settings_client
    r = client.get("/api/settings/effective")
    assert r.status_code == 401


def test_wrong_token_returns_403(settings_client):
    client, _, _ = settings_client
    r = client.get("/api/settings/effective", headers=_auth("wrong"))
    assert r.status_code == 403


def test_unset_token_returns_503(settings_client, monkeypatch):
    # Re-patch ADMIN_TOKEN to None and re-mount on a fresh app.
    monkeypatch.setattr("road_safety.api.settings.ADMIN_TOKEN", None)
    app = FastAPI()
    mon = ImpactMonitor(events_source=lambda: [])
    mount_settings_routes(app, impact_monitor=mon, impact_subscribers=[])
    c = TestClient(app)
    r = c.get("/api/settings/effective", headers=_auth("anything"))
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def test_get_schema(settings_client):
    client, token, _ = settings_client
    r = client.get("/api/settings/schema", headers=_auth(token))
    assert r.status_code == 200
    payload = r.json()
    assert payload["schema_version"] >= 1
    assert any(s["key"] == "CONF_THRESHOLD" for s in payload["settings"])


def test_get_effective(settings_client):
    client, token, _ = settings_client
    r = client.get("/api/settings/effective", headers=_auth(token))
    assert r.status_code == 200
    payload = r.json()
    assert "values" in payload
    assert "revision_hash" in payload


def test_default_template_present_in_list(settings_client):
    client, token, _ = settings_client
    r = client.get("/api/settings/templates", headers=_auth(token))
    assert r.status_code == 200
    tmpls = r.json()["templates"]
    assert tmpls[0]["id"] == "tpl_default"
    assert tmpls[0]["system"] is True


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------
def test_validate_returns_resolved_diff(settings_client):
    client, token, _ = settings_client
    r = client.post(
        "/api/settings/validate",
        headers=_auth(token),
        json={"diff": {"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.65}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["resolved_diff"]["CONF_THRESHOLD"] == 0.6


def test_validate_returns_422_with_errors(settings_client):
    client, token, _ = settings_client
    r = client.post(
        "/api/settings/validate",
        headers=_auth(token),
        json={"diff": {"TTC_HIGH_SEC": 5.0, "TTC_MED_SEC": 1.0}},
    )
    assert r.status_code == 422
    keys = {e["key"] for e in r.json()["errors"]}
    assert "TTC_HIGH_SEC" in keys


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def test_apply_success_then_reflected_in_effective(settings_client):
    client, token, _ = settings_client
    r = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={
            "diff": {"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.65},
            "operator_label": "test",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "CONF_THRESHOLD" in body["applied_now"]
    eff = client.get("/api/settings/effective", headers=_auth(token)).json()
    assert eff["values"]["CONF_THRESHOLD"] == 0.6


def test_apply_validation_error_returns_422(settings_client):
    client, token, _ = settings_client
    r = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={"diff": {"TTC_HIGH_SEC": 99.0}, "operator_label": "test"},
    )
    assert r.status_code == 422


def test_apply_revision_conflict_returns_409(settings_client):
    client, token, _ = settings_client
    r = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={
            "diff": {"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.65},
            "expected_revision_hash": "definitely-not-current",
            "operator_label": "test",
        },
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "revision_conflict"


def test_apply_privacy_confirm_required(settings_client):
    client, token, _ = settings_client
    r = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={"diff": {"ALPR_MODE": "on"}, "operator_label": "test"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "privacy_confirm_required"
    # With the flag it goes through.
    r2 = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={
            "diff": {"ALPR_MODE": "on"},
            "confirm_privacy_change": True,
            "operator_label": "test2",
        },
    )
    assert r2.status_code == 200


def test_apply_rate_limit_429(settings_client):
    client, token, _ = settings_client
    actor = "rate-test"
    body = {
        "diff": {"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.65},
        "operator_label": actor,
    }
    r1 = client.post("/api/settings/apply", headers=_auth(token), json=body)
    assert r1.status_code == 200
    # Immediate second hit is below the cooldown.
    r2 = client.post("/api/settings/apply", headers=_auth(token), json=body)
    assert r2.status_code == 429
    assert "Retry-After" in r2.headers


def test_failed_apply_does_not_burn_cooldown(settings_client):
    """A validation-failing apply must not lock the operator out.

    Regression test for the eager-stamp bug: previously the cooldown was
    stamped at the top of ``apply`` before validation ran, so a typo (e.g.
    ``TTC_HIGH_SEC=99`` violating the relative-ordering rule) would 422
    *and then* 429 every retry for the next 5 seconds — leaving the
    operator unable to fix their own diff.
    """
    client, token, _ = settings_client
    actor = "fail-then-succeed"
    bad = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={"diff": {"TTC_HIGH_SEC": 99.0}, "operator_label": actor},
    )
    assert bad.status_code == 422
    # Same actor, immediately, with a valid diff: must succeed (not 429).
    good = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={
            "diff": {"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.65},
            "operator_label": actor,
        },
    )
    assert good.status_code == 200, good.json()


def test_privacy_confirm_two_step_same_actor_succeeds(settings_client):
    """The privacy-confirm two-step is one logical operation; the second
    call from the same actor must not be blocked by the cooldown."""
    client, token, _ = settings_client
    actor = "privacy-flow"
    r1 = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={"diff": {"ALPR_MODE": "on"}, "operator_label": actor},
    )
    assert r1.status_code == 400
    assert r1.json()["error"] == "privacy_confirm_required"
    r2 = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={
            "diff": {"ALPR_MODE": "on"},
            "confirm_privacy_change": True,
            "operator_label": actor,
        },
    )
    assert r2.status_code == 200, r2.json()


def test_revision_conflict_does_not_burn_cooldown(settings_client):
    """A 409 from ``expected_revision_hash`` mismatch should not stamp
    the cooldown — the operator needs to refetch and retry immediately."""
    client, token, _ = settings_client
    actor = "etag-flow"
    bad = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={
            "diff": {"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.65},
            "expected_revision_hash": "stale-hash",
            "operator_label": actor,
        },
    )
    assert bad.status_code == 409
    good = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={
            "diff": {"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.65},
            "operator_label": actor,
        },
    )
    assert good.status_code == 200, good.json()


def test_template_apply_missing_does_not_burn_cooldown(settings_client):
    """404 on a missing template must not lock the operator out."""
    client, token, _ = settings_client
    actor = "missing-template"
    miss = client.post(
        "/api/settings/templates/tpl_does_not_exist/apply",
        headers=_auth(token),
        json={"operator_label": actor},
    )
    assert miss.status_code == 404
    good = client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={
            "diff": {"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.65},
            "operator_label": actor,
        },
    )
    assert good.status_code == 200, good.json()


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def test_template_create_apply_delete_round_trip(settings_client):
    client, token, _ = settings_client
    r = client.post(
        "/api/settings/templates",
        headers=_auth(token),
        json={
            "name": "tight",
            "description": "tighter",
            "payload": {"CONF_THRESHOLD": 0.65, "SLACK_HIGH_MIN_CONFIDENCE": 0.7},
        },
    )
    assert r.status_code == 200
    tmpl = r.json()
    tmpl_id = tmpl["id"]

    # Apply it via HTTP.
    _last_apply_at.clear()
    r = client.post(
        f"/api/settings/templates/{tmpl_id}/apply",
        headers=_auth(token),
        json={"operator_label": "tester"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Delete it.
    r = client.delete(f"/api/settings/templates/{tmpl_id}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    # System template delete is rejected.
    r = client.delete("/api/settings/templates/tpl_default", headers=_auth(token))
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Tickets + SSE
# ---------------------------------------------------------------------------
def test_stream_ticket_issue_and_consume(settings_client):
    client, token, _ = settings_client
    r = client.post(
        "/api/settings/stream_ticket",
        headers=_auth(token),
        json={"operator_label": "tester"},
    )
    assert r.status_code == 200
    ticket = r.json()["ticket"]
    assert len(ticket) >= 16

    # Replay rejected (single-use). We check the consume helper directly to
    # avoid keeping the SSE connection open in the test client.
    import asyncio

    from road_safety.api.settings import _consume_ticket

    actor1 = asyncio.run(_consume_ticket(ticket))
    assert actor1 == "tester"
    actor2 = asyncio.run(_consume_ticket(ticket))
    assert actor2 is None


def test_observability_counters_exposed(settings_client):
    client, token, _ = settings_client
    # Trigger one success + one validation_error so counters are non-zero.
    client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={
            "diff": {"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.65},
            "operator_label": "obs",
        },
    )
    _last_apply_at.clear()
    client.post(
        "/api/settings/apply",
        headers=_auth(token),
        json={"diff": {"TTC_HIGH_SEC": 99.0}, "operator_label": "obs2"},
    )
    r = client.get("/api/settings/observability", headers=_auth(token))
    assert r.status_code == 200
    counters = r.json()["counters"]
    assert counters["settings_apply_total_success"] >= 1
    assert counters["settings_apply_total_validation_error"] >= 1
