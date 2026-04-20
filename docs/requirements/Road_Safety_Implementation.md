# Road Safety AI Platform — Implementation Document

**Feature:** Road Safety — Live Event Detection + LLM Copilot
**Product:** Road Safety AI Platform (Python / FastAPI)
**Created:** 2026-04-15
**Last Updated:** 2026-04-15
**Author:** Andrei Tekhtelev
**Status:** SHIPPED

**Source Documents:**
- **BRD:** `docs/requirements/Road_Safety_BRD.md` (v1.1)
- **TRD:** `docs/requirements/Road_Safety_TRD.md` (v1.1)
- **Challenges:** `docs/challenges.md`
- **Architecture:** `docs/architecture.md`

---

## How to Use This Document

This document is the execution handoff from the approved TRD into actual repository implementation.

- The approved TRD is contract-authoritative for behavior, state, API, auth, schema, privacy, and rollout truth.
- This document wins for repo-specific execution order, file plan, and implementation sequencing.
- If conflict exists between this document and the approved TRD → flag as Known Gap, resolve before coding.

**Workflow:**
1. Start at Phase 1 (core detection pipeline)
2. Complete phases in order — each phase lists its dependencies
3. Verify acceptance criteria after each phase
4. Move to the next phase

---

## Implementation Boundary

This document translates approved TRD contracts into repo-specific execution. It does not redefine:
- Feature scope or non-goals from BRD/TRD
- Behavior, state, API, auth, schema, or privacy contracts from TRD
- Cross-module boundaries defined in TRD §6.2

Belongs in this document:
- File-level change planning
- Phase sequencing and dependencies
- Module-by-module implementation detail
- Testing approach per phase
- Deployment preparation

---

## Phase Status

| Phase | Name | Status | Dependencies | Summary |
|---|---|---|---|---|
| 1 | Core Detection Pipeline | `SHIPPED` | None | Stream, detection, tracking, risk, events, SSE, dashboard |
| 2 | LLM Enrichment Layer | `SHIPPED` | Phase 1 | Narration, vision enrichment, chat copilot, RAG |
| 3 | Privacy & Redaction | `SHIPPED` | Phase 1 | Dual thumbnails, plate hashing, DSAR gating |
| 4 | Edge/Cloud Architecture | `SHIPPED` | Phase 1, 3 | HMAC-signed batched delivery, cloud receiver |
| 5 | Feedback & Drift Monitoring | `SHIPPED` | Phase 1 | Operator verdicts, rolling precision, active learning |
| 6 | Alerting & Digest | `SHIPPED` | Phase 1, 2 | Slack notifications, hourly/daily digests |
| 7 | LLM Observability & Resilience | `SHIPPED` | Phase 2 | Multi-provider failover, rate budget, circuit breaker, cost tracking |
| 8 | Compliance & Audit | `SHIPPED` | Phase 3, 5 | Audit trail, data retention, DSAR enforcement |
| 9 | Road Readiness | `SHIPPED` | Phase 1 | Vehicle/driver identity, safety scoring, road-wide aggregation |
| 10 | AI Agent Orchestration | `SHIPPED` | Phase 2, 5, 9 | Coaching, investigation, report agents |
| 11 | Evaluation Harness | `SHIPPED` | Phase 1 | Precision/recall eval, suite runner, regression detection |

---

## Architecture Overview

### System Components

