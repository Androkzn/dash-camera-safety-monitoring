# Settings Page — Live Backend Tuning + AI Impact Analysis + Templates

> **v1.1 — critical review applied** (2026-04-18). Revisions called out inline with `▲ v1.1:` markers.

## Revisions in v1.1 (summary)

Findings against the actual codebase:

1. **Factual fixes.** (a) The existing SSE handler is `/stream/events`, not `/api/live/stream`. (b) `_HAIKU_BUCKET` is constructed with `refill_per_sec`, not per-minute — plan's `LLM_BUCKET_REFILL_PER_MIN` needs explicit unit conversion on apply. (c) `tests/test_core.py::test_no_plate_leak` does **not** exist; verification step 7 must create it, not assume it.
2. **Logical contradiction.** `TARGET_FPS` was flagged `requires_restart=true` and simultaneously described as "read from snapshot per loop iteration". Resolved: it is read **once at loop start**; mutations queue for next restart.
3. **Scene-context coupling (biggest architectural gap).** `SceneContextClassifier.adaptive_thresholds()` multiplies `TTC_*` and `DIST_*` at runtime based on urban/highway/parking. Without capturing scene in baseline/after, A/B impact comparisons are confounded by scene drift. Added `scene_distribution` to `WindowStats` and an "effective value" display next to each operator tunable.
4. **Session lifecycle gaps.** `_last_good` behavior across coalesce, archive, and server restart was undefined. Specified below.
5. **SSE auth smell.** Admin token in query string leaks to access logs, browser history, and `Referer`. Replaced with short-lived ticket exchange.
6. **Subscriber robustness.** Store never guarded against subscriber exceptions — one bad callback would tear down the apply thread. Added try/except isolation + warning propagation back to the caller.
7. **Template schema drift.** Applying an old template after a spec change could violate new cross-field validators or reference dropped keys. Added re-validation + spec migration at apply time.
8. **Privacy audit symmetry.** `settings.privacy_change` now fires for `on→off` *and* `off→on` (both change posture).
9. **Deque rebuild subtlety.** Changing `TRACK_HISTORY_LEN` mid-run requires per-track `maxlen` migration, not a simple constant swap. Rebuild semantics specified.
10. **Impact history persistence.** `GET /api/settings/impact/history` would return empty after restart with the v1.0 in-memory-only design. Added `data/settings_history.jsonl` append-on-archive.

Each applied inline.

---

## Context

Today every tunable in the road-safety pipeline (≈50 env vars + module constants for detection thresholds, perception quality, LLM rate limits, alerting tiers, retention, etc.) is **frozen at boot**. To change any of them an operator must edit `.env`, restart the server, and lose ~5–10 s of perception data. There is no way to A/B test thresholds, no audit trail of who changed what, and no live signal showing whether a change actually helped.

The user wants a new **Settings** tab next to Monitoring that:

1. Displays the live video stream + real-time perception context (scene, quality, fps).
2. Exposes a curated set of backend tunables as live-mutable controls grouped by category.
3. Saves named **Templates** of full settings sets (CRUD + quick-apply).
4. Captures a **baseline** of detection behavior at the moment of change, then a rolling **after-change window**, and asks an LLM to **narrate the impact** ("event rate down 38%, FP rate steady, recommend KEEP").
5. Visualizes baseline vs. after with comparison charts (recharts).
6. Surfaces a **one-click revert** when the AI flags a regression.

User explicitly chose: full-scope v1, recharts, manual revert (no auto-revert).

---

## Architecture overview

```
                ┌────────────────────────────────────────────────┐
                │  Frontend  (React 19 / Vite / CSS Modules)     │
                │  /settings page  —  3-column layout            │
                │  ┌──────────┬───────────────┬────────────────┐ │
                │  │ Video +  │  Settings      │ Templates +    │ │
                │  │ context  │  controls      │ Baseline +     │ │
                │  │          │                │ Impact (charts │ │
                │  │          │                │  + AI text)    │ │
                │  └──────────┴───────────────┴────────────────┘ │
                └──────┬──────────────────────────────────┬──────┘
                       │  HTTPS + Bearer (admin)          │  SSE (Bearer via ?token=)
                       ▼                                  ▼
        ┌────────────────────────┐        ┌────────────────────────────┐
        │ /api/settings (router) │        │ /api/settings/impact/stream│
        │  GET, PUT, reset       │        │  per-15s push: deltas +    │
        │  /templates CRUD       │        │  AI narrative (cached 30s) │
        │  /baseline             │        └────────────────────────────┘
        └─────────┬──────────────┘                       ▲
                  │                                      │
                  ▼                                      │
   ┌──────────────────────────────┐    ┌─────────────────┴─────────────┐
   │ road_safety/settings_store.py│    │ road_safety/services/impact.py│
   │  Snapshot + RLock + subscribers │  WindowStats, ImpactSession,  │
   │  All-or-nothing apply        │    │  ImpactMonitor.run_loop (15s) │
   └──────┬───────────────────────┘    │  computes deltas, calls LLM   │
          │ subscriber callbacks       └────────────┬──────────────────┘
          ▼                                         │
   detection.py / quality.py / llm.py / slack.py    ▼
   (read STORE.snapshot()[key] in hot path)    services/llm.py
                                               analyze_settings_impact()
                                               (1 token, _complete router)
```

Key invariants preserved:

- `road_safety/config.py` remains the single source of truth — but it now also exports a `SETTINGS_SPEC` registry (defaults + min/max + categories) that drives the store.
- LLM calls go through `_complete()` in `road_safety/services/llm.py` — preserves provider failover, shared `_HAIKU_BUCKET`, circuit breaker, `llm_observer` cost tracking.
- All hot-path gates remain unchanged in *behavior* — only their *constants* become snapshot reads.
- Privacy invariant intact: `enrich_event()` still scrubs plate at ingest.
- All write endpoints require `Authorization: Bearer <ROAD_ADMIN_TOKEN>` via existing `road_safety/security.py::require_bearer_token`.

---

## Phase 1 — Backend: live-mutable settings store

### 1.1 Create [road_safety/settings_store.py](road_safety/settings_store.py)

