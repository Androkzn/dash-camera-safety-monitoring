# Settings Console — Final Synthesized Plan v2.0

> Synthesizes the best of the four prior drafts (`codex` v1.1 architecture,
> `claude code` v1.0 implementation detail, `cursor` v1.0 production-readiness,
> and the original draft). This is the canonical plan that drives the
> implementation in this repo.

---

## Goal

Add a top-level **Settings** page next to Admin / Dashboard / Monitoring that lets
operators tune backend perception, alerting and LLM parameters live, save
named templates, capture deterministic baseline-vs-after impact, see an
advisory AI summary, and roll back in one click — without weakening any of
the existing safety gates.

## Non-negotiable production rules

1. Secrets, fleet identity, HMAC keys, DSAR token: **never** editable from the UI.
2. SQLite (`data/settings.db`) is the **canonical** store. SSE is best-effort
   notification only.
3. Deterministic math always outranks AI explanation. The LLM is advisory.
4. Comparisons are allowed to say "we do not know yet" (`insufficient evidence`).
5. Atomic apply: validate the whole payload, swap snapshot, run subscribers
   under per-callback `try/except`. Never partial.
6. Rollback to last-known-good is one click and audit-logged.
7. All `/api/settings/*` routes are **admin-tier** (Bearer required) — `GET`
   reads included. The single exception is the SSE stream, which uses a
   short-lived ticket exchange (Bearer-required to issue, single-use to consume).
8. Existing detection gates remain in place; only their *constants* become
   live snapshot reads.

---

## Phased delivery

### S0 — Prerequisite hardening (blocks Settings write paths)

- Gate the watchdog destructive endpoints (`DELETE /api/watchdog/findings`,
  `POST /api/watchdog/findings/delete`) behind `require_bearer_token`.
- Add a small lock around `state.recent_events` append/read so the impact
  engine can sample without races with the perception loop.
- Document the auth matrix in `CLAUDE.md`: public read (telemetry, MJPEG),
  Bearer admin (everything under `/api/settings/*`, `/api/audit`, `/api/llm/*`,
  watchdog mutations), DSAR (unredacted thumbnails).

### S1 — Runtime settings core

- New `road_safety/settings_spec.py` with `SettingSpec` dataclass and a
  `SETTINGS_SPEC` registry covering ~16 tunables (see "Spec inventory" below).
- Each spec carries: `key`, `default`, `min`, `max`, `enum`, `type`,
  `category`, `description`, `mutability`, optional `validator`, plus
  documentation of the source module.
- New `road_safety/settings_store.py` — `SettingsStore` singleton with:
  - Frozen-dict snapshot exposed via `MappingProxyType` and read with
    `STORE.snapshot()` (cheap, lock-free for readers).
  - `apply_diff(diff, *, actor, expected_revision_hash=None)` — atomic
    snapshot rebuild under a short `RLock`; runs subscribers after swap;
    isolates each subscriber in `try/except`; surfaces failures in
    `AppliedResult.warnings`. Honors `If-Match` via `expected_revision_hash`
    (returns `RevisionConflict` on mismatch).
  - `register_subscriber(callback)` and
    `register_subscriber_for(keys, callback)` — fine-grained variant so
    e.g. the LLM bucket only rebuilds when bucket params change.
  - `revision_hash()` — short stable hash of the current snapshot for the
    `If-Match` flow.
  - `rollback_to_last_good()` — restores the immediately-prior snapshot.

### Mutability classes (four buckets)

- **`hot_apply`**: pure snapshot rebind, picked up next frame.
  Examples: detection floors, TTC/distance thresholds, quality thresholds,
  Slack thresholds, `MAX_RECENT_EVENTS`, `PAIR_COOLDOWN_SEC`, `ALPR_MODE`.
- **`warm_reload`**: hot but a subscriber rebuilds a subsystem on the apply
  thread.
  Examples: `TRACK_HISTORY_LEN` (rebuild per-track deques),
  `LLM_BUCKET_CAPACITY` / `LLM_BUCKET_REFILL_PER_MIN` (rebuild
  `_HAIKU_BUCKET`).