| Component | Type | File | Description |
|---|---|---|---|
| Stream Reader | Pipeline | `stream.py` | Multi-protocol video reader (HLS/RTSP/mp4/webcam) |
| Object Detector | Pipeline | `detection.py` | YOLOv8n inference + ByteTrack tracking |
| Ego-Motion | Pipeline | `egomotion.py` | Optical flow camera-motion estimation |
| Scene Context | Engine | `context.py` | Scene classification + adaptive risk thresholds |
| Perception Quality | Engine | `quality.py` | Frame quality assessment (luminance, sharpness, confidence) |
| FastAPI Server | Orchestrator | `server.py` | Event lifecycle, API routing, SSE, episode management |
| LLM Client | Enrichment | `llm.py` | Narration, vision enrichment, chat with failover |
| PII Redactor | Privacy | `redact.py` | Face/plate blur, dual thumbnails, plate hashing |
| Edge Publisher | Transport | `road_safety/integrations/edge_publisher.py` | HMAC-signed batch delivery to cloud |
| Cloud Receiver | Transport | `cloud/receiver.py` | HMAC verification, event_id dedup, persistence |
| Feedback API | API | `road_safety/api/feedback.py` | Operator verdict ingestion |
| Drift Monitor | ML Ops | `road_safety/services/drift.py` | Rolling precision, trend detection, active learning |
| Slack Notifier | Alerting | `road_safety/integrations/slack.py` | Tiered Slack alerts |
| Digest Scheduler | Alerting | `road_safety/services/digest.py` | Hourly/daily summary schedulers |
| LLM Observer | Observability | `road_safety/services/llm_obs.py` | Per-call token/latency/cost tracking |
| Audit Logger | Compliance | `road_safety/compliance/audit.py` | Sensitive access audit trail |
| Retention Sweeper | Compliance | `road_safety/compliance/retention.py` | Automatic data expiry |
| Road Registry | Road | `road_safety/services/registry.py` | Vehicle/driver state, safety scoring |
| Agent Executor | AI Agents | `road_safety/services/agents.py` | Tool-calling agent orchestration |
| Eval Harness | Testing | `tools/eval_detect.py` | Precision/recall evaluation |
| Batch Analyzer | Utility | `tools/analyze.py` | Offline batch analysis |
| Dashboard | UI | `frontend/src/*` + `static/*` fallback | Operator dashboard (SSE-powered) |

### File Structure

```
road-safety/
├── pyproject.toml            ← Project metadata + dependencies
├── Dockerfile                ← Production container
├── docker-compose.yml        ← Compose stack
├── Makefile                  ← Dev workflow shortcuts
├── start.py                  ← One-command launcher
├── .env.example              ← Environment variable template
├── road_safety/              ← Installable Python package
│   ├── __init__.py
│   ├── config.py             ← Centralized configuration
│   ├── server.py             ← Main FastAPI orchestrator
│   ├── logging.py            ← Structured logging config
│   ├── core/
│   │   ├── stream.py         ← Video stream reader
│   │   ├── detection.py      ← YOLOv8 + ByteTrack
│   │   ├── egomotion.py      ← Optical flow ego-motion
│   │   ├── context.py        ← Scene-adaptive thresholds
│   │   └── quality.py        ← Perception quality monitor
│   ├── services/
│   │   ├── llm.py            ← LLM narration/enrichment/chat
│   │   ├── llm_obs.py        ← LLM cost/latency observability
│   │   ├── agents.py         ← AI coaching/investigation/report
│   │   ├── registry.py       ← Multi-vehicle registry + scoring
│   │   ├── drift.py          ← Drift monitor + active learning
│   │   ├── redact.py         ← PII redaction (blur + hash)
│   │   ├── digest.py         ← Hourly/daily digest schedulers
│   │   └── test_runner.py    ← Background test runner
│   ├── api/
│   │   └── feedback.py       ← Operator feedback API
│   ├── integrations/
│   │   ├── slack.py          ← Tiered Slack alerting
│   │   └── edge_publisher.py ← HMAC-signed edge→cloud delivery
│   └── compliance/
│       ├── audit.py          ← Compliance audit trail
│       └── retention.py      ← GDPR data retention sweeps
├── cloud/
│   └── receiver.py           ← Cloud ingest with dedup
├── tools/
│   ├── analyze.py            ← Offline batch analysis
│   ├── eval_detect.py        ← Detection evaluation harness
│   └── eval_enrich.py        ← LLM enrichment evaluation
├── tests/                    ← pytest suite (135 tests)
├── frontend/                 ← React/Vite dashboard source
├── static/
│   ├── index.html            ← Fallback operator dashboard UI
│   └── admin.html            ← Fallback admin video + detection UI
├── data/
│   ├── corpus/               ← RAG knowledge base (markdown)
│   └── test_suite/           ← Evaluation test clips + manifests
└── docs/
    ├── architecture.md
    ├── challenges.md
    └── requirements/
```

---

## Configuration Values