New module with:

- `@dataclass class SettingSpec` — `key, default, min, max, enum, type ("float"|"int"|"bool"|"enum"), category, description, requires_restart, validator`.
- `class SettingsStore`:
  - `_snapshot: Mapping[str, Any]` — read via `MappingProxyType` so callers cannot mutate.
  - `_lock: threading.RLock` — held only during `apply_diff()` (microseconds).
  - `snapshot() -> Mapping[str, Any]` — returns the current frozen dict reference; callers cache it for the duration of one frame.
  - `apply_diff(diff: dict, *, actor: str) -> AppliedResult` — validates all keys cross-field, builds a new dict, atomic-swaps the reference, fires subscriber callbacks, returns the resolved diff.
  - `reset(keys: list[str] | None) -> AppliedResult` — same atomic semantics.
  - `on_change(callback)` — register a subscriber. Subscribers run on the apply thread, must not raise.
  - `register_subscriber_for(keys: list[str], callback)` — fine-grained variant; only fires when one of the listed keys changes (used by `services/llm.py` to rebuild `_HAIKU_BUCKET` only when bucket params change).
- `STORE: SettingsStore` singleton at module bottom.
- `class SettingsValidationError(Exception)` carrying a list of `{key, reason}` dicts (returned as 422 body).

### 1.2 Extend [road_safety/config.py](road_safety/config.py) with `SETTINGS_SPEC`

Add a `SETTINGS_SPEC: list[SettingSpec]` registry near the bottom of the file (the SoT for defaults + ranges + categories). Existing module constants stay (used at import-time before STORE is initialized) — the STORE is seeded from `SETTINGS_SPEC` at startup with the *current* values of those constants.

**24 tunables shipped in v1** (full scope, per user choice):

| # | Key | Default | Range | Category | Source |
|---|---|---|---|---|---|
| 1 | `TARGET_FPS` | 2.0 | 0.5–10 | performance | config.py |
| 2 | `MAX_RECENT_EVENTS` | 500 | 50–5000 | performance | config.py |
| 3 | `PAIR_COOLDOWN_SEC` | 8.0 | 1–60 | dedup | config.py |
| 4 | `CONF_THRESHOLD` | 0.50 | 0.10–0.95 | detection | core/detection.py |
| 5 | `PERSON_CONF_THRESHOLD` | 0.25 | 0.10–0.95 | detection | core/detection.py |
| 6 | `VEHICLE_PAIR_CONF_FLOOR` | 0.60 | 0.30–0.95 | detection | core/detection.py |
| 7 | `MIN_BBOX_AREA` | 1200 | 400–4000 | detection | core/detection.py |
| 8 | `TTC_HIGH_SEC` | 0.5 | 0.05–3.0 | risk-tier | core/detection.py |
| 9 | `TTC_MED_SEC` | 1.0 | 0.10–6.0 | risk-tier | core/detection.py |
| 10 | `DIST_HIGH_M` | 2.0 | 0.5–20 | risk-tier | core/detection.py |
| 11 | `DIST_MED_M` | 5.0 | 1–50 | risk-tier | core/detection.py |
| 12 | `MIN_SCALE_GROWTH` | 1.10 | 1.01–2.0 | gating | core/detection.py |
| 13 | `TRACK_HISTORY_LEN` | 12 | 4–60 | gating | core/detection.py |
| 14 | `LLM_BUCKET_CAPACITY` | 3.0 | 1–20 | llm-cost | services/llm.py |
| 15 | `LLM_BUCKET_REFILL_PER_MIN` | 3.0 | 0.5–60 | llm-cost | services/llm.py |
| 16 | `LLM_CB_THRESHOLD` | 3 | 1–20 | llm-reliability | services/llm.py |
| 17 | `LLM_CB_COOLDOWN_SEC` | 60.0 | 5–600 | llm-reliability | services/llm.py |
| 18 | `SLACK_HIGH_MIN_DURATION_SEC` | 1.5 | 0.0–10 | alerting | integrations/slack.py |
| 19 | `SLACK_HIGH_MIN_FRAMES` | 2 | 1–20 | alerting | integrations/slack.py |
| 20 | `SLACK_HIGH_MIN_CONFIDENCE` | 0.55 | 0.30–0.95 | alerting | integrations/slack.py |
| 21 | `SLACK_MIN_RISK` | "high" | enum {low,medium,high} | alerting | integrations/slack.py |
| 22 | `QUALITY_BLUR_SHARP` | 40.0 | 10–200 | quality | core/quality.py |
| 23 | `QUALITY_LOW_LIGHT_LUM` | 45.0 | 10–120 | quality | core/quality.py |
| 24 | `ALPR_MODE` | "off" | enum {off,on,on_demand} | privacy | config.py |

Cross-field validators required:
- `TTC_MED_SEC > TTC_HIGH_SEC`
- `DIST_MED_M > DIST_HIGH_M`
- `LLM_BUCKET_CAPACITY >= 1`
- `MIN_SCALE_GROWTH > 1.0`
- **▲ v1.1:** `SLACK_HIGH_MIN_CONFIDENCE >= VEHICLE_PAIR_CONF_FLOOR` (otherwise Slack never fires — detection already suppressed).
- **▲ v1.1:** `LLM_BUCKET_REFILL_PER_MIN` is a UI-facing unit only; `services/llm.py` builds `TokenBucket(refill_per_sec=REFILL_PER_MIN/60.0)`. The subscriber must perform the divide — do not pass per-minute into the bucket constructor.

**▲ v1.1: Scene-context coupling.** `SceneContextClassifier.adaptive_thresholds()` currently returns per-scene multipliers (`ttc_multiplier`, `pixel_dist_multiplier`) that are applied on top of `TTC_*` / `DIST_*` in the hot path ([road_safety/core/context.py](road_safety/core/context.py)). The SETTINGS_SPEC value is the *operator-set base*; the *effective* value seen by gates is `base * multiplier`. The UI MUST display both (e.g. `TTC_HIGH_SEC 0.5 (eff 0.65, urban)`) or operators will misread why impact differs from expectation. Impact baselines also need scene distribution captured (see §2.1).

