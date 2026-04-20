# Settings Console Plan v1.1 (Critical Revision)

This revision replaces the earlier draft with a tighter, risk-first plan for
shipping a Settings Console without weakening the current safety pipeline.

## Summary

- Build the Settings Console in strict order: `S0 hardening`, `S1 runtime
  settings core`, `S2 baseline and impact engine`, `S3 templates and UI`,
  `S4 benchmark lane and soak`.
- Keep AI advisory-only. Deterministic metrics and explicit comparability
  checks remain the source of truth for apply, rollback, and operator trust.
- Do not treat all "impact" metrics as equally real-time. Some signals can be
  evaluated immediately; others, especially drift precision, are lagging and
  must be labeled as such.

## Critical Review Findings

### 1. The earlier draft was still too broad for a single safe rollout

The original direction was right, but the slices were still too large. Runtime
settings, baseline capture, template history, benchmark execution, and AI
summary all carry different failure modes and should not land as one feature
drop.

### 2. Runtime mutability was underspecified

Many of the knobs the UI wants to expose are not currently live settings.
They are module-level literals or import-time env reads in:

- [server.py](../../road_safety/server.py#L1310)
- [config.py](../../road_safety/config.py#L90)
- [detection.py](../../road_safety/core/detection.py#L101)
- [quality.py](../../road_safety/core/quality.py#L86)
- [context.py](../../road_safety/core/context.py#L336)
- [drift.py](../../road_safety/services/drift.py#L395)

That means the Settings Console cannot just be a UI over existing config. It
needs a proper runtime settings layer with explicit mutability classes.

### 3. Baseline comparison needed stronger guardrails

Comparing "before" and "after" without scene, quality, and warmup controls can
produce false conclusions. A setting can look "better" simply because traffic
changed, the scene switched from urban to parking, or the camera degraded.

### 4. Immediate and lagging metrics were mixed together

The earlier draft treated drift and false-positive impact as if they were
available instantly. That is not true in this system. Drift precision depends
on operator feedback and a rolling label window in
[drift.py](../../road_safety/services/drift.py#L395), so it must be treated as
lagging evidence, not immediate impact.

### 5. Control-plane durability was not explicit enough

The previous version correctly mentioned SSE, but SSE cannot be the canonical
state. The current event fan-out drops under queue pressure and provides no
durable acknowledgement path:

- [server.py](../../road_safety/server.py#L1319)
- [server.py](../../road_safety/server.py#L1761)

Settings apply state, baseline state, and benchmark state must be stored
durably first, then broadcast as best-effort notifications.

### 6. Benchmark isolation was missing

The current offline harness still relies on shared artifacts such as
`data/events.json`:

- [tools/eval_detect.py](../../tools/eval_detect.py#L367)
- [tools/README.md](../../tools/README.md#L22)

The Settings Console cannot run benchmark jobs against shared live output
paths. It needs isolated per-run output directories and a queued worker model.

### 7. Security prerequisites were not framed as blockers

The plan already called out hardening, but this needs to be explicit:
mutation endpoints marked public today must be fixed before the Settings
Console ships:

- [server.py](../../road_safety/server.py#L2276)
- [server.py](../../road_safety/server.py#L2292)

## Revised Implementation Approach

## S0 Prerequisite hardening

This is a blocking phase. No Settings Console write path should ship before it.

- Protect watchdog mutation endpoints with the existing admin bearer helper.
- Resolve `recent_events` concurrency so reads, writes, and SSE replay operate
  on safe snapshots.
- Document the auth split:
  - public read telemetry may remain public where already intended
  - all settings reads under `/api/settings/*` should be admin-only unless
    explicitly approved as operator-safe
  - all settings writes are admin-only
- Publish an explicit control-plane rule: DB state is canonical, SSE is
  notification only.

## S1 Runtime settings core

### Settings domain model

Introduce a dedicated settings service and typed schema with:

- `RuntimeSettings`
- `SettingsSchema`
- `SettingsTemplate`
- `TemplateRevision`
- `ApplyRequest`
- `ApplyResult`

Persist these in `data/settings.db`.

### Mutability classes

Every field must be classified up front:

- `hot_apply`: safe atomic swap during runtime
- `warm_reload`: requires a bounded subsystem reset or warmup window
- `restart_required`: requires process restart or model reload boundary
- `read_only`: informative only, not editable in UI

Recommended first-pass classification:

- `hot_apply`
  - detection thresholds and floors
  - scene adaptive thresholds
  - quality thresholds and multipliers
  - drift alert thresholds and window size
  - downstream alert thresholds that do not alter process startup
- `warm_reload`
  - tracker sub-config if it can be re-instantiated cleanly
  - any settings that require clearing rolling windows or re-seeding runtime state
- `restart_required`
  - `ROAD_MODEL_PATH`
  - `ROAD_TARGET_FPS`
  - source startup defaults that materially affect process wiring
- `read_only`
  - secrets
  - fleet identity
  - DSAR token
  - cloud signing secrets

### Apply contract

Settings apply must be atomic and durable:

1. Validate full payload against schema and mutability rules.
2. Store pending apply intent in SQLite.
3. Build a new immutable settings snapshot.
4. Swap snapshot atomically or fail without changing active state.
5. Record final result in SQLite and audit log.
6. Broadcast state change on SSE.

Never apply partial field updates.

### Rollback contract

- Always retain `last_known_good_revision_id`.
- Rollback is a first-class operation, not "apply old template".
- If apply fails after persistence but before activation, mark the run failed
  and keep the prior revision active.
- If warm reload or restart-required activation exceeds timeout, surface
  `degraded_activation` and offer rollback.

## S2 Baseline and impact engine

### Baseline artifacts

Introduce:

- `BaselineSnapshot`
- `ImpactReport`
- `ComparabilityReport`
- `SettingsEvent`

Each baseline snapshot must include:

- exact settings revision hash
- capture start and end time
- processed frame count
- emitted event count
- scene label distribution
- perception state distribution
- key throughput metrics
- warmup status

### Real-time vs lagging metrics

Split impact reporting into two classes.

Immediate metrics:

- processed FPS and throughput
- detections by class
- interactions by type
- emitted event counts by severity and type
- confidence distributions
- scene mix
- perception state mix

Lagging metrics:

- drift precision
- false-positive rate inferred from operator feedback
- feedback coverage

The UI and API must label lagging metrics clearly. They are not valid for
instant post-apply claims until enough new labeled events accrue.

### Comparability gates

An `ImpactReport` may only claim a valid comparison when:

- warmup is complete for the candidate revision
- minimum processed-frame volume is met
- scene distribution similarity passes
- perception-state similarity passes
- enough event volume exists for event-rate claims

If those gates fail, the API must return an "insufficient evidence" state with
machine-readable reasons instead of weak or misleading deltas.

### Confidence model

Every comparison should return:

- `confidence_tier`: `high`, `medium`, `low`, `insufficient`
- explicit reasons for downgrades
- which metrics remain immediate-only vs lagging

## S3 Templates and Settings UI

### Template lifecycle

Templates need stronger governance than the original draft described:

- create and edit operations produce immutable revisions
- delete is soft delete only
- historical revisions remain queryable
- active revisions cannot disappear from audit history
- benchmark and baseline records attach to revision ids, not just template ids

### UI scope

Add `/settings` as a new top-level route and nav entry, with:

- live video stream
- current effective settings summary
- grouped editable controls by subsystem
- validation panel
- apply and rollback controls
- baseline capture status
- impact summary cards
- template list with add/edit/delete/apply
- audit metadata
- benchmark status panel
- advisory AI explanation panel

### UX rules

- disable apply while validation fails
- show restart-required and warm-reload badges
- show comparability confidence prominently
- never present AI output without the underlying deterministic metrics beside it

## S4 Benchmark lane and soak

### Benchmark jobs

Introduce `BenchmarkRun` and a queued benchmark worker.

Requirements:

- one isolated output directory per run, for example
  `data/settings_benchmarks/<run_id>/`
- no reuse of shared live artifacts like `data/events.json`
- timeout, cancel, and failure reporting
- compare results attached to template revision history

### Tooling implication

The existing evaluation tooling likely needs an output-dir concept or wrapper
around `analyze.py` and `eval_detect.py` so benchmark jobs cannot clobber live
artifacts.

## Public API Surface

### Admin write endpoints

- `POST /api/settings/validate`
- `POST /api/settings/apply`
- `POST /api/settings/rollback`
- `POST /api/settings/templates`
- `PATCH /api/settings/templates/{id}`
- `DELETE /api/settings/templates/{id}`
- `POST /api/settings/baseline/capture`
- `POST /api/settings/benchmark/run`

### Admin read endpoints

- `GET /api/settings/effective`
- `GET /api/settings/schema`
- `GET /api/settings/templates`
- `GET /api/settings/templates/{id}/revisions`
- `GET /api/settings/impact`
- `GET /api/settings/stream`

### API rules

- all `/api/settings/*` routes are admin-tier
- schema version is mandatory on write payloads
- unknown or deprecated fields return structured validation output
- write responses return both durable state and activation state

## Test Plan

### Unit tests

- schema and range validation
- mutability-class enforcement
- atomic apply snapshot swap
- rollback correctness
- revision immutability
- soft-delete behavior
- confidence and comparability scoring

### Integration tests

- validate -> apply -> impact -> rollback lifecycle
- failed-apply invariants
- auth enforcement
- audit logging
- settings SSE ordering and replay behavior
- benchmark queue execution and isolation

### Pipeline regression tests

- unchanged default behavior for detection
- unchanged default behavior for scene adaptive thresholds
- unchanged default behavior for quality monitor
- unchanged default behavior for drift monitor

### UI acceptance tests

- capture baseline -> apply hot setting -> inspect impact -> rollback
- apply restart-required setting -> warmup -> low-confidence until stable
- template add/edit/delete/apply with revision history visible
- lagging metrics shown as unavailable or low-confidence immediately after apply

## Non-Negotiable Production Rules

- secrets and deployment identity are never editable in the Settings UI
- DB state is canonical; SSE is best-effort notification
- deterministic math always outranks AI explanation
- benchmark jobs never reuse live artifact paths
- rollback is guaranteed and auditable
- comparisons must be allowed to say "we do not know yet"

## Assumptions And Defaults

- This file is a revised v1.1 version of the `codex` draft variant.
- The canonical target filename for the shared plan should still be
  `docs/improvements/settings-console-plan.md`.
- The current repo does not yet contain that canonical filename, so this
  variant is being updated in place for review.