| Setting | Value | Owner | Env Variable | Default |
|---|---|---|---|---|
| YOLO model variant | YOLOv8n | Detection | — | Hardcoded `yolov8n.pt` |
| Processing frame rate | 2 fps | Detection | `ROAD_TARGET_FPS` | 2.0 |
| Episode idle timeout | 1.5 seconds | Server | — | Hardcoded |
| Pair cooldown | 8 seconds | Server | `ROAD_PAIR_COOLDOWN_SEC` | 8.0 |
| LLM rate limit | 3 req/min | LLM | — | Token bucket in `llm.py` |
| Circuit breaker threshold | 3 failures | LLM | — | Hardcoded in `llm.py` |
| Circuit breaker recovery | 60 seconds | LLM | — | Hardcoded in `llm.py` |
| Max recent events | 500 | Server | `ROAD_MAX_EVENTS` | 500 |
| TTC threshold (high, urban) | 1.5 seconds | Context | — | Hardcoded in `context.py` |
| TTC threshold (high, highway) | 2.8 seconds | Context | — | Hardcoded in `context.py` |
| TTC threshold (high, parking) | 0.8 seconds | Context | — | Hardcoded in `context.py` |
| Agent max iterations | 5 | Agents | — | Hardcoded in `agents.py` |
| Retention: thumbnails | 30 days | Retention | `ROAD_RETENTION_THUMBNAILS_DAYS` | 30 |
| Retention: feedback | 90 days | Retention | `ROAD_RETENTION_FEEDBACK_DAYS` | 90 |
| Retention: active learning | 60 days | Retention | `ROAD_RETENTION_AL_PENDING_DAYS` | 60 |
| Retention: outbound queue | 7 days | Retention | `ROAD_RETENTION_OUTBOUND_DAYS` | 7 |
| Retention sweep interval | 3600 seconds | Retention | `ROAD_RETENTION_INTERVAL_SEC` | 3600 |
| Vehicle ID | — | Road | `ROAD_VEHICLE_ID` | `""` |
| Road ID | — | Road | `ROAD_ID` | `""` |
| Driver ID | — | Road | `ROAD_DRIVER_ID` | `""` |
| DSAR token | — | Privacy | `ROAD_DSAR_TOKEN` | None (access denied) |
| Public thumbnail token gate | Off by default | Privacy | `ROAD_PUBLIC_THUMBS_REQUIRE_TOKEN` | 0 |
| Public thumbnail signing secret | Inherits cloud HMAC secret when unset | Privacy | `ROAD_THUMB_SIGNING_SECRET` | `ROAD_CLOUD_HMAC_SECRET` |
| Admin bearer token | — | Security | `ROAD_ADMIN_TOKEN` | None (protected endpoints disabled) |
| Cloud read bearer token | — | Security | `ROAD_CLOUD_READ_TOKEN` | None (cloud reads disabled) |
| Slack image relay | Off by default | Alerting | `SLACK_ENABLE_IMAGE_RELAY` | 0 |
| Plate salt | — | Privacy | `ROAD_PLATE_SALT` | Random per process if unset |
| ALPR policy mode | Off by default | LLM | `ROAD_ALPR_MODE` | `off` |
| Score decay scheduler interval | 3600 seconds | Road | `ROAD_SCORE_DECAY_INTERVAL_SEC` | 3600 |
| HMAC secret | — | Security | `ROAD_CLOUD_HMAC_SECRET` | None (publishing disabled) |
| Cloud endpoint | — | Transport | `ROAD_CLOUD_ENDPOINT` | None (publishing disabled) |
| Slack webhook | — | Alerting | `SLACK_WEBHOOK_URL` | None (alerts disabled) |

---

## Phase 1 — Core Detection Pipeline

### Prerequisites
- Python 3.10+ installed
- Dependencies from `pyproject.toml` installed (`pip install -e ".[dev]"`)
- YOLOv8n weights auto-downloaded by Ultralytics on first run

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `stream.py` | CREATE | Multi-protocol video stream reader with frame queue | §6.3 step 1 |
| 2 | `detection.py` | CREATE | YOLOv8n inference + ByteTrack multi-object tracking | §6.3 step 2 |
| 3 | `egomotion.py` | CREATE | Sparse optical flow for camera-motion estimation | §6.3 step 3 |
| 4 | `context.py` | CREATE | Scene classification + risk threshold engine | §6.3 step 4-5 |
| 5 | `quality.py` | CREATE | Perception quality monitor (luminance, sharpness, confidence) | §6.3 step 5 |
| 6 | `server.py` | CREATE | FastAPI orchestrator: episode management, event emission, SSE, REST APIs | §6.3 steps 6-10 |
| 7 | `frontend/src/*` + `static/index.html` fallback | CREATE | Operator dashboard with SSE event stream | §11.1 |
| 8 | `pyproject.toml` | CREATE | Python project metadata + dependencies | §3.3 |

### Implementation Details

**stream.py:**
- Thread-based video reader with queue-based frame delivery
- Supports HLS, RTSP, local mp4, and webcam
- Automatic reconnection on stream failure
- FPS throttling to target processing rate