### 1.3 Convert hot-path constants to snapshot reads

Smallest possible diff in each module. Top of each module gets:

```python
from road_safety.settings_store import STORE
```

Each function that uses a tunable starts with `cfg = STORE.snapshot()` (one read per frame, not per comparison). Files touched:

- [road_safety/core/detection.py](road_safety/core/detection.py) — replace `CONF_THRESHOLD`, `VEHICLE_PAIR_CONF_FLOOR`, `PERSON_CONF_THRESHOLD`, `MIN_BBOX_AREA`, `TTC_HIGH_SEC`, `TTC_MED_SEC`, `DIST_HIGH_M`, `DIST_MED_M`, `MIN_SCALE_GROWTH` reads inside `_classify_risk`, `_pair_risk`, `_filter_detections`. **▲ v1.1:** pass the single `cfg` reference through nested call args (do NOT re-snapshot in inner helpers) to guarantee all gates in one frame see the same config.
- `TRACK_HISTORY_LEN` — **▲ v1.1:** `deque(maxlen=N)` does not support resizing in place. Subscriber must iterate `TrackHistory._trails` and for each track rebuild the deque: `trails[tid] = deque(list(old)[-N:], maxlen=N)`. Tracks whose history currently exceeds the new maxlen are truncated from the head; tracks below stay intact. This may briefly degrade TTC quality for tracks that lose history; worst case is ~1 missed TTC sample per affected track. Document as expected behavior.
- [road_safety/core/quality.py](road_safety/core/quality.py) — `_THRESH["blur_sharp"]` and `_THRESH["low_light_lum"]` become snapshot reads inside `update()`/`state()`.
- [road_safety/services/llm.py](road_safety/services/llm.py) — `_HAIKU_BUCKET` rebuilt via `STORE.register_subscriber_for(["LLM_BUCKET_CAPACITY","LLM_BUCKET_REFILL_PER_MIN"], _rebuild_bucket)`. **▲ v1.1:** `_rebuild_bucket` must divide `REFILL_PER_MIN/60.0` before passing to `TokenBucket(refill_per_sec=...)`. `_CB_THRESHOLD` and `_CB_COOLDOWN_SEC` become snapshot reads inside `_circuit_open()` / `_cb_record()`.
- [road_safety/integrations/slack.py](road_safety/integrations/slack.py) — `SLACK_HIGH_*` and `_MIN_RISK` (module var) become snapshot reads.
- [road_safety/server.py](road_safety/server.py) — **▲ v1.1:** `_run_loop` reads `TARGET_FPS` **once at loop entry** (consistent with `requires_restart=true`); `MAX_RECENT_EVENTS`, `PAIR_COOLDOWN_SEC`, `ALPR_MODE` become snapshot reads per loop iteration (cheap and hot-reloadable).

**▲ v1.1: `TARGET_FPS` hot-reload semantics (resolved contradiction).** `TARGET_FPS` is `requires_restart=true` in v1. The apply endpoint *accepts* a new value and persists it to the store (so the UI shows the pending value), but the loop does not re-read it; it stays on its boot-time tick rate until restart. The row shows a yellow badge: `applied on next restart`. (v2 follow-up: timer rebuild.)

**▲ v1.1: Subscriber exception isolation.** Subscriber callbacks run on the apply thread inside the RLock. Wrap each dispatch in `try: cb(before, after) except Exception as e: log_warning(...); result.warnings.append(f"{cb.__name__}: {e}")`. The PUT response surfaces `warnings` so the operator sees *"the config applied but subscriber X failed to rebuild"* instead of a 500. Never let a bad subscriber take down the store.

**▲ v1.1: Interaction with `AdaptiveThresholds`.** The snapshot reads supply *base* thresholds; `SceneContextClassifier.adaptive_thresholds()` still applies per-scene multipliers on top. This is intentional (scene adaptation is load-bearing for the false-positive suppression story in CLAUDE.md). The UI must display both base and effective values (see §3.2).

### 1.4 Create [road_safety/api/settings.py](road_safety/api/settings.py)

FastAPI router mounted on `server.py` with `app.include_router(settings.router)`. All routes guarded by `require_bearer_token(...)`, all mutators write one `compliance/audit.py::log()` row.

| Route | Purpose | Body / Response |
|---|---|---|
| `GET /api/settings` | List with current values + spec | `{settings:[{key,value,default,min,max,type,category,description,requires_restart}], categories:[...]}` |
| `PUT /api/settings` | Bulk apply | body `{diff:{...},note?:str}` → `{applied:{...},change_id,change_ts,baseline_window_sec}`; 422 on validation |
| `POST /api/settings/preview` | Dry-run validate | body `{diff}` → resolved diff + which subscribers would re-init |
| `POST /api/settings/reset` | Revert to defaults | optional `{keys:[...]}` |
| `GET /api/settings/templates` | List templates | `{templates:[{id,name,description,values,created_at,updated_at,system}]}` |
| `POST /api/settings/templates` | Create | `{name,description,values}` |
| `PUT /api/settings/templates/{id}` | Update (409 if `system`) | `{name?,description?,values?}` |
| `DELETE /api/settings/templates/{id}` | Delete (409 if `system`) | — |
| `POST /api/settings/templates/{id}/apply` | Apply template + capture baseline | — |
| `POST /api/settings/revert_last` | One-click revert to pre-change values | — (uses synthetic `_last_good` snapshot) |
| `GET /api/settings/baseline` | Active session baseline | session JSON |
| `GET /api/settings/impact` | One-shot before/after | session JSON with `narrative` |
| `GET /api/settings/impact/stream` | SSE | per-15s push (see Phase 3) |
| `POST /api/settings/impact/ticket` | **▲ v1.1:** Issue single-use 30 s ticket for SSE auth | `{ticket, expires_in}` |
| `GET /api/settings/impact/history?limit=20` | Archived sessions (reads `data/settings_history.jsonl`) | list |
| `GET /api/settings/effective` | **▲ v1.1:** Base + scene-multiplied effective values for scene-adapted tunables | `{key: {base, effective, multiplier, scene}}` |

