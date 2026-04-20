"""Tests for the templates service.

Covers: CRUD against SQLite, default-template immutability, apply-time
re-validation + spec migration (drop unknown keys, fill missing keys with
current defaults, reject on cross-field violation).
"""

from __future__ import annotations

import pytest

from road_safety import settings_spec
from road_safety.services import settings_db, templates as template_svc


@pytest.fixture(autouse=True)
def _reset_db(tmp_path):
    """Re-point the singleton SQLite connection at a fresh per-test file."""
    settings_db._reset_for_tests(tmp_path / "settings.db")
    yield
    settings_db._reset_for_tests(None)


def test_default_template_is_synthetic_and_immutable() -> None:
    tmpls = template_svc.list_templates()
    assert tmpls[0]["id"] == template_svc.DEFAULT_TEMPLATE_ID
    assert tmpls[0]["system"] is True
    assert tmpls[0]["payload"]["CONF_THRESHOLD"] == settings_spec.spec_for("CONF_THRESHOLD").default

    with pytest.raises(PermissionError):
        template_svc.update_template(template_svc.DEFAULT_TEMPLATE_ID, name="rename")
    with pytest.raises(PermissionError):
        template_svc.soft_delete_template(template_svc.DEFAULT_TEMPLATE_ID)


def test_create_then_update_creates_immutable_revision() -> None:
    tmpl = template_svc.create_template(
        name="conservative",
        description="tighter thresholds",
        payload={"CONF_THRESHOLD": 0.65, "SLACK_HIGH_MIN_CONFIDENCE": 0.7},
        actor_label="op",
    )
    revs = template_svc.list_revisions(tmpl["id"])
    assert len(revs) == 1
    assert revs[0]["payload"]["CONF_THRESHOLD"] == 0.65

    template_svc.update_template(tmpl["id"], payload={"CONF_THRESHOLD": 0.7})
    revs = template_svc.list_revisions(tmpl["id"])
    assert len(revs) == 2
    assert revs[1]["payload"]["CONF_THRESHOLD"] == 0.7
    # Revision 1 is unchanged.
    assert revs[0]["payload"]["CONF_THRESHOLD"] == 0.65


def test_soft_delete_hides_from_list() -> None:
    tmpl = template_svc.create_template(
        name="ephemeral", description="", payload={}, actor_label="op"
    )
    assert template_svc.soft_delete_template(tmpl["id"]) is True
    visible_ids = [t["id"] for t in template_svc.list_templates()]
    assert tmpl["id"] not in visible_ids


def test_apply_drops_unknown_keys_and_fills_missing() -> None:
    # CONF_THRESHOLD=0.4 keeps the cross-field rule
    # (SLACK_HIGH_MIN_CONFIDENCE >= CONF_THRESHOLD) satisfied with the
    # spec default for the Slack key.
    tmpl = template_svc.create_template(
        name="with-old-key",
        description="",
        payload={"RETIRED_KEY": 99, "CONF_THRESHOLD": 0.4},
        actor_label="op",
    )
    plan = template_svc.prepare_template_apply(
        tmpl["id"], current_snapshot=settings_spec.defaults()
    )
    assert "RETIRED_KEY" in plan.dropped_keys
    # Every spec key absent from the stored payload should be filled.
    spec_keys = set(settings_spec.all_keys())
    stored_keys = {"CONF_THRESHOLD"}
    expected_filled = sorted(spec_keys - stored_keys)
    assert plan.filled_keys == expected_filled
    assert plan.validation_errors == []


def test_apply_rejects_cross_field_violation() -> None:
    tmpl = template_svc.create_template(
        name="broken",
        description="",
        payload={"TTC_HIGH_SEC": 1.5, "TTC_MED_SEC": 0.5},
        actor_label="op",
    )
    plan = template_svc.prepare_template_apply(
        tmpl["id"], current_snapshot=settings_spec.defaults()
    )
    keys = {e["key"] for e in plan.validation_errors}
    assert "TTC_MED_SEC" in keys


def test_payload_coercion_repairs_string_floats() -> None:
    tmpl = template_svc.create_template(
        name="stringy",
        description="",
        payload={"CONF_THRESHOLD": "0.55", "SLACK_HIGH_MIN_CONFIDENCE": "0.6"},
        actor_label="op",
    )
    plan = template_svc.prepare_template_apply(
        tmpl["id"], current_snapshot=settings_spec.defaults()
    )
    assert plan.cleaned_diff.get("CONF_THRESHOLD") == 0.55
    assert plan.validation_errors == []