**detection.py:**
- Loads YOLOv8n model (`yolov8n.pt`)
- Runs inference on each frame; filters to relevant classes (person, vehicle, motorcycle, bicycle, bus, truck)
- ByteTrack integration for consistent track IDs across frames
- Returns list of detections with `(track_id, class, bbox, confidence)`

**server.py — Episode Management:**
- Per-pair `(track_A, track_B)` interaction tracking
- Episode opens when TTC threshold is exceeded
- Episode accumulates peak severity across frames
- Episode emits on idle timeout (2s) or pair separation
- 8-second cooldown after emission prevents re-fire

**server.py — Risk Classification:**
- TTC computed from bbox scale growth rate between frames
- Ground-plane distance from focal-length model
- Scene-adaptive thresholds from `context.py`
- Risk bands: high (immediate danger), medium (monitor), low (informational)

### Acceptance Criteria

- [x] Server starts and processes live video stream
- [x] Events are detected and classified with risk levels
- [x] Episode dedup prevents same pair from emitting multiple times
- [x] SSE endpoint streams events to dashboard
- [x] Dashboard displays events with thumbnails and risk badges
- [x] REST endpoints return event data and system status

---

## Phase 2 — LLM Enrichment Layer

### Prerequisites
- Phase 1 complete
- Anthropic API key and/or Azure OpenAI credentials (optional — system degrades gracefully)

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `llm.py` | CREATE | LLM client: narration, vision enrichment, chat with RAG | §6.2 LLM layer |
| 2 | `data/corpus/*.md` | CREATE | RAG knowledge base documents | §9.1 chat endpoint |

### Implementation Details

**llm.py — Narration:**
- System prompt with safety-analyst persona
- Event metadata (type, risk, TTC, distance, scene, quality) as structured input
- Returns one short operator-facing sentence; never sees raw video frames
- Graceful degradation: no API key → templated summary

**llm.py — Vision Enrichment (ALPR):**
- Internal thumbnail may be sent to vision model for license plate reading only when `ROAD_ALPR_MODE=third_party` (default is `off`)
- Self-consistency: two calls at different temperatures (0.0 and 0.3)
- Disagreeing reads → `plate_readable: "partial"` instead of guessing (TRD D-07)
- Circuit breaker: 3 consecutive failures → 60s open (TRD §8.4)

**llm.py — Chat Copilot:**
- RAG: loads markdown corpus from `data/corpus/`
- Semantic matching against user query
- Context window: corpus excerpts + recent events
- Returns natural-language answer

### Acceptance Criteria

- [x] Events are narrated with contextual LLM summaries
- [x] Vision enrichment produces plate hash when readable
- [x] Self-consistency check rejects disagreeing plate readings
- [x] Chat copilot answers questions using road policy corpus
- [x] System operates without LLM keys (templated fallback)

---

## Phase 3 — Privacy & Redaction

### Prerequisites
- Phase 1 complete

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `redact.py` | CREATE | Face blur, plate blur, plate hash, dual thumbnails | §10.2 |

### Implementation Details

**redact.py — Dual Thumbnails:**
- Internal: `{event_id}.jpg` — full resolution, unredacted, local disk only
- Public: `{event_id}_public.jpg` — faces blurred, plates blurred, safe for egress
- Shared event channels (SSE, Slack, cloud) always use public version

**redact.py — PII Protection:**
- Face blur: Gaussian blur on upper 35% of person bounding box (over-blur is correct failure mode)
- Plate blur: Gaussian blur on lower-middle strip (55-95% height, 15% inset) of vehicle bbox
- Plate hash: `hash_plate(text, salt)` → `plate_{sha256[:16]}`; salt from `ROAD_PLATE_SALT` env
- PII scrub in server: `plate_text` and `plate_state` stripped before SSE/Slack/cloud payloads; optional third-party enrichment is a separately governed path

### Acceptance Criteria

- [x] Every event produces both internal and public thumbnails
- [x] Public thumbnails have faces and plates blurred
- [x] Optional signed public-thumbnail mode (`ROAD_PUBLIC_THUMBS_REQUIRE_TOKEN=1`) enforces `exp/token` query params
- [x] Plate text is hashed with deployment-specific salt
- [x] Raw plate text never appears in any egress channel
- [x] Unredacted thumbnails require DSAR token

---

## Phase 4 — Edge/Cloud Architecture

### Prerequisites
- Phase 1 complete
- Phase 3 complete (redacted thumbnails for egress)

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `edge_publisher.py` | CREATE | HMAC-signed batched event delivery | §10.3, §8.5 |
| 2 | `cloud/receiver.py` | CREATE | Cloud ingest with HMAC verification and dedup | §6.1 cloud |