### 1.5 Create [road_safety/services/templates.py](road_safety/services/templates.py)

File-backed CRUD on `data/settings_templates.json`:

- Schema: `{"version":1, "templates":[{"id":"tpl_<uuid7>","name","description","values":{},"created_at","updated_at","system":false}]}`.
- Atomic write via `os.replace()` on a `.tmp` sibling.
- `threading.Lock` around **both** mutations and reads (**▲ v1.1:** reads without the lock could observe a partially-rewritten file if a write crashes mid-flight, even with `os.replace()`. Hold the lock for the whole read-parse cycle — the file is tiny, contention is negligible).
- Synthetic `tpl_default` template (system=true, never persisted) returned at the head of `list_templates()`. **▲ v1.1:** its `values` come from `SETTINGS_SPEC` defaults (NOT the current live snapshot) — otherwise "default" drifts each time something is tuned. PUT/DELETE return 409.
- Synthetic `_last_good` is *not* persisted — it lives only in `services/impact.py`'s session state (see §2.1 for lifetime rules).

**▲ v1.1: Template apply — re-validation and spec migration.**
When a template is applied, treat its stored `values` as untrusted input (spec may have evolved since the template was saved):

1. **Drop** keys not in current `SETTINGS_SPEC` (log `settings.template.key_dropped`).
2. **Fill** keys present in current spec but missing from template with current spec defaults (log `settings.template.key_filled`).
3. **Validate** the merged dict against current min/max/enum + cross-field rules. On failure, return 422 — do NOT partially apply. The response lists invalid keys; the UI prompts the operator to edit the template.
4. Only on clean validation does `STORE.apply_diff()` run.

This prevents a v2 spec tightening (e.g. raising `CONF_THRESHOLD` minimum) from allowing old templates to silently bypass new floors.

---

## Phase 2 — Backend: AI impact analysis engine

### 2.1 Create [road_safety/services/impact.py](road_safety/services/impact.py)

```python
@dataclass
class WindowStats:
    window_start_ts: float; window_end_ts: float; duration_sec: float
    sample_size: int; confidence: str  # "low"|"medium"|"high"
    event_rate_per_min: float
    severity_counts: dict[str, int]; severity_ratios: dict[str, float]
    ttc_p50: float | None; ttc_p95: float | None
    distance_p50_m: float | None; distance_p95_m: float | None
    fp_rate: float | None; fp_rate_source: str  # "feedback"|"proxy"|"insufficient"
    episode_duration_mean: float | None; episode_duration_p95: float | None
    confidence_mean: float | None
    llm_cost_usd_per_min: float; llm_latency_p95_ms: float; llm_skip_rate: float
    enrichment_skipped_rate: float
    frames_read: int; frames_processed: int; frames_processed_ratio: float
    # ▲ v1.1: scene capture — without this, a baseline recorded under
    # "urban" that's then compared against an "after" window of mostly
    # "highway" will look like the threshold change caused the delta,
    # when it's actually scene drift applying different multipliers.
    scene_distribution: dict[str, float]  # {"urban": 0.72, "highway": 0.28, ...}
    quality_distribution: dict[str, float]  # {"nominal": 0.9, "blurred": 0.1, ...}

@dataclass
class ImpactSession:
    audit_id: str; change_ts: float; actor: str
    before: dict; after: dict
    changed_keys: list[str]       # ▲ v1.1: explicit list of keys in this (possibly coalesced) session
    baseline: WindowStats
    history: list[WindowStats]    # capped 60 = 15 min of ticks
    last_narrative: dict | None
    last_good: dict               # ▲ v1.1: the snapshot `before` the FIRST change (preserved across coalesce)
    state: str                    # "monitoring" | "monitoring_unattended" | "archived"
    warnings: list[str]
```

`ImpactMonitor` (instance held on `state.impact`):

- `on_settings_change(before, after, actor) -> audit_id` — captures baseline. Coalesces with prior session if `< COALESCE_WINDOW_SEC=30` ago: composite diff, original baseline reused, after-window reset.
- Baseline lookback: starts at 300 s, expands to 600 → 1200 → 1800 s if `< MIN_BASELINE_EVENTS=20`. Below threshold after expansion → `confidence="low"`, proceed anyway.
- `run_loop()` — asyncio task ticking every `IMPACT_TICK_SEC=15`:
  1. Compute `after = compute_window(events_since_change, change_ts, now)` capped at `AFTER_WINDOW_SEC=300`.
  2. `deltas = compute_deltas(baseline, after)` — percent for non-zero baseline, absolute otherwise.
  3. Decide whether to call LLM (cadence rule below).
  4. Append to `session.history`.
  5. Broadcast to `state.impact_subscribers`.
  6. Archive sessions older than `IMPACT_SESSION_MAX_AGE_SEC=3600`.

LLM cadence: call `analyze_settings_impact` when `max(|pct_delta_top5|) >= 0.20` OR `now - last_call >= 60` OR recommendation flipped — never more than once per 30 s.

FP-rate proxy when `< MIN_FEEDBACK=5` feedback verdicts:
`proxy = 0.5*risk_demoted_rate + 0.3*sustained_failure_rate + 0.2*terse_episode_rate`.

**▲ v1.1: `_last_good` and session lifecycle (fully specified).**

| Event | `_last_good` behavior |
|---|---|
| First change applied (session created) | `session.last_good = before_snapshot` |
| Subsequent change within `COALESCE_WINDOW_SEC=30` | **Preserved** (still points at the original `before`) so revert takes you back to before the *first* change, not the last one |
| Subsequent change after coalesce window | **New session**, new `last_good = current before` |
| Session archived at `IMPACT_SESSION_MAX_AGE_SEC=3600` | Copied into the archived record. Revert is still possible for archived sessions via `POST /api/settings/revert_last?session_id=…` within a 24 h grace window |
| Server restart | In-memory session lost. If the prior session wrote to `settings_history.jsonl` (see §2.6), `_last_good` is recoverable on demand; otherwise revert button is disabled with tooltip "no recent change to revert" |