- **`restart_required`**: value persists, takes effect on next boot.
  Examples: `TARGET_FPS`, `MODEL_PATH`. Apply response splits result
  into `applied_now` and `pending_restart`; UI shows a persistent banner.
- **`read_only`**: surfaced in UI for visibility, never editable.
  Examples: tokens, salts, fleet identity.

### Cross-field validators (server-side, mandatory)

- `TTC_MED_SEC > TTC_HIGH_SEC`
- `DIST_MED_M > DIST_HIGH_M`
- `MIN_SCALE_GROWTH > 1.0`
- `LLM_BUCKET_CAPACITY >= 1`
- `SLACK_HIGH_MIN_CONFIDENCE >= VEHICLE_PAIR_CONF_FLOOR`
- `LLM_BUCKET_REFILL_PER_MIN` is a UI-facing per-minute unit; the
  subscriber divides by 60 before rebuilding the bucket.

Validation errors return **422** with a structured `[{key, reason}]` body so
the UI can highlight individual rows.

### S2 — Persistence (SQLite)

`data/settings.db` with a tiny schema:

| Table | Purpose |
| --- | --- |
| `migrations(version, applied_at)` | applied schema versions |
| `templates(id, name, description, system, soft_deleted_at, created_at, updated_at)` | template index |
| `template_revisions(id, template_id, revision_no, payload_json, payload_hash, created_at, created_by_label)` | immutable history |
| `apply_log(id, ts, actor_label, revision_hash_before, revision_hash_after, result, warnings_json, audit_id)` | every apply |
| `baselines(id, audit_id, settings_hash, captured_start, captured_end, sample_count, payload_json)` | baseline snapshots |
| `impact_sessions(id, audit_id, change_ts, before_json, after_json, baseline_id, last_payload_json, state, archived_at)` | active+archived sessions |

Single-writer file model under the edge process (out of scope: HA / multi-instance).

### S3 — Templates

- Atomic CRUD against SQLite. Soft delete only (90 days retention).
- Apply is **re-validation + migration**:
  1. drop unknown keys (audit `settings.template.key_dropped`),
  2. fill missing keys with current spec defaults
     (audit `settings.template.key_filled`),
  3. re-validate against current schema + cross-field rules,
  4. `STORE.apply_diff()` only on success.
- Synthetic `tpl_default` (system=true) returned at the head of `list_templates()`
  with values from `SETTINGS_SPEC` defaults (not the live snapshot).
- Edits create a new immutable `template_revisions` row; baseline / impact
  records attach to revision ids, not just template ids.

### S4 — Impact engine

`road_safety/services/impact.py` with `WindowStats`, `ImpactSession`,
`ImpactMonitor`. Persisted to SQLite on every tick so a server restart inside
the monitoring window does not lose state.

`WindowStats` captures:
- `window_start_ts`, `window_end_ts`, `duration_sec`, `sample_size`,
  `confidence_tier` ∈ {high, medium, low, insufficient}.
- Event-rate by severity, severity ratios.
- Distance / TTC / confidence percentiles where available.
- LLM cost and latency (via `llm_observer`).
- **Scene distribution** and **quality-state distribution** (for comparability).

#### Real-time vs lagging metrics (must be labeled in API + UI)

- **Immediate**: processed FPS, detections, interactions, emitted-event counts,
  confidence distributions, scene mix, perception state mix.
- **Lagging**: drift precision, false-positive rate from operator feedback,
  feedback coverage. UI shows these as "awaiting feedback" until
  `>= MIN_FEEDBACK` verdicts accrue.

#### Comparability gates (concrete algorithm)

- **Sample size**: each window `>= MIN_BASELINE_EVENTS=20` and
  `>= MIN_AFTER_EVENTS=20`. Below → `confidence="low"`, reason
  `insufficient_events`.
- **Scene mix**: Jensen–Shannon divergence between baseline and after
  scene-label histograms; `JSD > 0.2` caps confidence at `medium`,
  reason `scene_mix_drift`.
- **Quality state**: same-bucket fraction of `QualityMonitor.state()`
  between windows; `< 0.6` caps `low`, reason `quality_drift`.