### Implementation Details

**edge_publisher.py:**
- Events queue in append-only JSONL (`data/outbound_queue.jsonl`)
- Background task reads queue, batches up to 20 events
- Each batch HMAC-SHA256 signed with shared secret
- POST to cloud endpoint with `Signature` header
- Exponential backoff on failure (1s → 2s → 4s → 8s → 16s → 32s max)
- Survives network outages — queue drains on reconnect

**cloud/receiver.py:**
- Separate FastAPI app on port 8001
- Verifies HMAC signature on each batch
- Deduplicates on `event_id` (set-based)
- Persists events to SQLite
- Read endpoints (`/events`, `/stats`) require `ROAD_CLOUD_READ_TOKEN`
- Returns 200 on success, 401 on signature mismatch

### Acceptance Criteria

- [x] Events are batched and HMAC-signed for delivery
- [x] Cloud receiver verifies signatures and rejects invalid batches
- [x] Duplicate event_ids are handled idempotently
- [x] Edge queue survives network outages and drains on reconnect
- [x] Only public (redacted) thumbnails are transmitted

---

## Phase 5 — Feedback & Drift Monitoring

### Prerequisites
- Phase 1 complete

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `road_safety/api/feedback.py` | CREATE | Operator feedback API (tp/fp verdicts) | §9.1 feedback |
| 2 | `drift.py` | CREATE | Rolling precision, trend detection, active learning sampler | §13.2, §14 |

### Implementation Details

**road_safety/api/feedback.py:**
- POST `/api/feedback` — accepts `{event_id, verdict, note}`
- Appends to `data/feedback.jsonl`
- Updates drift monitor with new verdict

**drift.py — DriftMonitor:**
- Joins feedback verdicts with emitted events
- Computes rolling-window precision, sliced by risk level and event type
- Compares current vs prior window: "improving", "stable", "degrading" (±5% noise band)
- Minimum bucket size: 3 labels (avoids noisy 1/1 = 100% precision)

**drift.py — ActiveLearningSampler:**
- Decision boundary: confidence ∈ [0.35, 0.50] → sampled at 50% probability
- Disputed samples: verdict=fp → always captured
- Samples saved as JSON files in `data/active_learning/pending/`
- Export endpoint bundles pending samples into zip with manifest

### Acceptance Criteria

- [x] Operators can submit tp/fp verdicts on events
- [x] Drift monitor computes per-type and per-risk precision
- [x] Trend detection identifies degradation with noise-band guard
- [x] Active learning selects decision-boundary and disputed samples
- [x] Export produces Label Studio/CVAT-compatible zip

---

## Phase 6 — Alerting & Digest

### Prerequisites
- Phase 1 complete
- Phase 2 complete (narrations for digest content)

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `road_safety/integrations/slack.py` | CREATE | Tiered Slack alerting | §13.3 |
| 2 | `digest.py` | CREATE | Hourly/daily summary schedulers | §13.3 |

### Implementation Details

**road_safety/integrations/slack.py:**
- Tiered delivery:
  - High risk: instant Slack notification
  - Medium risk: batched in hourly digest
  - Low risk: batched in daily digest
- Rich message formatting with event details and risk badge
- Text-only by default; optional screenshot relay requires `SLACK_ENABLE_IMAGE_RELAY=1`
- Graceful degradation: no webhook URL → alerts disabled silently

**digest.py:**
- Background schedulers for hourly and daily digests
- Aggregates events by type and risk
- Sends single Slack message with summary table

### Acceptance Criteria

- [x] High-risk events trigger instant Slack notification
- [x] Hourly digest summarizes medium-risk events
- [x] Daily digest summarizes all events
- [x] No Slack webhook → alerts disabled silently

---

## Phase 7 — LLM Observability & Resilience

### Prerequisites
- Phase 2 complete

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `llm_obs.py` | CREATE | LLM call instrumentation (tokens, latency, cost, errors) | §13.1, §13.2 |
| 2 | `llm.py` | EXTEND | Multi-provider failover, token bucket, circuit breaker | §8.4, D-06 |

### Implementation Details