**▲ v1.1: Session archive soft-warning.** At `now - change_ts >= IMPACT_SESSION_MAX_AGE_SEC - 600` (50 min), set `state="monitoring_unattended"` and surface a banner "this session archives in 10 minutes — Keep or Revert?". Hard archival at 60 min still applies. Prevents operators losing their monitoring window without warning.

**▲ v1.1: Impact history persistence.** On archive, append `{session_id, change_ts, actor, changed_keys, final_recommendation, last_good, baseline_summary, final_window_summary}` (compact, no per-tick history) to `data/settings_history.jsonl`. `GET /api/settings/impact/history?limit=20` reads from this file (tailing, not loading the whole thing). Retention sweep drops entries older than 90 d.

### 2.2 Add `analyze_settings_impact()` to [road_safety/services/llm.py](road_safety/services/llm.py)

Sits next to `narrate_event` / `enrich_event` / `chat`. Costs **1 token** from `_HAIKU_BUCKET`. Routes through `_complete()` (provider failover) and records via `llm_observer.record(call_type="settings_impact", ...)` — auto-appears in `/api/llm/stats`.

```python
SETTINGS_IMPACT_SYSTEM = (
    "You are a road-safety configuration analyst. Given baseline vs after-change "
    "metrics for a fleet detection pipeline, identify the IMPACT in 2-3 sentences "
    "(<=80 words) and recommend KEEP / REVERT / MONITOR. Cite the largest deltas. "
    "Reference scene context if it shifted. Return STRICT JSON, no markdown: "
    '{"narrative": str, "recommendation": "keep"|"revert"|"monitor", '
    '"confidence": "low"|"medium"|"high"}. '
    "Recommend REVERT only if a critical safety metric (high-severity event rate, "
    "ttc_p95, fp_rate) degraded materially."
)

async def analyze_settings_impact(
    change_summary: dict, baseline: "WindowStats", after: "WindowStats",
    *, operator_hint: str | None = None,
) -> dict | None: ...
```

Returns `None` on rate-budget exhaustion / circuit-open / parse failure — caller renders numeric-only impact.

### 2.3 Wire impact monitor into [road_safety/server.py](road_safety/server.py) lifespan

In the existing `lifespan` startup (after the agent executor wiring), build `state.impact = ImpactMonitor(events_source=lambda: list(state.recent_events), ...)` and `state.impact_task = asyncio.create_task(state.impact.run_loop())`. Add `state.impact_subscribers: list[asyncio.Queue]` next to existing SSE subscriber lists. Cancel `state.impact_task` in shutdown alongside `retention_task`.

### 2.4 Wire `PUT /api/settings` to call `state.impact.on_settings_change(before, after, actor)`

After successful `STORE.apply_diff()`, capture pre/post snapshots and call into the monitor before responding. The endpoint returns the new `audit_id` so the frontend can subscribe to the impact stream for that specific session.

### 2.5 SSE `/api/settings/impact/stream`

**▲ v1.1:** Modeled on the existing `/stream/events` handler in `road_safety/server.py` (plan v1.0 incorrectly referenced `/api/live/stream`, which does not exist). Per tick payload:

```json
{
  "audit_id": "...", "change_ts": 1713400000.0,
  "tunables": {"before": {...}, "after": {...}},
  "baseline": <WindowStats>, "after_change": <WindowStats>,
  "deltas": {"event_rate_per_min": -38.2, "ttc_p95": 0.15, ...},
  "narrative": "Raising TTC_HIGH 0.5→0.8s produced 38% fewer high-severity...",
  "recommendation": "keep", "confidence": "medium",
  "sample_size_baseline": 47, "sample_size_after": 31,
  "session_state": "monitoring", "warnings": []
}
```

On connect, replay the latest snapshot of the active session. Keepalive every 15 s.

### 2.6 Audit-log entries (via [road_safety/compliance/audit.py](road_safety/compliance/audit.py))

| action | when | detail |
|---|---|---|
| `settings.apply` | every PUT/template apply | `{before, after, actor, audit_id, warnings}` |
| `settings.preview` | POST /preview | `{diff}` **▲ v1.1:** throttled to 1 row/min/actor (preview is cheap; audit bloat risk otherwise) |
| `settings.privacy_change` | **▲ v1.1:** when ALPR_MODE changes in **any direction** (off↔on, off↔on_demand, on↔on_demand) | `{actor, prior, new, direction}` |
| `settings.template.create/update/delete/apply` | template CRUD | `{template_id, name}` |
| `settings.template.key_dropped` / `settings.template.key_filled` | **▲ v1.1:** template-apply spec-migration events | `{template_id, key, reason}` |
| `settings.revert_last` | one-click revert | `{from_audit_id, restored_values}` |
| `settings.apply.subscriber_failed` | **▲ v1.1:** subscriber exception during dispatch | `{audit_id, subscriber, error}` |
| `impact.baseline_captured` | once per session | `{audit_id, sample_size, lookback_sec, confidence}` |
| `impact.significant_delta` | when LLM call triggered | `{audit_id, deltas_top3}` |
| `impact.recommendation_changed` | rec flips | `{audit_id, prev, new}` |
| `impact.session_archived` | at MAX_AGE | `{audit_id, tick_count, final_recommendation}` |

---

## Phase 3 — Frontend: Settings page

### 3.1 Routing & nav

- [frontend/src/App.tsx](frontend/src/App.tsx) — add `<Route path="/settings" element={<SettingsPage />} />` after the Monitoring route (~line 75).
- [frontend/src/components/layout/TopBar.tsx](frontend/src/components/layout/TopBar.tsx) — add `<Link to="/settings" className={pathname === "/settings" ? styles.active : ""}>Settings</Link>` after the Monitoring `<Link>` (~line 125). Add `{adminTokenCached && <span className={styles.adminBadge}>admin</span>}` between the Pill and `{children}`.

### 3.2 Page layout (3-column desktop)

[frontend/src/pages/SettingsPage.tsx](frontend/src/pages/SettingsPage.tsx) + [SettingsPage.module.css](frontend/src/pages/SettingsPage.module.css) — `display: grid; grid-template-columns: 360px 1fr 420px`.

