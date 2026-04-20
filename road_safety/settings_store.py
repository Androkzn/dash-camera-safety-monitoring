"""Settings Console — runtime settings store with atomic apply + subscribers.

The store owns one in-memory snapshot of every operator-tunable parameter.
Hot-path code reads it via ``STORE.snapshot()`` (returns a frozen
``MappingProxyType`` view, lock-free for readers) and writes go through
``STORE.apply_diff(...)`` which:

1. Validates the merged snapshot against :mod:`road_safety.settings_spec`.
2. Honours the ``If-Match`` lost-update protection
   (``expected_revision_hash``).
3. Builds a new immutable snapshot and atomically rebinds it under a short
   ``RLock``.
4. Records the previous snapshot as ``last_known_good`` for ``rollback()``.
5. Fans out to subscribers — each callback runs inside its own
   ``try/except`` so a misbehaving consumer never reverts the snapshot.
6. Returns an :class:`AppliedResult` that the API layer translates into the
   wire response.

The store is a process-wide singleton (``STORE`` at the bottom of the file).
There is exactly one settings snapshot per edge process — out-of-scope for
v1: HA / multi-instance with leader election (see plan §S2).
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from road_safety import settings_spec


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
class SettingsValidationError(Exception):
    """Raised by :meth:`SettingsStore.apply_diff` on schema / range failure.

    The ``errors`` attribute is the flat list returned by
    :func:`settings_spec.validate` so the API layer can render it as a 422
    body without further marshalling.
    """

    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__(f"settings validation failed: {len(errors)} error(s)")
        self.errors = errors


class RevisionConflict(Exception):
    """Raised when ``expected_revision_hash`` does not match the live snapshot.

    The store's revision hash advanced between the operator opening the
    page and pressing apply. The API layer translates this into a 409 with
    the current ``revision_hash`` so the UI can prompt for a refresh.
    """

    def __init__(self, expected: str, actual: str):
        super().__init__(f"revision conflict: expected={expected} actual={actual}")
        self.expected = expected
        self.actual = actual


class PrivacyConfirmRequired(Exception):
    """Raised when an ALPR-mode flip is attempted without the explicit confirm."""

    def __init__(self, key: str):
        super().__init__(f"privacy-sensitive change to {key} requires confirm flag")
        self.key = key


@dataclass
class AppliedResult:
    """What the caller learns about an apply (or validate) attempt.

    Attributes:
        ok: Whether the apply landed.
        applied_now: Keys that took effect in this apply (hot/warm).
        pending_restart: Keys whose value was persisted but won't take
            effect until the process restarts (``restart_required``
            mutability class).
        warnings: Subscriber-isolation warnings, e.g. ``"rebuild_bucket: ..."``.
        revision_hash_before / revision_hash_after: Short hashes of the
            snapshot before and after the apply.
        revision_no: Monotonic counter; bumps on every successful apply.
    """

    ok: bool
    applied_now: list[str] = field(default_factory=list)
    pending_restart: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    revision_hash_before: str = ""
    revision_hash_after: str = ""
    revision_no: int = 0


# ---------------------------------------------------------------------------
# Hashing helper
# ---------------------------------------------------------------------------
def _hash_snapshot(snap: Mapping[str, Any]) -> str:
    """Return a short, stable hash of ``snap`` for ``If-Match`` semantics.

    SHA-256 truncated to 16 hex chars is plenty for collision avoidance
    inside a single process and keeps the wire payload tiny.
    """
    payload = json.dumps(snap, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Subscriber bookkeeping
# ---------------------------------------------------------------------------
@dataclass
class _Subscriber:
    callback: Callable[[Mapping[str, Any], Mapping[str, Any]], None]
    keys: tuple[str, ...] | None  # None == fire on every apply
    name: str


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class SettingsStore:
    """Process-wide settings snapshot with atomic apply + subscribers.

    Readers
    -------
    Hot-path code calls :meth:`snapshot` once per frame and uses the
    returned frozen mapping for the duration. The reader path is lock-free
    and O(1) — it is just an attribute read.

    Writers
    -------
    Writers go through :meth:`apply_diff`. The diff is validated against
    the merged prospective snapshot, the snapshot is atomically swapped,
    and registered subscribers are fanned out **after** the swap so a
    crashing subscriber cannot leave the store half-applied.
    """

    def __init__(self, initial: Mapping[str, Any] | None = None):
        seed = dict(settings_spec.defaults())
        if initial:
            seed.update(initial)
        # Validate the seed eagerly — a misconfigured default is a programming
        # error, not a runtime error, so we want it to surface at boot.
        errors = settings_spec.validate(seed)
        if errors:
            raise SettingsValidationError(errors)
        self._snapshot: Mapping[str, Any] = MappingProxyType(seed)
        self._last_good: Mapping[str, Any] = self._snapshot
        self._revision_no: int = 1
        # ``RLock`` (re-entrant) so a subscriber can call back into the
        # store (e.g. read ``snapshot()``) without deadlocking.
        self._lock = threading.RLock()
        self._subscribers: list[_Subscriber] = []
        # Counters surfaced by the observability endpoints.
        self.counters: dict[str, int] = {
            "settings_apply_total_success": 0,
            "settings_apply_total_validation_error": 0,
            "settings_apply_total_subscriber_error": 0,
            "settings_apply_total_conflict": 0,
            "settings_rollback_total": 0,
        }

    # ------------------------------------------------------------------
    # Snapshot reads
    # ------------------------------------------------------------------
    def snapshot(self) -> Mapping[str, Any]:
        """Return the current frozen snapshot (cheap, lock-free)."""
        return self._snapshot

    def revision_hash(self) -> str:
        """Short stable hash of the current snapshot for ``If-Match`` flows."""
        return _hash_snapshot(self._snapshot)

    def revision_no(self) -> int:
        """Monotonic apply counter (informational; not for concurrency)."""
        return self._revision_no

    # ------------------------------------------------------------------
    # Subscribers
    # ------------------------------------------------------------------
    def register_subscriber(
        self,
        callback: Callable[[Mapping[str, Any], Mapping[str, Any]], None],
        *,
        name: str | None = None,
    ) -> None:
        """Register a callback that fires after **every** successful apply.

        ``callback(before, after)`` runs on the apply thread inside the
        store lock. It MUST NOT raise; if it does, the exception is logged
        as a warning and surfaced in :class:`AppliedResult.warnings` —
        the snapshot is not rolled back.
        """
        self._subscribers.append(
            _Subscriber(callback=callback, keys=None, name=name or callback.__name__)
        )

    def register_subscriber_for(
        self,
        keys: Iterable[str],
        callback: Callable[[Mapping[str, Any], Mapping[str, Any]], None],
        *,
        name: str | None = None,
    ) -> None:
        """Register a callback that fires only when one of ``keys`` changes."""
        self._subscribers.append(
            _Subscriber(
                callback=callback,
                keys=tuple(keys),
                name=name or callback.__name__,
            )
        )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def apply_diff(
        self,
        diff: Mapping[str, Any],
        *,
        actor: str = "system",
        expected_revision_hash: str | None = None,
        confirm_privacy_change: bool = False,
    ) -> AppliedResult:
        """Atomically merge ``diff`` into the snapshot.

        Args:
            diff: ``{key: new_value}`` for each key the caller wants to
                change. Unknown keys are ignored (with a warning) so that
                stale UI clients don't 422 on retired knobs.
            actor: Free-text label for audit logs. Under the single shared
                admin token we cannot establish true identity — see plan
                §S5 for the operator-label convention.
            expected_revision_hash: Pass the ``revision_hash`` the operator
                saw when they opened the page. If the live snapshot has
                advanced, raises :class:`RevisionConflict`.
            confirm_privacy_change: Must be True when ``diff`` touches a
                key with ``requires_privacy_confirm=True`` (e.g.
                ``ALPR_MODE``). Otherwise raises :class:`PrivacyConfirmRequired`.

        Raises:
            RevisionConflict: Lost-update protection tripped.
            PrivacyConfirmRequired: Privacy-sensitive change without consent.
            SettingsValidationError: Per-key or cross-field validation failed.

        Returns:
            :class:`AppliedResult` with the applied + pending-restart key
            lists and any subscriber warnings.
        """
        with self._lock:
            before = self._snapshot
            before_hash = _hash_snapshot(before)

            if expected_revision_hash and expected_revision_hash != before_hash:
                self.counters["settings_apply_total_conflict"] += 1
                raise RevisionConflict(expected_revision_hash, before_hash)

            # Coerce types and drop unknown keys.
            warnings: list[str] = []
            cleaned: dict[str, Any] = {}
            for key, raw in diff.items():
                if settings_spec.spec_for(key) is None:
                    warnings.append(f"unknown key dropped: {key}")
                    continue
                spec = settings_spec.spec_for(key)
                if spec.mutability == "read_only":
                    warnings.append(f"read-only key ignored: {key}")
                    continue
                if spec.requires_privacy_confirm and not confirm_privacy_change:
                    raise PrivacyConfirmRequired(key)
                try:
                    cleaned[key] = settings_spec.coerce(key, raw)
                except (TypeError, ValueError) as exc:
                    self.counters["settings_apply_total_validation_error"] += 1
                    raise SettingsValidationError(
                        [{"key": key, "reason": f"coercion failed: {exc}"}]
                    )

            if not cleaned:
                # Nothing to do — return a no-op result. We still bump nothing
                # so callers can detect "applied but no change".
                return AppliedResult(
                    ok=True,
                    applied_now=[],
                    pending_restart=[],
                    warnings=warnings,
                    revision_hash_before=before_hash,
                    revision_hash_after=before_hash,
                    revision_no=self._revision_no,
                )

            # Build the prospective merged snapshot.
            merged = dict(before)
            merged.update(cleaned)

            errors = settings_spec.validate(merged)
            if errors:
                self.counters["settings_apply_total_validation_error"] += 1
                raise SettingsValidationError(errors)

            # Atomic snapshot swap. Until this rebind, readers see the old
            # snapshot; after it, readers see the new one. Subscribers run
            # AFTER the swap so a subscriber crash cannot revert state.
            self._last_good = before
            self._snapshot = MappingProxyType(merged)
            self._revision_no += 1
            after_hash = _hash_snapshot(self._snapshot)

            buckets = settings_spec.changed_mutability(cleaned)
            applied_now = sorted(buckets.get("hot_apply", []) + buckets.get("warm_reload", []))
            pending_restart = sorted(buckets.get("restart_required", []))

            warnings.extend(self._fan_out(before, self._snapshot, list(cleaned.keys())))

            self.counters["settings_apply_total_success"] += 1
            return AppliedResult(
                ok=True,
                applied_now=applied_now,
                pending_restart=pending_restart,
                warnings=warnings,
                revision_hash_before=before_hash,
                revision_hash_after=after_hash,
                revision_no=self._revision_no,
            )

    def rollback_to_last_good(self, *, actor: str = "system") -> AppliedResult:
        """Restore the snapshot that was active immediately before the last apply.

        A no-op (returns ``ok=True`` with empty lists) when nothing has been
        applied since boot — the API layer translates that into a 409 if
        the operator clicked rollback with no eligible target.
        """
        with self._lock:
            before = self._snapshot
            before_hash = _hash_snapshot(before)
            if self._last_good is before:
                return AppliedResult(
                    ok=True,
                    applied_now=[],
                    pending_restart=[],
                    warnings=["no rollback target — store at last-known-good already"],
                    revision_hash_before=before_hash,
                    revision_hash_after=before_hash,
                    revision_no=self._revision_no,
                )
            target = self._last_good
            # Stash the current snapshot as the new last_good so an operator
            # who hits rollback twice can return to where they started.
            self._last_good = before
            self._snapshot = target
            self._revision_no += 1
            after_hash = _hash_snapshot(self._snapshot)
            changed_keys = [k for k in target if target[k] != before.get(k)]
            warnings = self._fan_out(before, self._snapshot, changed_keys)
            self.counters["settings_rollback_total"] += 1
            buckets = settings_spec.changed_mutability({k: target[k] for k in changed_keys})
            return AppliedResult(
                ok=True,
                applied_now=sorted(buckets.get("hot_apply", []) + buckets.get("warm_reload", [])),
                pending_restart=sorted(buckets.get("restart_required", [])),
                warnings=warnings,
                revision_hash_before=before_hash,
                revision_hash_after=after_hash,
                revision_no=self._revision_no,
            )

    # ------------------------------------------------------------------
    # Subscriber fan-out (private)
    # ------------------------------------------------------------------
    def _fan_out(
        self,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        changed_keys: list[str],
    ) -> list[str]:
        """Run subscribers, isolating exceptions into warnings."""
        warnings: list[str] = []
        changed_set = set(changed_keys)
        for sub in self._subscribers:
            if sub.keys is not None and not changed_set.intersection(sub.keys):
                continue
            try:
                sub.callback(before, after)
            except Exception as exc:  # noqa: BLE001 — subscribers must never crash apply.
                self.counters["settings_apply_total_subscriber_error"] += 1
                warnings.append(f"{sub.name}: {exc}")
        return warnings


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------
STORE = SettingsStore()
"""Process-wide :class:`SettingsStore` singleton. Import as
``from road_safety.settings_store import STORE`` and call
``STORE.snapshot()`` in any hot path."""