**llm_obs.py — LLMObserver:**
- Ring buffer of 5,000 `LLMRecord` entries
- Each record: `call_type`, `model`, `input_tokens`, `output_tokens`, `latency_ms`, `success`, `error`, `skip_reason`, `event_id`
- `stats(window_sec)` → total calls, success/error/skip counts, P50/P95 latency, estimated USD cost, error rate
- `recent(limit)` → last N records as dicts
- Exposed via `/api/llm/stats` and `/api/llm/recent` behind `ROAD_ADMIN_TOKEN`

**llm.py — Multi-Provider Failover (TRD D-06):**
- Primary provider: Anthropic (or Azure if configured)
- On primary failure: automatic retry through secondary provider
- Zero operator intervention required
- Both providers instrumented through `llm_obs.py`

**llm.py — Rate Budget:**
- Token bucket: 3 req/min sustained, burst of 3
- Calls refused *before* hitting API when bucket empty
- Prevents 429 errors from provider

**llm.py — Circuit Breaker:**
- Tracks consecutive failures for vision enrichment
- 3 failures → breaker opens for 60 seconds
- During open state: enrichment calls skipped (returns None)
- Halves API load during rate-limit storms

### Acceptance Criteria

- [x] Every LLM call is instrumented with token/latency/cost tracking
- [x] Stats endpoint returns P50/P95 latency, error rate, cost
- [x] Primary→secondary failover works when primary fails
- [x] Rate bucket refuses calls before API returns 429
- [x] Circuit breaker opens after 3 failures, recovers after 60s

---

## Phase 8 — Compliance & Audit

### Prerequisites
- Phase 3 complete (privacy layer exists)
- Phase 5 complete (feedback exists for retention)

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `audit.py` | CREATE | Compliance audit trail | §7.4, §10.1 |
| 2 | `retention.py` | CREATE | Automatic data retention sweeps | §10.2 |
| 3 | `server.py` | EXTEND | Audit logging in sensitive endpoints | §10.1 |

### Implementation Details

**audit.py:**
- `log(action, resource, *, actor, outcome, detail, ip)` → appends to JSONL
- `tail(n)` → last N records
- `stats()` → action counts
- Every sensitive operation is audit-logged:
  - Unredacted thumbnail access (success + denial)
  - Feedback submission
  - Active learning export
  - Chat queries
  - Agent invocations
- Sensitive operational endpoints are bearer-protected via `ROAD_ADMIN_TOKEN`

**retention.py:**
- `sweep_thumbnails(max_age_days)` → deletes old thumbnail files
- `sweep_al_pending(max_age_days)` → deletes old active learning samples
- `sweep_feedback(max_age_days)` → trims old feedback entries from JSONL
- `sweep_outbound(max_age_days)` → trims old outbound queue entries
- `retention_loop()` → async background task, runs every `ROAD_RETENTION_INTERVAL_SEC`
- `run_sweep()` → manual trigger returning counts per category
- Defaults: thumbnails 30d, feedback 90d, AL 60d, queue 7d (TRD §10.2)

### Acceptance Criteria

- [x] All sensitive data access is audit-logged with timestamp, actor, outcome
- [x] Audit endpoint returns recent records and action counts
- [x] Retention sweep deletes expired artifacts automatically
- [x] Retention intervals are configurable via environment variables
- [x] Manual sweep trigger available via API

---

## Phase 9 — Road Readiness

### Prerequisites
- Phase 1 complete

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `road_safety/services/registry.py` | CREATE | Multi-vehicle registry, driver scoring, road aggregation | §7.5 |
| 2 | `server.py` | EXTEND | Road identity on events; road API endpoints | §9.1 road |

### Implementation Details

**road_safety/services/registry.py — RoadRegistry:**
- In-memory dict of `VehicleState` keyed by `vehicle_id`
- `record_event(event)` → increments counters, applies score penalty
- `record_feedback(event_id, verdict, vehicle_id)` → updates tp/fp counts for the matched vehicle
- `decay_scores()` → 0.5 points/hour recovery toward 100
- `road_summary()` → aggregate stats, risk breakdown, worst vehicle
- `driver_leaderboard(limit)` → drivers sorted by score ascending

**server.py — Road Identity:**
- Every emitted event carries `vehicle_id`, `road_id`, `driver_id` from env
- `_emit_event()` calls `road_registry.record_event(event)`
- `_on_feedback()` calls `road_registry.record_feedback(event_id, verdict, vehicle_id)` using the matched event's vehicle context

**Driver Safety Scoring (TRD D-10):**
- Base score: 100 (max)
- Penalties: high=-10, medium=-3, low=-1
- Recovery: +0.5/hour decay toward 100
- Clamped to [0, 100]

### Acceptance Criteria