- **Window length**: each `>= 300 s` clock; expands to 600/1200/1800
  if sample-size threshold not met.

Coalescing: changes within `COALESCE_WINDOW_SEC=30` of the prior change
fold into the same session; original `before` is preserved so revert always
goes back to the **first** change.

`_last_good` lifecycle and "no recent change to revert" disabled state are
spelled out in the code comments — see `services/impact.py`.

### S5 — API surface

All under `/api/settings/*`, all `require_bearer_token`, all mutations
audit-logged through `road_safety/compliance/audit.py`.

Reads:
- `GET  /api/settings/effective` → current values + raw + scene-effective
  + `revision_hash`.
- `GET  /api/settings/schema` → `SETTINGS_SPEC` for UI rendering.
- `GET  /api/settings/templates` → list incl. synthetic default.
- `GET  /api/settings/templates/{id}/revisions` → revision history.
- `GET  /api/settings/baseline?audit_id=…` → baseline payload.
- `GET  /api/settings/impact?audit_id=…` → current impact snapshot
  (deterministic; never mutates server state).
- `GET  /api/settings/impact/history?limit=20` → archived sessions.

Writes:
- `POST /api/settings/validate` → dry-run validation, returns resolved diff
  + `would_warm_reload: [...]` + `would_restart: [...]`.
- `POST /api/settings/apply` → atomic apply.
  - Accepts `If-Match: <revision_hash>` for lost-update protection.
  - Server-side `MIN_CHANGE_INTERVAL_SEC=5` per-token cooldown → 429 + Retry-After.
  - Body must include `?confirm_privacy_change=1` (or body field) when
    `ALPR_MODE` is being changed (off↔on / off↔on_demand / on↔on_demand).
  - Response: `{applied_now, pending_restart, warnings, audit_id, revision_hash}`.
- `POST /api/settings/rollback` → restore last-known-good; audit-logged.
- `POST /api/settings/templates` → create.
- `PATCH /api/settings/templates/{id}` (`If-Match`) → new revision.
- `DELETE /api/settings/templates/{id}` → soft delete (409 if `system`).
- `POST /api/settings/templates/{id}/apply` → re-validate, migrate, apply.
- `POST /api/settings/baseline/capture` → freeze a new baseline window.
- `POST /api/settings/stream_ticket` → returns `{ticket, expires_in}`,
  single-use, 30 s TTL. Requires Bearer.
- `GET  /api/settings/impact/stream?ticket=…` → SSE; mirrors the existing
  `/stream/events` handler pattern.

### S6 — Frontend

- New top-level route `/settings` and `<Link to="/settings">` in
  `TopBar.tsx`.
- Page layout (3-column desktop, collapses on narrow):
  - Left: live MJPEG video reusing `VideoFeed` + scene/quality context badge.
  - Center: grouped tunable controls (`<details>`/`<summary>` per category),
    each row showing **base → effective** when scene multipliers apply.
    Validation errors highlight individual rows.
  - Right: template list with add/edit/delete/apply, baseline panel, impact
    panel with bar comparison (lightweight inline SVG to avoid pulling
    `recharts` for v1), advisory AI text below numbers, revert button.
- Hooks:
  - `useSettings` — polled effective + schema; debounced apply (400 ms).
  - `useImpact` — polls `/impact?audit_id=…` every 15 s; falls back from
    SSE → polling on disconnect.
  - `useAdminToken` — `sessionStorage`-backed; pasted via a small dialog on
    first 401 / 503; "Forget token" link in the page header.
- Auth:
  - `frontend/src/lib/adminApi.ts` — `withAdminAuth(init)` injects
    `Authorization: Bearer …`; throws `MissingAdminTokenError`. Comment block
    documents the localStorage-vs-sessionStorage trade-off and CSRF posture
    (Bearer header is not auto-attached, so CSRF is structurally mitigated).
  - SSE never carries the long-lived bearer; uses the ticket exchange.