```
+-----------------------------------------------------------------------------+
| TopBar:  Admin  Dashboard  Monitoring [3]  Settings        [admin] cam-01  |
+----------------+-----------------------------+-----------------------------+
|  LIVE VIDEO    |  Settings   [Forget token]  | Templates       [+ Save as]|
|  (canvas reuse |  [Reset all]                | - Default (builtin) Apply  |
|  from VideoFeed)| v Detection                | - Night Mode        Apply  |
|  fps 14.8      |   YOLO conf  [====O--] 0.50 | - High-Recall  Edit  Del   |
|  Scene: urban  |   PERSON conf [==O---] 0.25 |                            |
|  Perception:   | v Risk Tier                 | -------------------------- |
|   nominal      |   TTC_HIGH   [O-----] 0.5s  | Baseline  (sc_abc123)      |
|  Source:       |   TTC_MED    [-O----] 1.0s  | Captured 12:04:11 UTC      |
|   /vid/cam01   | > Gating                    | Window: 47 events / 5 min  |
|                | > LLM / Cost                | event_rate 4.2/min         |
|                | > Alerting                  | ttc_p95 0.6s  fp_rate 7%   |
|                | > Performance               | -------------------------- |
|                | > Privacy                   | Impact (live) [confident]  |
|                |                             | event_rate 4.2 → 3.1 (-26%)│
|                |                             | ttc_p95   0.6 → 0.9 (+50%) │
|                |                             | llm_cost  $.18 → $.11      │
|                |                             | fp_rate    7% → 5%         │
|                |                             | [bar chart: before/after]  │
|                |                             | AI: "Tighter TTC reduced   │
|                |                             |  HIGH events 26%, fp drop  │
|                |                             |  modest. Recommend KEEP."  │
|                |                             | [Revert to last-known-good]│
+----------------+-----------------------------+-----------------------------+
```

Responsive breakpoints (CSS modules, no Tailwind):
- `≤ 1280px`: collapse right rail below center.
- `≤ 900px`: single column; video sticky-top at 240 px.
- `≤ 600px`: collapse all category sections by default.

**▲ v1.1: Effective-value display for scene-adapted tunables.** `TTC_HIGH_SEC`, `TTC_MED_SEC`, `DIST_HIGH_M`, `DIST_MED_M` rows additionally render an inline pill `eff 0.65s · urban ×1.3` next to the slider. The effective value comes from a new lightweight endpoint `GET /api/settings/effective` (returns `{key: {base, effective, multiplier, scene}}`), polled every 5 s alongside the live context badge. Without this, operators misread A/B deltas as caused by their edit when the real driver is scene drift.

### 3.3 Components to create

All under `frontend/src/components/settings/`:

| File | Responsibility | Reuses |
|---|---|---|
| `SettingsPanel.tsx` | Render `<details>`/`<summary>` per category, iterate tunables | — |
| `TunableControl.tsx` | One row: `<label>` + control (slider \| input \| select \| toggle) + units + reset glyph + dirty/error indicators | — |
| `TemplateManager.tsx` | List + add/edit/delete/apply buttons | `Pill`, `Tag` |
| `TemplateModal.tsx` | Native `<dialog>` with name/description fields + diff preview | — |
| `ConfirmDialog.tsx` | Generic `<dialog>` for confirmations | — |
| `BaselinePanel.tsx` | Show captured baseline metrics + sample-size + window | `Pill` |
| `ImpactPanel.tsx` | Side-by-side numbers + AI narrative + revert button | `Pill`, `Tag`, `RiskBadge`, `ImpactChart` |
| `ImpactChart.tsx` | recharts BarChart for delta comparison; pure SVG for sparkline kind | recharts |
| `ContextBadge.tsx` | Scene + perception + fps mini-widget for left rail | `Pill`, `Dot` |
| `Toast.tsx` + `ToastHost.tsx` | Tiny toast system (none today); `aria-live="polite"` | — |
| `index.ts` | Barrel re-export | — |

Hooks under `frontend/src/hooks/`:

| File | Responsibility |
|---|---|
| `useSettings.ts` | `usePolling(api.getSettings, 15_000)` + `applyChanges(diff)` with 400 ms debounce + optimistic update + rollback on error |
| `useSettingsTemplates.ts` | CRUD via `api.*Template*`; refetch on success |
| `useImpactStream.ts` | SSE wrapper for `/api/settings/impact/stream`; falls back to 20 s polling after 3 reconnect failures |
| `useAdminToken.ts` | `sessionStorage` get/set/clear + custom event for cross-component refresh |

Lib:

| File | Responsibility |
|---|---|
| `lib/adminAuth.ts` | `withAdminAuth(init)` injects `Authorization: Bearer …`; throws `MissingAdminTokenError`. `adminUrl(path)` appends `?token=…` for SSE (EventSource cannot set headers) |
| `lib/api.ts` (extend) | Add `getSettings`, `putSettings`, `previewSettings`, `resetSettings`, `listTemplates`, `createTemplate`, `updateTemplate`, `deleteTemplate`, `applyTemplate`, `revertLast`, `getBaseline`, `getImpact` |
| `types.ts` (extend) | Add `Tunable`, `SettingsResponse`, `Template`, `BaselineSnapshot`, `ImpactPayload`, `ImpactConfidence`, `Recommendation` |

### 3.4 Admin token handling

Prompt-on-first-write + **sessionStorage** (auto-clears on tab close, smaller XSS window than localStorage). On `MissingAdminTokenError`, mount `<TokenPromptDialog>` (uses `<ConfirmDialog>` styles); user pastes token → stash → re-run original write. "Forget token" link in SettingsPage header. TopBar shows small `admin` badge when token cached.

Document the localStorage-vs-sessionStorage tradeoff in a comment block at the top of `adminAuth.ts`.

**▲ v1.1: SSE auth via ephemeral ticket (not query-string token).** `EventSource` cannot set headers, but putting `ROAD_ADMIN_TOKEN` in `?token=` leaks it into access logs, browser history, and any `Referer` a subsequent navigation sends. Instead:

1. Frontend calls `POST /api/settings/impact/ticket` with the admin bearer header → server returns `{ticket: "<opaque 32-byte hex>", expires_in: 30}`.
2. Frontend opens `new EventSource("/api/settings/impact/stream?ticket=<ticket>")`.
3. Server validates ticket on connect, consumes it (single-use), keeps the SSE open until drop.
4. Tickets stored in a `{ticket: (actor, exp)}` dict in-memory; janitor sweeps expired every 60 s.
5. Access-log middleware strips `?ticket=` query params (belt-and-suspenders) — but even if not stripped, the ticket is single-use and 30-second-TTL, so log leakage is nearly harmless compared to leaking the long-lived admin token.

Audit log: `settings.impact.ticket_issued` (per issuance), `settings.impact.ticket_rejected` (on replay or expired-use). Rate limit: 30 ticket requests / min / actor.

### 3.5 Optimistic update + debounce

`useSettings` keeps a `lastConfirmedRef` for rollback. Slider drags coalesce into one PUT after 400 ms of quiescence. Each PUT carries a monotonic `requestSeq`; stale responses (older than `latestAckedSeq`) are dropped to avoid races.

### 3.6 Empty / error / loading

- Initial load → skeleton rows.
- Pre-baseline → "Apply a change to capture a baseline."
- Pre-after-window-fill → "Gathering data… N/20 events observed" with thin progress bar.
- PUT failure → row red ring + inline error + toast. No page crash.
- LLM unavailable → render numbers + "AI analysis unavailable."
- 401 → toast "Token rejected", clear token, re-prompt (no silent retry — per `.claude/rules/frontend.md`).
- SSE drop → fall back to 20 s polling, show "live updates paused" pill.

### 3.7 Revert flow (per user choice)

When `recommendation === "revert"`:

1. `<ImpactPanel>` shows red banner: "AI recommends REVERT — high-severity rate up 240%."
2. Single `[Revert to last-known-good]` button → `<ConfirmDialog>` summarizing what will change → `POST /api/settings/revert_last`.
3. Server restores the pre-change snapshot (pulled from the active `ImpactSession`) and starts a fresh impact session.
4. Audit-logged with `action="settings.revert_last"`.

No auto-revert. No timer. Operator stays in control.

**▲ v1.1: Revert button disabled states.**
- No active session → button disabled, tooltip "No recent change to revert".
- Session in `archived` state & older than 24 h grace → button disabled, tooltip "Change too old; recreate manually".
- Server restart while session active → on page reload, if `last_good` is recoverable from `settings_history.jsonl` (not yet archived), re-enable with "Revert last change from <ts>"; otherwise disabled.

### 3.8 Accessibility

- All sliders: native `<input type="range">` with `step` from spec → arrow keys work natively.
- Native `<dialog>` for modals → focus trap + Esc dismiss for free.
- `<details>`/`<summary>` for collapsible sections.
- `:focus-visible` outline on every interactive control.
- Color-only signals (red ring on error, accent ring on dirty) get sibling text indicators (WCAG 1.4.1).
- Toast host uses `aria-live="polite"`.

### 3.9 Add `recharts` dependency

`cd frontend && npm install recharts@^2.13.0` — bundle hit ~90 KB gzipped, route-split so it only loads when `/settings` is visited.

---

## Files: create vs modify (full list)

### NEW backend
- [road_safety/settings_store.py](road_safety/settings_store.py) — STORE singleton, snapshots, atomic apply, subscribers.
- [road_safety/api/settings.py](road_safety/api/settings.py) — FastAPI router with all `/api/settings/*` routes.
- [road_safety/services/impact.py](road_safety/services/impact.py) — WindowStats, ImpactSession, ImpactMonitor, FP proxy.
- [road_safety/services/templates.py](road_safety/services/templates.py) — file-backed CRUD + atomic writes.

### MODIFY backend
- [road_safety/config.py](road_safety/config.py) — add `SETTINGS_SPEC` registry.
- [road_safety/server.py](road_safety/server.py) — mount router, lifespan wires `state.impact` + task, `state.impact_subscribers`, `_run_loop` reads snapshot, `PUT /api/settings` calls `on_settings_change`, SSE endpoint.
- [road_safety/core/detection.py](road_safety/core/detection.py) — snapshot reads for 9 constants.
- [road_safety/core/quality.py](road_safety/core/quality.py) — snapshot reads for 2 thresholds.
- [road_safety/services/llm.py](road_safety/services/llm.py) — add `analyze_settings_impact()`; rebuild-bucket subscriber; CB params via snapshot.
- [road_safety/integrations/slack.py](road_safety/integrations/slack.py) — snapshot reads for 4 SLACK_* constants.

### NEW frontend
- `frontend/src/pages/SettingsPage.tsx` + `.module.css`
- `frontend/src/components/settings/` — 11 components (see 3.3)
- `frontend/src/hooks/useSettings.ts`, `useSettingsTemplates.ts`, `useImpactStream.ts`, `useAdminToken.ts`
- `frontend/src/lib/adminAuth.ts`

### MODIFY frontend
- [frontend/src/App.tsx](frontend/src/App.tsx) — add `/settings` route.
- [frontend/src/components/layout/TopBar.tsx](frontend/src/components/layout/TopBar.tsx) + `.module.css` — add Settings link + admin badge.
- `frontend/src/lib/api.ts` — add settings/templates/impact methods.
- `frontend/src/types.ts` — add Tunable, Template, ImpactPayload types.
- `frontend/package.json` — add `recharts`.