- [x] Every event includes vehicle_id, road_id, driver_id
- [x] Road registry tracks per-vehicle event counts and safety scores
- [x] Driver scoring applies decaying penalty model
- [x] Road summary API returns aggregate stats
- [x] Driver leaderboard ranks by score ascending (worst first)

---

## Phase 10 — AI Agent Orchestration

### Prerequisites
- Phase 2 complete (LLM client available)
- Phase 5 complete (drift data for investigation)
- Phase 9 complete (road context for agents)

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `agents.py` | CREATE | Tool-calling agent executor + three specialized agents | §8.4, D-08, D-09 |
| 2 | `server.py` | EXTEND | Agent API endpoints | §9.1 agents |

### Implementation Details

**agents.py — Tool Registry:**

| Tool | Used By | Parameters | Returns |
|---|---|---|---|
| `get_event` | Coaching, Investigation | `event_id: str` | Event dict or null |
| `get_recent_events` | Investigation, Report | `limit: int` | List of events |
| `get_policy` | Coaching | — | Road policy markdown |
| `get_feedback` | Investigation, Report | — | Feedback stats dict |
| `get_drift_report` | Investigation, Report | — | Drift report dict |
| `count_by_type` | Report | — | Event counts by type |
| `count_by_risk` | Report | — | Event counts by risk |

**Agent Tool Assignments (TRD D-08):**

| Agent | Purpose | Tools | Max Tools |
|---|---|---|---|
| Coaching | Generate coaching note for specific event | `get_event`, `get_policy`, `get_recent_events` | 3 |
| Investigation | Root-cause analysis of specific event | `get_event`, `get_recent_events`, `get_feedback`, `get_drift_report`, `get_policy` | 5 |
| Report | Session-wide safety summary | `get_recent_events`, `get_feedback`, `get_drift_report`, `count_by_type`, `count_by_risk` | 5 |

**agents.py — AgentExecutor:**
- Tool-calling loop: send system prompt + tools → LLM responds with tool calls or final answer
- Each iteration: execute tool calls, append results, re-query LLM
- Hard stop at 5 iterations (TRD D-09) — returns partial result
- Structured JSON output: each agent's system prompt specifies exact output schema

**Output Schemas:**

Coaching:
```json
{
  "event_summary": "...",
  "risk_explanation": "...",
  "coaching_recommendation": "...",
  "policy_reference": "..."
}
```

Investigation:
```json
{
  "event_summary": "...",
  "pattern_analysis": "...",
  "root_cause_hypothesis": "...",
  "confidence": "high|medium|low",
  "recommended_actions": ["..."]
}
```

Report:
```json
{
  "period_summary": "...",
  "top_issues": [{"type": "...", "count": 0, "trend": "..."}],
  "precision_assessment": "...",
  "recommendations": ["..."]
}
```

### Acceptance Criteria

- [x] Coaching agent produces structured coaching note given event_id
- [x] Investigation agent correlates events with history and drift data
- [x] Report agent summarizes session-wide safety activity
- [x] All agents respect 5-iteration hard stop
- [x] Tool catalogs are bounded (≤5 tools per agent)
- [x] Agent invocations are audit-logged

---

## Phase 11 — Evaluation Harness

### Prerequisites
- Phase 1 complete

### Deliverables

| # | File | Action | Description | TRD Source |
|---|---|---|---|---|
| 1 | `tools/eval_detect.py` | CREATE | Precision/recall evaluation, suite runner, comparison | §14 |
| 2 | `eval_enrich.py` | CREATE | Evaluation enrichment utilities | §14 |
| 3 | `data/test_suite/` | CREATE | Test clip manifests and ground truth | §14.5 |

### Implementation Details

**tools/eval_detect.py — Modes:**

| Mode | Command | Output |
|---|---|---|
| Single clip | `python tools/eval_detect.py` | P/R/F1 per risk + per type |
| Suite | `python tools/eval_detect.py --suite` | Markdown report, regression flag (>3% drop) |
| Compare | `python tools/eval_detect.py --compare run_a.json run_b.json` | Delta table with highlighted regressions |

**tools/eval_detect.py — Metrics:**
- Precision = TP / (TP + FP)
- Recall = TP / (TP + FN)
- F1 = 2 * P * R / (P + R)
- Sliced by: risk_level, event_type, scene_type
- Regression threshold: >3% F1 drop from baseline

### Acceptance Criteria

- [x] Single-clip evaluation produces correct P/R/F1
- [x] Suite runner processes multiple clips and generates markdown report
- [x] Comparison mode highlights regressions exceeding 3% threshold
- [x] Results are saved as JSON for historical tracking