- UX rules:
  - Disable apply while validation fails.
  - Show `restart-required` and `warm-reload` badges on each control.
  - Show comparability `confidence_tier` and reasons prominently.
  - Lagging metrics show "awaiting feedback" rather than weak deltas.
  - Never present AI output without the underlying numbers beside it.

### S7 — Observability

Counters / gauges emitted (so an alert can be wired even without Prometheus
in v1, by tailing audit + structured logs):
- `settings_apply_total{result=success|validation_error|subscriber_error|conflict}`
- `settings_revision` (current revision number, monotonic)
- `settings_rollback_total`
- `impact_comparability_blocked_total{reason}`
- apply-latency histogram (snapshot swap + subscriber fan-out)

### S8 — Tests

- **Unit**: spec validation, cross-field rules, atomic apply, subscriber
  isolation (raise → warning surfaced, store still applies),
  `TRACK_HISTORY_LEN` deque rebuild preserves tail.
- **API**: auth tier enforcement (401/403/503), validation 422 shape,
  `If-Match` 409, `MIN_CHANGE_INTERVAL_SEC` 429, rollback, ticket issue +
  single-use consumption + expiry.
- **Templates**: CRUD, default-template immutability, atomic write, apply
  with key dropped (logged), apply with key filled (logged), apply violating
  new cross-field validator (422).
- **Impact**: baseline lookback expansion, coalescing preserves `_last_good`
  across rapid changes, comparability gates produce correct `confidence_tier`
  + reasons, JSD scene-drift detection, FP-rate proxy fallback, archived
  session resume after restart.
- **Privacy invariant regression** (new): a synthetic event through
  `enrich_event()` never lands `plate_text` / `plate_state` in any buffer
  or JSONL write — replaces the fictitious `tests/test_core.py::test_no_plate_leak`
  the original draft assumed.
- **Pipeline regression**: detection / quality / Slack default behaviour
  unchanged when settings = defaults.

### S9 — Out of scope (v2 candidates)

- `TARGET_FPS` mid-run timer rebuild (kept as `restart_required` in v1).
- Per-role admin tokens (currently single shared `ROAD_ADMIN_TOKEN`).
- Cookie-based auth instead of `sessionStorage` bearer.
- Cloud-receiver mirror of impact history for fleet-wide settings analytics.
- Auto-revert on AI recommendation (v1 is operator-driven only).
- A 6th "Impact Deep Dive" agent (would push us past the 5-tool agent cap).
- Benchmark lane / soak (S4 of the codex draft) — defer until baseline
  + impact engine is proven in production.

---

## Spec inventory (v1 — 16 tunables)

This is the curated v1 set; the registry is designed to grow. All values
documented here flow through `settings_spec.SETTINGS_SPEC`.

| Key | Default | Range | Mutability | Category | Source |
| --- | --- | --- | --- | --- | --- |
| `CONF_THRESHOLD` | 0.50 | 0.10 – 0.95 | hot_apply | detection | `core/detection.py` |
| `PERSON_CONF_THRESHOLD` | 0.25 | 0.10 – 0.95 | hot_apply | detection | `core/detection.py` |
| `VEHICLE_PAIR_CONF_FLOOR` | 0.60 | 0.30 – 0.95 | hot_apply | detection | `core/detection.py` |
| `MIN_BBOX_AREA` | 1200 | 400 – 4000 | hot_apply | detection | `core/detection.py` |
| `TTC_HIGH_SEC` | 0.5 | 0.05 – 3.0 | hot_apply | risk-tier | `core/detection.py` |
| `TTC_MED_SEC` | 1.0 | 0.10 – 6.0 | hot_apply | risk-tier | `core/detection.py` |
| `DIST_HIGH_M` | 2.0 | 0.5 – 20 | hot_apply | risk-tier | `core/detection.py` |
| `DIST_MED_M` | 5.0 | 1 – 50 | hot_apply | risk-tier | `core/detection.py` |
| `MIN_SCALE_GROWTH` | 1.10 | 1.01 – 2.0 | hot_apply | gating | `core/detection.py` |
| `TRACK_HISTORY_LEN` | 12 | 4 – 60 | warm_reload | gating | `core/detection.py` |
| `QUALITY_BLUR_SHARP` | 40.0 | 10 – 200 | hot_apply | quality | `core/quality.py` |
| `QUALITY_LOW_LIGHT_LUM` | 45.0 | 10 – 120 | hot_apply | quality | `core/quality.py` |
| `LLM_BUCKET_CAPACITY` | 3.0 | 1 – 20 | warm_reload | llm-cost | `services/llm.py` |
| `LLM_BUCKET_REFILL_PER_MIN` | 3.0 | 0.5 – 60 | warm_reload | llm-cost | `services/llm.py` |
| `SLACK_HIGH_MIN_CONFIDENCE` | 0.55 | 0.30 – 0.95 | hot_apply | alerting | `integrations/slack.py` |
| `ALPR_MODE` | "off" | enum {off, on, on_demand} | hot_apply (privacy gate) | privacy | `config.py` |
| `PAIR_COOLDOWN_SEC` | 8.0 | 1 – 60 | hot_apply | dedup | `config.py` |
| `MAX_RECENT_EVENTS` | 500 | 50 – 5000 | hot_apply | performance | `config.py` |
| `TARGET_FPS` | 2.0 | 0.5 – 10 | restart_required | performance | `config.py` |

