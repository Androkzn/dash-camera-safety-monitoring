"""Tests for ``road_safety.settings_store.SettingsStore``.

Covers: snapshot/apply atomicity, validation, cross-field rules, subscriber
isolation, ``If-Match`` revision conflicts, privacy-confirm gate, rollback,
and the warm-reload deque rebuild for ``TRACK_HISTORY_LEN``.
"""

from __future__ import annotations

import pytest

from road_safety import settings_spec
from road_safety.settings_store import (
    PrivacyConfirmRequired,
    RevisionConflict,
    SettingsStore,
    SettingsValidationError,
)


@pytest.fixture()
def store() -> SettingsStore:
    """Fresh store with default seed values for each test."""
    return SettingsStore()


def test_snapshot_is_immutable(store: SettingsStore) -> None:
    snap = store.snapshot()
    with pytest.raises(TypeError):
        snap["CONF_THRESHOLD"] = 0.99  # type: ignore[index]


def test_apply_diff_atomic_swap(store: SettingsStore) -> None:
    before_hash = store.revision_hash()
    result = store.apply_diff(
        {"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.7},
        actor="op",
    )
    assert result.ok
    assert sorted(result.applied_now) == ["CONF_THRESHOLD", "SLACK_HIGH_MIN_CONFIDENCE"]
    assert result.revision_hash_before == before_hash
    assert result.revision_hash_after != before_hash
    assert store.snapshot()["CONF_THRESHOLD"] == 0.6


def test_validation_failure_does_not_swap(store: SettingsStore) -> None:
    before = store.revision_hash()
    with pytest.raises(SettingsValidationError) as exc:
        store.apply_diff({"TTC_HIGH_SEC": 5.0, "TTC_MED_SEC": 1.0})
    keys = {e["key"] for e in exc.value.errors}
    assert "TTC_HIGH_SEC" in keys
    assert "TTC_MED_SEC" in keys
    assert store.revision_hash() == before  # snapshot untouched


def test_revision_conflict_when_etag_stale(store: SettingsStore) -> None:
    # First apply moves the revision forward.
    store.apply_diff({"CONF_THRESHOLD": 0.55, "SLACK_HIGH_MIN_CONFIDENCE": 0.6}, actor="a")
    with pytest.raises(RevisionConflict):
        store.apply_diff(
            {"CONF_THRESHOLD": 0.6},
            expected_revision_hash="not-the-current-hash",
        )


def test_privacy_confirm_required_gate(store: SettingsStore) -> None:
    with pytest.raises(PrivacyConfirmRequired):
        store.apply_diff({"ALPR_MODE": "on"})
    # With explicit confirm it goes through.
    result = store.apply_diff({"ALPR_MODE": "on"}, confirm_privacy_change=True)
    assert "ALPR_MODE" in result.applied_now
    assert store.snapshot()["ALPR_MODE"] == "on"


def test_subscriber_exception_isolated(store: SettingsStore) -> None:
    """A raising subscriber must surface as a warning — never abort apply."""
    fired: list[str] = []

    def good(_b, _a):
        fired.append("good")

    def bad(_b, _a):
        raise RuntimeError("boom")

    store.register_subscriber(good, name="good")
    store.register_subscriber(bad, name="bad")

    result = store.apply_diff({"CONF_THRESHOLD": 0.55, "SLACK_HIGH_MIN_CONFIDENCE": 0.6})
    assert result.ok
    assert "good" in fired
    assert any("bad" in w and "boom" in w for w in result.warnings)
    assert store.counters["settings_apply_total_subscriber_error"] >= 1
    # Snapshot still reflects the apply.
    assert store.snapshot()["CONF_THRESHOLD"] == 0.55


def test_subscriber_keys_filter(store: SettingsStore) -> None:
    fired: list[str] = []
    store.register_subscriber_for(
        ["LLM_BUCKET_CAPACITY"], lambda b, a: fired.append("llm"), name="llm"
    )
    # Touch a different key — should not fire.
    store.apply_diff({"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.7})
    assert fired == []
    # Touch the watched key — should fire.
    store.apply_diff({"LLM_BUCKET_CAPACITY": 5.0})
    assert fired == ["llm"]


def test_rollback_returns_to_last_good(store: SettingsStore) -> None:
    store.apply_diff(
        {"CONF_THRESHOLD": 0.7, "SLACK_HIGH_MIN_CONFIDENCE": 0.75},
        actor="a",
    )
    after_hash = store.revision_hash()
    result = store.rollback_to_last_good()
    assert result.ok
    # CONF_THRESHOLD should be back to its boot default.
    assert store.snapshot()["CONF_THRESHOLD"] == settings_spec.spec_for("CONF_THRESHOLD").default
    assert result.revision_hash_before == after_hash


def test_unknown_key_dropped_with_warning(store: SettingsStore) -> None:
    result = store.apply_diff({"NOT_A_REAL_KEY": 123})
    assert result.ok
    assert any("unknown key dropped" in w for w in result.warnings)


def test_track_history_warm_reload_resizes_deque() -> None:
    """TrackHistory must rebuild per-track deques on TRACK_HISTORY_LEN change."""
    from collections import deque

    from road_safety.core.detection import TrackHistory

    th = TrackHistory(maxlen=12)
    # Stuff one track with 12 samples.
    for i in range(12):
        from road_safety.core.detection import TrackSample
        th._tracks[1] = th._tracks.get(1, deque(maxlen=12))
        th._tracks[1].append(TrackSample(t=float(i), height=10, bottom=20))
    assert len(th._tracks[1]) == 12

    # Trigger the warm reload via the singleton store.
    from road_safety.settings_store import STORE
    STORE.apply_diff({"TRACK_HISTORY_LEN": 6})
    assert th._maxlen == 6
    assert len(th._tracks[1]) == 6
    # Restore default for downstream tests.
    STORE.apply_diff({"TRACK_HISTORY_LEN": 12})


def test_counters_increment(store: SettingsStore) -> None:
    before = store.counters["settings_apply_total_success"]
    store.apply_diff({"CONF_THRESHOLD": 0.6, "SLACK_HIGH_MIN_CONFIDENCE": 0.65})
    assert store.counters["settings_apply_total_success"] == before + 1

    with pytest.raises(SettingsValidationError):
        store.apply_diff({"TTC_HIGH_SEC": 99.0})
    assert store.counters["settings_apply_total_validation_error"] >= 1