---

## Cross-Phase Rules

### Error Handling

- All modules use Python exceptions with descriptive messages
- FastAPI endpoints return structured JSON errors with appropriate HTTP status codes
- No silent failures — errors are logged with `[module]` prefix
- LLM failures are caught and return None (not raised as exceptions)

### Logging Convention

All modules follow the pattern:
```python
print(f"[module_name] descriptive message")
```

### Configuration

- All secrets via environment variables
- All tuning parameters have sane defaults
- `.env.example` documents all available variables with descriptions
- Missing optional env vars → feature disabled silently

### Security

- No PII in any log output or error message
- All external communications signed (HMAC) or over TLS
- Image content in LLM calls marked as UNTRUSTED USER DATA

---

## Deployment Checklist

### Pre-Deployment

- [x] All phases complete (status table shows SHIPPED)
- [x] Python syntax valid for all modules (verified via `ast.parse`)
- [x] No missing imports or circular dependencies
- [x] `.env.example` documents all environment variables
- [x] `pyproject.toml` includes all dependencies with version ranges
- [x] PII isolation verified (redact.py blur, plate hash, DSAR gating)
- [x] Audit trail covers all sensitive operations
- [x] Retention sweep deletes expired data
- [x] Episode dedup prevents duplicate emissions
- [x] Agent tool catalogs bounded at ≤5 tools each
- [x] Agent iteration hard stop at 5 steps
- [x] Multi-provider LLM failover tested
- [x] Circuit breaker and rate budget tested

### Deployment

```bash
# 1. Clone and install
git clone <repo>
cd road-safety
pip install -e ".[dev]"

# 2. Configure environment
cp .env.example .env
# Edit .env with API keys and settings

# 3. Start edge node
uvicorn road_safety.server:app --host 0.0.0.0 --port 8000

# 4. (Optional) Start cloud receiver
uvicorn cloud.receiver:app --host 0.0.0.0 --port 8001

# 5. Open dashboard
# Navigate to http://localhost:8000
```

### Post-Deployment

- [ ] Verify events are being detected from live stream
- [ ] Verify LLM narration is working (if API keys configured)
- [ ] Verify Slack alerts are firing (if webhook configured)
- [ ] Verify drift monitor initializes cleanly
- [ ] Monitor `/api/llm/stats` for error rate with `Authorization: Bearer <ROAD_ADMIN_TOKEN>`
- [ ] Submit test feedback and verify drift update
- [ ] Test agent endpoints with known event_id

---

## Known Gaps & Open Questions

| # | Area | Issue | Severity | Blocks Phase | Status |
|---|---|---|---|---|---|
| 1 | Persistence | In-memory storage resets on restart | Medium | None | Accepted for v1.0 |
| 2 | Auth | No centralized RBAC or user identity layer; sensitive ops use shared bearer tokens | Medium | None | Accepted for v1.0 |
| 3 | GPU | CPU-only limits to ~2 fps | Low | None | Future optimization |
| 4 | Multi-tenant | No tenant isolation | Low | None | Out of scope for v1.0 |
| 5 | Structured logging | Using print statements instead of structured logger | Low | None | Future improvement |

---

## Key Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | JSONL over database for audit/feedback/queue | Append-only, no external dependency; meets v1.0 throughput envelope; cleanly upgradable to SQLite/PostgreSQL when scale demands |
| 2 | In-memory road registry with periodic snapshot | Operational state reconstructible from event stream; avoids hard dependency on Redis/DB at current scale |
| 3 | YOLOv8n (nano) for detection model | Must run on CPU at 2 fps; smallest variant |
| 4 | LLM on metadata only (no video frames in prompt) | Cost: $0.005/frame vs $0.001/event; privacy: no video in LLM context |
| 5 | Edge-first architecture | Shared event path excludes raw plate text and unredacted thumbnails; optional external enrichment is governed separately |
| 6 | Three bounded agents (≤5 tools each) | Prevents tool overload (68% hallucination rate with >10 tools) |
| 7 | Dual-thumbnail architecture | Keeps shared channels redacted while preserving a local-only internal review copy |
| 8 | Salted SHA-256 for plate text | Cross-event correlation without storing raw PII |
| 9 | Self-consistency for ALPR (2 calls, different temps) | Eliminates 15-20% hallucinated plate readings |
| 10 | Decaying penalty model for driver scoring | Balances accountability with recovery over time |