## Files: create vs modify (high level)

### New backend
- `road_safety/settings_spec.py`
- `road_safety/settings_store.py`
- `road_safety/services/settings_db.py`
- `road_safety/services/templates.py`
- `road_safety/services/impact.py`
- `road_safety/api/settings.py`

### Modified backend
- `road_safety/config.py` — no schema change; settings_spec imports defaults from here.
- `road_safety/server.py` — mount router; add `recent_events` lock; gate watchdog mutators; lifespan wires impact monitor; per-loop snapshot reads for `MAX_RECENT_EVENTS` / `PAIR_COOLDOWN_SEC` / `ALPR_MODE`.
- `road_safety/core/detection.py` — snapshot reads for the detection / risk-tier / gating constants; `TRACK_HISTORY_LEN` deque rebuild on subscriber.
- `road_safety/core/quality.py` — snapshot reads for the two threshold constants.
- `road_safety/services/llm.py` — `_HAIKU_BUCKET` rebuilt via subscriber; `_CB_THRESHOLD` / `_CB_COOLDOWN_SEC` snapshot reads; new `analyze_settings_impact()` advisory function.
- `road_safety/integrations/slack.py` — snapshot reads for the Slack thresholds.

### New frontend
- `frontend/src/pages/SettingsPage.tsx` (+ `.module.css`)
- `frontend/src/components/settings/` — `TunableControl`, `TemplateManager`, `BaselinePanel`, `ImpactPanel`, `ImpactBars`, `ContextBadge`, `TokenPromptDialog`.
- `frontend/src/hooks/useSettings.ts`, `useImpact.ts`, `useAdminToken.ts`.
- `frontend/src/lib/adminApi.ts`.

### Modified frontend
- `frontend/src/App.tsx` — register `/settings`.
- `frontend/src/components/layout/TopBar.tsx` — add Settings link, admin badge.
- `frontend/src/types.ts` — add settings + template + impact types.

### Tests
- `tests/test_settings_store.py`
- `tests/test_settings_api.py`
- `tests/test_settings_templates.py`
- `tests/test_settings_impact.py`
- `tests/test_privacy_invariant.py` (new — replaces the fictitious test referenced by the original draft)

---

## Definition of Done

- Operators can apply, rollback, edit/save templates, and inspect impact from
  the Settings tab without touching `.env` or restarting the process for any
  `hot_apply` / `warm_reload` knob.
- Every apply is dry-run-validatable, auditable, atomic, isolated against
  subscriber failures, conflict-protected via `If-Match`, and rate-limited.
- Baseline-vs-after impact has a concrete comparability algorithm and clearly
  separates immediate from lagging metrics.
- Existing detection gates remain intact; only their constants change.
- Pytest suite for the new modules passes; existing core tests remain green.
- Frontend builds (`tsc -b --noEmit` + `vite build`) without new errors.
- The privacy invariant test exists and protects against plate-text leak.