### NEW tests
- `tests/test_settings_store.py` — apply/reset/atomic-validation/snapshot-isolation/subscribers. **▲ v1.1:** + subscriber-raises-exception → warning surfaced, store still applies; + `TRACK_HISTORY_LEN` deque rebuild preserves tail within new maxlen.
- `tests/test_settings_api.py` — auth tier, validation 422, audit row written, preview. **▲ v1.1:** + SSE ticket issuance + single-use consumption + expiry; + `GET /api/settings/effective` returns scene-multiplied values.
- `tests/test_settings_templates.py` — CRUD, default-template immutability, atomic write. **▲ v1.1:** + apply old template with key dropped by spec → dropped + audit logged; + apply with key missing from template → filled from default; + apply violating new cross-field validator → 422.
- `tests/test_impact.py` (`@pytest.mark.asyncio`) — baseline lookback expansion, coalescing, percentile math, delta computation, FP proxy fallback, LLM-unavailable degradation, `revert_last` flow. **▲ v1.1:** + coalesce preserves `_last_good` across multiple rapid changes; + session archived after 1 h, revert within 24 h grace works; + scene shift between baseline and after surfaces in `scene_distribution` delta.
- **▲ v1.1:** `tests/test_privacy_invariant.py` — see verification step 7 (this replaces the fictitious `test_no_plate_leak`).

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Operator sets `TTC_HIGH_SEC=0.01` | `min` in spec catches it; cross-field validator blocks `TTC_MED <= TTC_HIGH`; 422 with structured errors. |
| Mid-frame race between read and apply | Snapshot pattern: store builds a new dict and atomic-rebinds; in-flight readers keep old reference. No torn reads. |
| Audit-log bloat | PUTs are operator-driven (handfuls/day). History JSONL trimmed by retention sweep (>90 d). |
| FP storm after a bad apply | (a) Auto-detection: `fp_rate > 3× baseline` → recommendation flips to revert + warning banner. (b) One-click revert to `_last_good`. No silent auto-revert. |
| Rapid config thrashing | `COALESCE_WINDOW_SEC=30` composite session; `MIN_CHANGE_INTERVAL_SEC=5` debounce returns 429. |
| ALPR_MODE flip off→on (privacy) | Endpoint requires `?confirm_privacy_change=1`; distinct `audit.privacy_change` row. |
| LLM cost runaway | `analyze_settings_impact` caps at 1 token/30 s; shared bucket protects narration/enrichment. Empty bucket → numeric-only impact. |
| `TARGET_FPS` hot-reload subtlety | Marked `requires_restart=true` in v1; yellow badge in UI. v2 follow-up rebuilds timer. |
| EventSource cannot set Authorization header | `adminUrl(path)` helper appends `?token=…`; server accepts query-string token on SSE routes only (logged + rate-limited). |
| Operator abandons monitoring | At 10 min idle, set `session_state="monitoring_unattended"` + advisory banner. Never auto-revert. |
| **▲ v1.1:** Subscriber raises during apply | Wrap each dispatch in try/except; log `settings.apply.subscriber_failed` audit; surface in `AppliedResult.warnings`; never propagate to a 500. |
| **▲ v1.1:** Scene drift confounds A/B impact | `WindowStats.scene_distribution` captured for baseline and after; LLM prompt explicitly flags when distribution shifts >20 pp; UI shows scene pie next to comparison chart. |
| **▲ v1.1:** Admin token leaks via SSE query param | Ephemeral single-use ticket exchange (§3.4); access-log middleware strips `?ticket=`. |
| **▲ v1.1:** Old template violates new spec | Template apply re-validates + migrates (drop unknown keys, fill with defaults, 422 on cross-field violation). Never partial apply. |
| **▲ v1.1:** Impact history empty after restart | Archive writes to `data/settings_history.jsonl`; history endpoint tails that file. |
| **▲ v1.1:** CSRF on admin PUT/POST | Bearer token in `Authorization` header is not auto-attached by browsers (unlike cookies), so CSRF is structurally mitigated. Document this explicitly in `adminAuth.ts` header comment so a future switch to cookie-auth doesn't silently regress it. |

---

## Verification (end-to-end)

1. **Backend unit tests**
   `pytest tests/test_settings_store.py tests/test_settings_api.py tests/test_settings_templates.py tests/test_impact.py -v`
2. **Existing detection gates intact**
   `pytest tests/test_core.py -v` — must still pass with snapshot-read refactor.
3. **Cheap lint**
   `make lint` (py_compile on the entrypoints).
4. **Frontend type check**
   `cd frontend && npx tsc -b --noEmit`
5. **Frontend build**
   `cd frontend && npm run build`
6. **End-to-end smoke**
   - `python start.py` (builds FE + runs tests + boots server on :8000).
   - Navigate to `http://localhost:8000/settings`.
   - First write → token prompt → paste `ROAD_ADMIN_TOKEN`.
   - Drag `TTC_HIGH_SEC` slider 0.5 → 0.8 → confirm row turns dirty, PUT fires after 400 ms, baseline captures.
   - Wait ~60 s → confirm `<ImpactPanel>` shows numeric deltas.
   - Wait ~5 min or until `≥20` after-window events → confirm AI narrative arrives.
   - Save current as template "Conservative" → confirm appears in list.
   - Apply `Default` template → confirm values revert + new baseline starts.
   - Edit "Conservative" → change description → confirm persists across server restart (`docker compose restart` or stop/start).
   - Verify `data/audit.jsonl` contains `settings.apply`, `impact.baseline_captured`, `settings.template.create`, `settings.template.apply` rows.
   - Verify `/api/llm/stats` includes `settings_impact` call_type bucket.
7. **Regression — privacy invariant**
   Confirm `data/feedback.jsonl` and `data/audit.jsonl` contain no raw plate text after running through one full enrichment cycle. **▲ v1.1:** plan v1.0 claimed an existing `tests/test_core.py::test_no_plate_leak` — that test does **not** exist today. Add it as part of Phase 2: a new `tests/test_privacy_invariant.py::test_no_plate_in_audit_or_feedback` that (a) drives a synthetic event through `enrich_event()`, (b) grep-asserts no `plate_text` / `plate_state` field in any buffer or JSONL write, (c) runs via the standard suite so the invariant has ratcheted regression coverage.

---

## Out of scope (v2 candidates)

- Hot-reload `TARGET_FPS` without restart (timer rebuild).
- Per-role admin tokens (currently single shared `ROAD_ADMIN_TOKEN`).
- Cookie-based auth (httpOnly) instead of `sessionStorage` bearer.
- A 6th "Impact Deep Dive" agent (Sonnet, ≤5 tools) for operator-initiated drill-downs — keeps us under the agent tool-cap rule.
- Cloud receiver mirror of `data/settings_history.jsonl` for fleet-wide settings analytics.
- Auto-revert with operator-set policy (timer-driven, opt-in).
