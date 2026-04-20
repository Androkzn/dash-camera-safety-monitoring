# Road Safety AI Platform — Technical Requirements Document (TRD)

**Feature:** Road Safety — Live Event Detection + LLM Copilot
**Product:** Road Safety AI Platform (Python / FastAPI)
**Document Type:** Technical Requirements Document (TRD)
**Version:** v1.1
**Status:** APPROVED
**Created:** 2026-04-15
**Last Updated:** 2026-04-15
**Author(s):** Andrei Tekhtelev
**Reviewers:** Architecture, ML/AI, Compliance, DevOps

**Source Documents:**
- **BRD:** `docs/requirements/Road_Safety_BRD.md` (v1.1)
- **Challenges:** `docs/challenges.md`
- **Architecture:** `docs/architecture.md`
- **Related:** `README.md`, `.env.example`

---

## 0. Authoring Control Tables

### 0.1 Version History

| Version | Date | Author | Changes |
|---|---|---|---|
| v0.1 | 2026-04-01 | A. Tekhtelev | Initial TRD — core detection loop |
| v0.5 | 2026-04-08 | A. Tekhtelev | Added edge/cloud, drift, privacy, road |
| v1.0 | 2026-04-15 | A. Tekhtelev | Added agents, LLM observability, retention, audit |
| v1.1 | 2026-04-15 | A. Tekhtelev | Documentation sync: thumbnail signed-access option, ALPR policy gate, auth matrix alignment |

### 0.2 BRD Requirement Inventory

| BRD Req ID | BRD Section | Requirement Summary | In Scope | Phase | Source Anchor |
|---|---|---|---|---|---|
| BR-01 | FR-14 | Real-time pedestrian-vehicle and vehicle-vehicle detection from live video | Y | P1 | FR-14 |
| BR-02 | FR-15 | TTC and distance-based risk classification with scene-adaptive thresholds | Y | P1 | FR-15 |
| BR-03 | FR-01 | Live operator feed with thumbnails, risk badges, and narrations | Y | P1 | FR-01 |
| BR-04 | FR-16 | PII-redacted thumbnails for shared event channels; optional external enrichment governed separately | Y | P1 | FR-16 |
| BR-05 | FR-03 | Operator feedback (tp/fp) on events | Y | P1 | FR-03 |
| BR-06 | FR-04 | Tiered Slack alerts (high=instant, medium=hourly, low=daily) | Y | P1 | FR-04 |
| BR-07 | FR-02 | Chat copilot for event/policy Q&A using RAG | Y | P1 | FR-02 |
| BR-08 | FR-17 | Edge→cloud event delivery with HMAC-signed batches | Y | P1 | FR-17 |
| BR-09 | FR-11 | Active learning export for labeling tools | Y | P2 | FR-11 |
| BR-10 | FR-12 | Drift monitoring with rolling precision and trend detection | Y | P2 | FR-12 |
| BR-11 | FR-13 | LLM cost, latency, and error rate observability | Y | P2 | FR-13 |
| BR-12 | FR-09 | Audit trail for sensitive data access | Y | P2 | FR-09 |
| BR-13 | FR-10 | Automatic data retention with configurable expiry | Y | P2 | FR-10 |
| BR-14 | FR-05 | Road-wide safety summary with per-vehicle scores | Y | P2 | FR-05 |
| BR-15 | FR-06 | AI coaching agent — structured coaching note per event | Y | P3 | FR-06 |
| BR-16 | FR-07 | AI investigation agent — root-cause analysis | Y | P3 | FR-07 |
| BR-17 | FR-08 | AI report agent — session-wide safety summary | Y | P3 | FR-08 |

### 0.3 Coverage Checksum

| Check | Formula | Expected | Actual | Pass |
|---|---|---|---|---|
| In-scope inventory count | Unique IDs in §0.2 where In Scope = Y | 17 | 17 | [x] |
| Traceability count | Unique IDs in §18 | 17 | 17 | [x] |
| No orphan IDs in §18 | Every ID in §18 exists in §0.2 | true | true | [x] |
| No duplicate IDs | IDs are unique across §0.2 and §18 | true | true | [x] |
| No unmapped in-scope requirements | Every in-scope ID has a row in §18 | true | true | [x] |

---

## 1. Executive Summary

### 1.1 Problem Statement

Road safety cameras generate massive video volumes that operators cannot review manually. Existing automated detection systems suffer from five production failure modes: false positive floods, LLM unreliability, privacy liability, silent model drift, and inability to scale beyond single-camera deployments. This platform addresses all five simultaneously with a modular, open architecture.

### 1.2 Proposed Solution

- **Edge-first detection** using YOLOv8n + ByteTrack for real-time object tracking
- **Physical-unit risk classification** using time-to-collision and ground-plane distance with scene-adaptive thresholds
- **LLM enrichment layer** with multi-provider failover, rate budgeting, circuit breakers, and policy-gated external ALPR
- **Privacy-by-design** architecture: dual thumbnails, optional signed public-thumbnail access, plate hashing, DSAR gating, audit trail, auto-retention
- **Feedback-driven improvement** via drift monitoring, active learning, and operator verdicts
- **AI agent orchestration** with bounded tools, structured output, and hard iteration limits

### 1.3 Outcome

After ship:
- Operators see contextually-enriched, deduplicated safety events — not raw detection spam
- Shared event channels never carry raw plate text or unredacted thumbnails; any optional third-party enrichment path must be explicitly governed
- LLM failures are invisible to operators (graceful degradation)
- Model precision is continuously tracked with automated data curation for retraining
- Road managers have vehicle-level and driver-level safety dashboards
- AI agents provide on-demand coaching, investigation, and reporting

---

## 2. Scope

### 2.1 In Scope

- Real-time video stream ingestion (HLS, RTSP, mp4, webcam)
- Object detection (persons, vehicles, motorcycles, bicycles, buses, trucks)
- Multi-object tracking with ByteTrack
- Time-to-collision and distance estimation from monocular camera
- Scene-adaptive risk threshold engine
- Ego-motion estimation via optical flow
- Perception quality monitoring (luminance, sharpness, confidence)
- LLM narration and vision enrichment with multi-provider failover
- RAG-based chat copilot with corpus ingestion
- PII redaction (face blur, plate blur, plate text hashing)
- Edge→cloud event delivery with HMAC signing
- Operator feedback loop (tp/fp verdicts)
- Drift monitoring with rolling precision and trend detection
- Active learning pipeline (decision boundary + disputed samples)
- Tiered Slack alerting (per risk level)
- AI agents (coaching, investigation, report)
- Multi-vehicle road registry with driver scoring
- LLM cost/latency/error observability
- Compliance audit trail
- Configurable data retention

### 2.2 Out of Scope

- Driver-facing camera / driver monitoring system (DMS)
- GPS / telematics integration
- Multi-tenant authentication / authorization
- Persistent database (current: in-memory + JSONL)
- Mobile application
- Custom model training pipeline
- Video recording / archival
- Multi-language support for UI

### 2.3 Success Criteria

| Criterion | Target | Measurement |
|---|---|---|
| Detection precision | ≥ 70% | Rolling precision via drift monitor |
| High-risk recall | ≥ 80% | Eval harness with labeled test clips |
| LLM enrichment availability | > 90% uptime | `llm_obs.py` success rate |
| PII leakage | Zero | Code-path structural audit |
| Bandwidth reduction vs raw video | ≥ 2,000x | JSON event size vs H.264 stream bitrate |
| Agent response time (P95) | < 10 seconds | LLM observer latency metric |

---

## 3. Goals, Non-Goals, and Constraints

### 3.1 Technical Goals

- TG-01: Sub-2-second detection-to-event latency at 2 fps
- TG-02: Zero-GPU inference capability (YOLOv8n on CPU)
- TG-03: Fault-tolerant LLM layer (no detection downtime from LLM failures)
- TG-04: Structural PII isolation (redacted outputs are the only egress path)
- TG-05: Feedback-driven precision tracking without manual pipeline work
- TG-06: Road-ready data model from day one

### 3.2 Non-Goals

- NG-01: Production multi-tenant SaaS deployment
- NG-02: Custom model training/fine-tuning
- NG-03: Real-time video transcoding or recording
- NG-04: Mobile or native desktop clients
- NG-05: Multi-GPU inference optimization

### 3.3 Constraints

- **Platform:** Python 3.10+; no GPU required; macOS/Linux
- **Dependencies:** Ultralytics (YOLOv8), FastAPI, Uvicorn, OpenCV, PyTorch, Anthropic SDK, Azure OpenAI SDK, httpx
- **LLM Budget:** 3 req/min sustained rate limit (token bucket)
- **Privacy:** GDPR Art. 4/5/30, CCPA PI definitions
- **Latency:** 2s detection-to-emission target at 2 fps processing
- **Compatibility:** All event payloads are JSON; no binary formats in APIs

### 3.4 Shared Platform Standards Compliance

| Standard | Canonical Source | Contract for This Feature | Deviation |
|---|---|---|---|
| Event schema | `server.py` event emission | All events include `event_id`, `vehicle_id`, `road_id`, `driver_id`, `event_type`, `risk_level`, `confidence`, `timestamp` | None |
| Error handling | FastAPI `HTTPException` | All REST endpoints return structured JSON errors with HTTP status codes | None |
| Logging | Python `print` statements | All modules use descriptive `[module]` prefix in console output | Future: migrate to structured logging |
| Configuration | Environment variables | All secrets and tuning parameters via env vars; `.env.example` documents all | None |
| Data formats | JSON / JSONL | Events: JSON objects; Feedback: JSONL; Audit: JSONL; Queue: JSONL | None |

---

## 4. Assumptions and Dependencies

### 4.1 Assumptions

- A-01: Camera provides a continuous stream at ≥ 2 fps
- A-02: Network connectivity is intermittent but available (edge→cloud queue handles outages)
- A-03: LLM API keys are optional; system runs without them
- A-04: Operators provide feedback within 24 hours of event emission (for drift accuracy)
- A-05: Single vehicle per edge node; road aggregation at cloud layer

### 4.2 Dependencies

| Dependency | Owner | Type | Risk | Status |
|---|---|---|---|---|
| YOLOv8 pretrained weights | Ultralytics | Upstream | Low | Available (pip) |
| Anthropic API | Anthropic | Upstream | Medium | Rate-limited tier |
| Azure OpenAI API | Microsoft | Upstream | Medium | Failover provider |
| Slack Webhook | Slack | Downstream | Low | Alert delivery |
| Label Studio / CVAT | ML team | Downstream | Low | Active learning export |

---

## 5. Current State (As-Is)

### 5.1 Existing Architecture Summary

The platform runs as a single-process FastAPI application with a background thread for video stream processing:

```
Stream → Detection → Tracking → Risk Classification → Event Emission → SSE/Slack/Edge Publish
                                                          ↓
                                                   LLM Narration/Enrichment
                                                          ↓
                                                   Operator Dashboard
```

### 5.2 Current Limitations

| Limitation | Impact | Planned Mitigation |
|---|---|---|
| In-memory event storage | Events lost on restart | Future: SQLite or PostgreSQL persistence |
| Single-process architecture | Cannot scale horizontally | Future: worker-based processing with message queue |
| No authentication | All endpoints are public | Future: JWT/OAuth2 middleware |
| No persistent database | Road registry resets on restart | Future: Redis or PostgreSQL for road state |
| CPU-only inference | Limited to ~2 fps | Future: GPU/TensorRT for real-time rates |

### 5.3 Relevant Existing Files/Modules

- **Detection:** `road_safety/core/detection.py` (YOLOv8 + ByteTrack)
- **Stream:** `road_safety/core/stream.py` (multi-protocol video reader)
- **Server:** `road_safety/server.py` (FastAPI orchestrator)
- **LLM:** `road_safety/services/llm.py` (Anthropic + Azure with failover)
- **Privacy:** `road_safety/services/redact.py` (face/plate blur, plate hash)
- **Drift:** `road_safety/services/drift.py` (precision monitor + active learning)
- **Agents:** `road_safety/services/agents.py` (coaching, investigation, report)
- **Road:** `road_safety/services/registry.py` (vehicle registry + driver scoring)

---

## 6. Target Architecture (To-Be)

### 6.1 Architecture Overview

```
┌──────────────────────────── Edge Node (per vehicle) ────────────────────────────┐
│  stream.py → detection.py → server.py → edge_publisher.py → Cloud              │
│     │            │              │                                               │
│     │        egomotion.py   context.py                                          │
│     │        quality.py     drift.py                                            │
│     │                       redact.py                                           │
│     │                       llm.py ──→ Anthropic / Azure (failover)             │
│     │                       agents.py                                           │
│     │                       registry.py                                        │
│     │                       llm_obs.py                                          │
│     │                       audit.py                                            │
│     │                       retention.py                                        │
│     │                                                                           │
│     └──→ static/index.html (SSE dashboard)                                     │
└─────────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼ HMAC-signed HTTPS
┌──────────────── Cloud Receiver ────────────────────┐
│  cloud/receiver.py (port 8001)                      │
│  - HMAC verification                               │
│  - event_id dedup                                   │
│  - SQLite persistence                               │
└────────────────────────────────────────────────────┘
```

### 6.2 Ownership Boundaries

| Layer | Owns | Must Not Own |
|---|---|---|
| **Detection pipeline** (`detection.py`, `stream.py`, `egomotion.py`) | Object detection, tracking, TTC/distance computation | Risk policy decisions, LLM calls |
| **Risk engine** (`context.py`, `quality.py`) | Scene classification, threshold adaptation, perception quality | Object detection, event emission |
| **Server** (`server.py`) | Event lifecycle (episodes, emission, dedup), API routing | LLM logic, detection logic |
| **LLM layer** (`llm.py`, `agents.py`) | Narration, enrichment, chat, agent orchestration | Detection, risk classification, event emission |
| **Privacy layer** (`redact.py`, `audit.py`, `retention.py`) | PII redaction, audit logging, data expiry | Event semantics, risk classification |
| **Road layer** (`registry.py`) | Vehicle/driver state, scoring, aggregation | Event detection, LLM calls |
| **Observability** (`llm_obs.py`, `drift.py`) | Metrics collection, drift detection | Any mutation of events or state |
| **Edge/Cloud** (`edge_publisher.py`, `cloud/receiver.py`) | Event transport, integrity verification | Event semantics, modification |

### 6.3 Data Flow

1. **Frame capture:** `stream.py` reads frames from the live video source.
2. **Detection:** `detection.py` runs YOLOv8n inference + ByteTrack.
3. **Quality observation:** `quality.py` updates the perception state machine; degraded states tighten downstream multipliers and may skip vision enrichment.
4. **Ego-motion:** `egomotion.py` runs Farneback optical flow on the masked background to produce an ego flow vector + speed proxy.
5. **Scene context:** `context.py` classifies scene as urban / highway / parking / unknown and emits adaptive TTC + distance thresholds.
6. **Interaction discovery:** `find_interactions` enumerates candidate pairs from class membership and edge-pixel proximity.
7. **Pair gates:** for vehicle-vehicle pairs, the depth-aware proximity gate and convergence-angle filter discard pairs that are not real conflicts.
8. **Ego-relative motion gate:** TTC is discarded when neither track shows a positive approach residual against the ego flow estimate.
9. **Multi-gate TTC:** `estimate_pair_ttc` (preferred) or `estimate_ttc_sec` (fallback) returns a value only when sustained-evidence gates pass (monotonic growth, jitter-floor pixel delta, non-trivial track motion, scale ratio, closing rate).
10. **Distance estimation:** `estimate_inter_distance_m` returns 3D inter-object distance (depth difference + lateral offset).
11. **Risk classification:** `_classify_with_scene` ladders TTC > distance > pixel fallback against the scene-adaptive thresholds, then applies the speed-aware floor.
12. **Episode management:** per-pair episodes accumulate risk-frame counts and the peak-severity frame; on flush, `Episode.final_risk()` downgrades unsupported peaks.
13. **PII redaction:** `redact.py` writes dual thumbnails (internal unredacted + public face-/plate-blurred); plate text is salted-hashed before egress.
14. **LLM enrichment:** `llm.py` narrates the event and optionally runs ALPR only when policy permits (`ROAD_ALPR_MODE=third_party`), and skips on degraded perception or low-risk events.
15. **Event emission:** SSE to dashboard, tier-aware Slack dispatch (high-risk subject to the Slack quality gate; medium / low buffered), edge publish, road-registry update.
16. **Feedback ingestion:** `road_safety/api/feedback.py` receives operator verdicts.
17. **Drift update:** `drift.py` recomputes rolling precision and trend.
18. **Active learning:** `drift.py` selects decision-boundary and disputed events for relabeling.
19. **Agent invocation:** `agents.py` runs the tool-calling loop on operator request.
20. **Audit logging:** `audit.py` records all sensitive operations.
21. **Retention sweep:** `retention.py` expires old data on schedule.

---

## 7. Canonical Domain Model

### 7.1 Entities

| Entity | Purpose | Source of Truth |
|---|---|---|
| SafetyEvent | A single proximity incident between two tracked objects | `server.py` emission (in-memory list) |
| Feedback | Operator verdict on an event | `data/feedback.jsonl` |
| ActiveLearningSample | High-value sample selected for relabeling | `data/active_learning/pending/*.json` |
| AuditRecord | Log entry for sensitive data access | `data/audit.jsonl` |
| VehicleState | Per-vehicle event counters, safety score | `registry.py` (in-memory registry) |
| LLMRecord | Single LLM API call metrics | `llm_obs.py` (in-memory ring buffer) |
| DriftReport | Rolling precision and trend data | `drift.py` (in-memory state) |

### 7.2 Field Contract — SafetyEvent

| Field | Type | Required | Default | Writable By | Notes |
|---|---|---|---|---|---|
| `event_id` | str (UUID-like) | Y | auto-generated | Server only | `evt_{timestamp}_{seq}` |
| `vehicle_id` | str | Y | from env `ROAD_VEHICLE_ID` | Server only | |
| `road_id` | str | Y | from env `ROAD_ID` | Server only | |
| `driver_id` | str | N | from env `ROAD_DRIVER_ID` | Server only | |
| `video_id` | str | Y | `"live_stream"` | Server only | |
| `event_type` | str | Y | — | Detection pipeline | `pedestrian_proximity`, `vehicle_close_interaction` |
| `risk_level` | str | Y | — | Risk engine | `high`, `medium`, `low` — final tier after sustained-risk downgrade |
| `peak_risk_level` | str | Y | — | Server | Pre-downgrade peak risk observed during the episode |
| `risk_demoted` | bool | Y | — | Server | True if `risk_level` was downgraded from `peak_risk_level` |
| `risk_frame_counts` | object | Y | `{low,medium,high}` | Server | Frame counts per risk tier across episode lifetime |
| `frame_count` | int | Y | — | Server | Total frames observed in the episode |
| `episode_duration_sec` | float | Y | — | Server | Wall-clock duration the episode stayed open |
| `confidence` | float | Y | — | Detection pipeline | YOLO detection confidence [0, 1] |
| `ttc_sec` | float | N | — | Risk engine | Multi-gate time-to-collision in seconds; null when gates fail |
| `distance_m` | float | N | — | Risk engine | Inter-object 3D distance (depth diff + lateral offset) |
| `distance_px` | float | Y | — | Detection pipeline | Image-plane bbox edge distance |
| `track_ids` | int[] | Y | — | Detection pipeline | The two ByteTrack IDs that form the interaction pair |
| `scene_context` | object | Y | — | Context engine | `{label, confidence, speed_proxy_mps, reason}` |
| `ego_flow` | object | N | — | Ego-motion | `{speed_proxy_mps, confidence}` |
| `perception_state` | str | Y | `"nominal"` | Quality monitor | `nominal`, `degraded_low_light`, `degraded_blur`, `degraded_low_confidence`, `degraded_overexposed` |
| `summary` | str | N | None | LLM layer | Narration text (may be templated fallback) |
| `plate_hash` | str | N | None | LLM layer (enrichment) | Salted SHA-256 hash of plate text |
| `plate_readable` | str | N | None | LLM layer | `"yes"`, `"partial"`, `"no"` |
| `thumbnail_public` | str | N | None | Server | Filename of redacted thumbnail |
| `timestamp` | str (ISO 8601) | Y | — | Server | UTC time of event emission |

### 7.3 Field Contract — Feedback

| Field | Type | Required | Default | Writable By | Notes |
|---|---|---|---|---|---|
| `event_id` | str | Y | — | Operator (via API) | References SafetyEvent |
| `verdict` | str | Y | — | Operator | `tp` or `fp` |
| `note` | str | N | `""` | Operator | Free-text explanation |
| `operator_ts` | str (ISO 8601) | Y | auto-generated | Server | UTC time of feedback |

### 7.4 Field Contract — AuditRecord

| Field | Type | Required | Default | Writable By | Notes |
|---|---|---|---|---|---|
| `ts` | str (ISO 8601) | Y | auto-generated | System only | |
| `action` | str | Y | — | System only | e.g., `access_unredacted_thumbnail`, `chat_query` |
| `resource` | str | Y | — | System only | Resource identifier |
| `actor` | str | Y | `"system"` | System only | |
| `outcome` | str | Y | `"success"` | System only | `success`, `denied` |
| `detail` | dict | N | None | System only | Additional context |
| `ip` | str | N | None | System only | Client IP (when available) |

### 7.5 Field Contract — VehicleState

| Field | Type | Required | Default | Writable By | Notes |
|---|---|---|---|---|---|
| `vehicle_id` | str | Y | — | Road registry | |
| `road_id` | str | Y | — | Road registry | |
| `driver_id` | str | N | None | Road registry | |
| `total_events` | int | Y | 0 | Road registry | Incremented on event |
| `events_by_risk` | dict[str, int] | Y | `{}` | Road registry | Count per risk level |
| `events_by_type` | dict[str, int] | Y | `{}` | Road registry | Count per event type |
| `safety_score` | float | Y | 100.0 | Road registry | Decaying penalty model |
| `feedback_tp` | int | Y | 0 | Road registry | True positive count |
| `feedback_fp` | int | Y | 0 | Road registry | False positive count |
| `last_event_ts` | float | N | None | Road registry | Unix timestamp (`time.time()`) of most recent event |

### 7.6 Schema Evolution Rules

- **Additive-only policy:** New fields are added as optional with defaults. No removal or renaming.
- **Backward compatibility:** JSON event payloads are forward-compatible; consumers must tolerate unknown keys.
- **Migration:** Not currently applicable (in-memory + JSONL); will be required when persistent DB is introduced.

---

## 8. State Model

### 8.1 Event Lifecycle States

| State | Description | Owner |
|---|---|---|
| DETECTING | Object pair is being tracked but has not yet passed all conflict gates | Detection pipeline |
| EPISODE_OPEN | Pair passed depth, convergence, ego-residual, and TTC gates; episode accumulates per-frame risk counts | Server |
| EMITTED | Episode flushed; `final_risk` computed (with possible sustained-risk downgrade) and dispatched | Server |
| COOLDOWN | Pair is in 8-second post-emission cooldown | Server |
| ENRICHED | LLM narration and/or vision enrichment completed | LLM layer |
| FEEDBACK_RECEIVED | Operator has submitted tp/fp verdict | Feedback service |
| SAMPLED | Event selected for active-learning relabeling | Drift monitor |

### 8.2 Allowed Transitions

| From | Event | To | Validation |
|---|---|---|---|
| DETECTING | All conflict gates pass for the pair this frame | EPISODE_OPEN | Depth gate, convergence filter, ego-residual gate, multi-gate TTC, scene-adaptive thresholds, low-speed floor |
| EPISODE_OPEN | Same-frame update | EPISODE_OPEN | Episode accumulates risk frame counts and peak frame |
| EPISODE_OPEN | Pair idle > `EPISODE_IDLE_FLUSH_SEC` | EMITTED | `Episode.final_risk()` computed; risk may be downgraded |
| EMITTED | — | COOLDOWN | 8-second timer starts |
| COOLDOWN | Timer expires | DETECTING | Same pair can re-trigger |
| EMITTED | LLM narration completes | ENRICHED | LLM available and rate budget OK |
| EMITTED | Operator submits verdict | FEEDBACK_RECEIVED | Valid event_id |
| EMITTED | Confidence in active-learning band or verdict=fp | SAMPLED | Active-learning sampler accepts |

### 8.3 Forbidden Transitions

- DETECTING → EMITTED (must go through EPISODE_OPEN — no skip-to-emit)
- COOLDOWN → EPISODE_OPEN (must wait for cooldown expiry, return to DETECTING first)
- EMITTED → EPISODE_OPEN (episodes are one-shot; new interaction = new episode)

### 8.4 Invariants

| Invariant | Enforcement | Verification |
|---|---|---|
| Same (track_A, track_B) pair emits at most once per episode | `server.py` pair dedup dict | Duplicate pair within episode window → no double emission |
| Events in COOLDOWN never re-trigger for same pair | `server.py` cooldown timer (8s) | Same pair reappearing < 8s → suppressed |
| Public thumbnail never contains unblurred faces/plates | `redact.py` blur pipeline | `_public.jpg` file always has blur applied to person/vehicle regions |
| Plate text never appears in egress payload | `server.py` PII scrub | `plate_text` and `plate_state` stripped before SSE/Slack/cloud |
| Safety score never exceeds 100 or drops below 0 | `registry.py` clamping | Score clamped to [0, 100] after penalty/decay |
| Agent tool loop never exceeds 5 iterations | `agents.py` hard stop | Counter check per iteration |
| LLM rate bucket never allows > 3 req/min sustained | `llm.py` TokenBucket | Refusal before API call when bucket empty |

### 8.5 Idempotency / Retries

- **Event emission:** Each event gets a unique `event_id` (`evt_{timestamp}_{seq}`). Cloud receiver deduplicates on `event_id`.
- **Edge→cloud delivery:** At-least-once semantics with HMAC verification. Cloud ignores duplicate `event_id`.
- **Feedback:** Multiple verdicts for the same `event_id` are appended (latest wins for drift calculation).
- **LLM calls:** Retried through secondary provider on failure; result is cached per event (no double-narration).
- **Agent invocations:** Not idempotent by design (each invocation produces fresh analysis). Rate-limited at API layer.

---

## 9. API Contract

### 9.1 Endpoints

| Operation | Method | Path | Caller | Auth | Purpose | Source |
|---|---|---|---|---|---|---|
| Live status | GET | `/api/live/status` | Dashboard | None | Current event/frame counts | BR-01 |
| Live perception | GET | `/api/live/perception` | Dashboard | None | Perception-quality state | BR-02 |
| Live scene | GET | `/api/live/scene` | Dashboard | None | Scene + adaptive thresholds | BR-02 |
| Event stream | GET | `/stream/events` | Dashboard (SSE) | None | Real-time event stream | BR-03 |
| Recent live events | GET | `/api/live/events` | Dashboard, agents | None | Last N in-memory events | BR-03 |
| Batch events | GET | `/api/events` | Ops, evaluation | None | Offline batch-analysis events | BR-03 |
| Event by ID | GET | `/api/events/{event_id}` | Ops, evaluation | None | Single offline batch event | BR-03 |
| Thumbnails | GET | `/thumbnails/{name}` | Dashboard / DSAR holder | Public for `*_public.*` by default; optional `exp`/`token` query when `ROAD_PUBLIC_THUMBS_REQUIRE_TOKEN=1`; `X-DSAR-Token` for raw | Redacted thumbnails and DSAR-gated unredacted thumbnails | BR-04 |
| Feedback submit | POST | `/api/feedback` | Operator | None | Submit tp/fp verdict | BR-05 |
| Coaching queue | GET | `/api/coaching_queue` | Operator | None | Pending medium-risk review queue | BR-05 |
| Drift report | GET | `/api/drift` | ML engineer | None | Rolling precision + trends | BR-10 |
| Active learning export | POST | `/api/active_learning/export` | ML engineer | Bearer `ROAD_ADMIN_TOKEN` | Zip of pending samples | BR-09 |
| Chat | POST | `/chat` | Operator | None | RAG-based Q&A | BR-07 |
| LLM stats | GET | `/api/llm/stats` | ML engineer | Bearer `ROAD_ADMIN_TOKEN` | Token cost, latency, error rate | BR-11 |
| LLM recent | GET | `/api/llm/recent` | ML engineer | Bearer `ROAD_ADMIN_TOKEN` | Last N LLM call records | BR-11 |
| Audit trail | GET | `/api/audit` | Compliance | Bearer `ROAD_ADMIN_TOKEN` | Recent audit records | BR-12 |
| Audit stats | GET | `/api/audit/stats` | Compliance | Bearer `ROAD_ADMIN_TOKEN` | Audit event counts | BR-12 |
| Retention sweep | POST | `/api/retention/sweep` | Ops | Bearer `ROAD_ADMIN_TOKEN` | Trigger manual retention sweep | BR-13 |
| Road summary | GET | `/api/road/summary` | Road manager | Bearer `ROAD_ADMIN_TOKEN` | Aggregate road stats | BR-14 |
| Road vehicle | GET | `/api/road/vehicle/{id}` | Road manager | Bearer `ROAD_ADMIN_TOKEN` | Per-vehicle detail | BR-14 |
| Road drivers | GET | `/api/road/drivers` | Road manager | Bearer `ROAD_ADMIN_TOKEN` | Driver leaderboard | BR-14 |
| Agent coaching | POST | `/api/agents/coaching` | Road manager | Bearer `ROAD_ADMIN_TOKEN` | AI coaching note | BR-15 |
| Agent investigation | POST | `/api/agents/investigation` | Road manager | Bearer `ROAD_ADMIN_TOKEN` | AI root-cause analysis | BR-16 |
| Agent report | POST | `/api/agents/report` | Road manager | Bearer `ROAD_ADMIN_TOKEN` | AI safety summary | BR-17 |

### 9.2 Request/Response Schemas

#### POST /api/feedback

**Request:**
```json
{
  "event_id": "evt_1776232308220_0003",
  "verdict": "tp",
  "note": "Confirmed close call with pedestrian"
}
```

**Response (200):**
```json
{
  "ok": true
}
```

#### POST /chat

**Request:**
```json
{
  "query": "What is our following distance policy?"
}
```

**Response (200):**
```json
{
  "answer": "Per road policy, maintain 3 seconds following distance at highway speeds..."
}
```

#### POST /api/agents/coaching

**Request:**
```json
{
  "event_id": "evt_1776232308220_0003"
}
```

**Response (200):**
```json
{
  "result": "...",
  "steps": 3,
  "model": "claude-sonnet-4-20250514"
}
```

#### GET /api/road/summary

**Response (200):**
```json
{
  "road_id": "road_01",
  "vehicle_count": 5,
  "total_events": 142,
  "aggregate_by_risk": {"high": 12, "medium": 45, "low": 85},
  "lowest_score_vehicle": {
    "vehicle_id": "VH-003",
    "safety_score": 62.5
  },
  "vehicles": []
}
```

### 9.3 Error Contract

| Code | HTTP | Meaning | User-Facing Behavior | Retry? |
|---|---|---|---|---|
| `MISSING_QUERY` | 400 | Chat query body empty | "Missing 'query' field" | No |
| `MISSING_EVENT_ID` | 400 | Agent request without event_id | "Missing 'event_id'" | No |
| `NOT_FOUND` | 404 | Requested event/vehicle not found | "Event not found" | No |
| `DSAR_DENIED` | 403 | Unredacted thumbnail without DSAR token | "Present X-DSAR-Token header" | No (need token) |
| `PUBLIC_THUMB_DENIED` | 403 | Public thumbnail token missing/invalid when signed mode enabled | "public thumbnail requires valid exp/token query params" | Yes (request a fresh signed URL) |
| `STREAM_ERROR` | 500 | Video stream read failure | "Stream unavailable" | Yes (auto-reconnect) |
| `LLM_UNAVAILABLE` | 503 | Both LLM providers failed | Narration returns None; detection continues | Yes (auto-retry) |

### 9.4 Backward Compatibility

- All API responses are JSON objects with optional fields
- New fields are added as optional; existing consumers ignore unknown keys
- No breaking changes to existing endpoint signatures

---

## 10. Authorization, Privacy, and Security

### 10.1 Permissions Matrix

| Operation | Auth Required | Gate Mechanism | Notes |
|---|---|---|---|
| Public endpoints (status, events, recent) | No | — | Operator dashboard view |
| Redacted thumbnails (`*_public.*`) | Optional | If enabled: `exp`/`token` query params (`ROAD_PUBLIC_THUMBS_REQUIRE_TOKEN=1`) | Audit-logged on success and denial |
| Unredacted thumbnails | Yes | `X-DSAR-Token` header vs `ROAD_DSAR_TOKEN` env | Audit-logged on success and denial |
| Feedback submission | Recommended | Reverse-proxy auth or API key (deployment-specific) | Audit-logged |
| Active learning export | Yes | Bearer `ROAD_ADMIN_TOKEN` | Audit-logged |
| Chat copilot | No | — | Audit-logged |
| Agent invocations | Yes | Bearer `ROAD_ADMIN_TOKEN` | Audit-logged |
| LLM observability / audit / retention / road summary | Yes | Bearer `ROAD_ADMIN_TOKEN` | Operational control plane |
| Cloud ingest | Yes | HMAC-SHA256 signature verification | `Signature` header |

### 10.2 Privacy Requirements

| Requirement | Implementation | Module |
|---|---|---|
| Shared event channels exclude raw plate text | `plate_text` / `plate_state` stripped before SSE/Slack/cloud; unredacted thumbnails remain local except for optional enrichment integrations | `server.py` |
| Optional signed public-thumbnail access | `_public` thumbnails can require short-lived `exp`/`token` query params when `ROAD_PUBLIC_THUMBS_REQUIRE_TOKEN=1` | `server.py` |
| Face blurring | Upper 35% of person bbox Gaussian-blurred | `redact.py` |
| Plate blurring | Lower-middle strip of vehicle bbox blurred | `redact.py` |
| Plate text hashing | Raw text → salted SHA-256; salt per deployment | `redact.py` |
| DSAR-gated raw access | Token required for unredacted thumbnails | `server.py` |
| External ALPR policy gate | Third-party ALPR disabled unless `ROAD_ALPR_MODE=third_party` | `server.py` |
| Data retention | Auto-expiry: thumbnails 30d, feedback 90d, AL 60d, queue 7d | `retention.py` |
| Audit trail | All sensitive access logged with actor, timestamp, outcome | `audit.py` |

### 10.3 Security Controls

| Control | Implementation | Notes |
|---|---|---|
| HMAC-SHA256 signing | Edge→cloud payloads signed with shared secret | `edge_publisher.py` |
| Prompt injection defense | Image content marked UNTRUSTED; injection patterns detected/scrubbed | `llm.py` (OWASP LLM01:2025) |
| Rate limiting (LLM) | Token bucket (3 req/min) + circuit breaker (3 failures → 60s open) | `llm.py` |
| Input validation | FastAPI request body validation | `server.py` |
| Secret management | All secrets via environment variables; never hardcoded | `.env.example` |

---

## 11. Client Behavior and UX Technical Contract

### 11.1 UX / Interaction Rules

- Dashboard receives events via SSE (Server-Sent Events)
- Events render as cards with risk badge (color-coded), thumbnail, narration
- Operator can click thumbs-up/thumbs-down to submit feedback
- Chat copilot is a text input that POSTs to `/chat` and displays the response
- Road summary is a read-only dashboard panel

### 11.2 Offline / Weak Network Behavior

| Scenario | Behavior |
|---|---|
| Dashboard loses SSE connection | Browser auto-reconnects; events accumulate server-side |
| Edge→cloud network outage | Events queue in local JSONL; drain on reconnect with exponential backoff |
| LLM API unreachable | Narration returns None; detection continues; events emit without narrative |

### 11.3 Caching and Staleness

| Dataset | Cache Location | TTL | Refresh Trigger |
|---|---|---|---|
| Recent events | Server in-memory (200 events max) | Evicted on overflow | New event pushes oldest out |
| Road vehicle state | Server in-memory | Persistent during session | Updated on every event/feedback |
| Drift report | Server in-memory | Re-computed on each feedback | `/api/drift` always returns fresh state |
| LLM metrics | In-memory ring buffer (5000 records) | Evicted on overflow | Computed on-demand from ring buffer |
| Thumbnails | Disk (`data/thumbnails/`) | Retention policy (30d default) | Deleted by retention sweep |

### 11.4 Accessibility and Localization Impact

- Dashboard: semantic HTML, color-coded badges with text labels (not color-only)
- All timestamps in ISO 8601 UTC format
- No localization required for v1.0 (English only)

---

## 12. Non-Functional Requirements

### 12.1 Performance

| Metric | Target | Measurement |
|---|---|---|
| Frame-to-event latency | < 2 seconds | Timestamp delta: frame capture → event emission |
| LLM narration P95 | < 3 seconds | `llm_obs.py` latency metric |
| Chat copilot P95 | < 5 seconds | `llm_obs.py` latency metric |
| Thumbnail generation | < 200ms | OpenCV encode time |
| SSE event delivery | < 100ms from emission | SSE push latency |
| API response (non-LLM) | < 50ms P95 | FastAPI response time |

### 12.2 Reliability

| Metric | Target |
|---|---|
| Core detection uptime | 99.9% (LLM failures do not affect detection) |
| LLM enrichment availability | > 90% (with multi-provider failover) |
| Edge→cloud delivery | At-least-once (HMAC-signed, queued) |
| Data loss on restart | Zero for JSONL-persisted data; in-memory state resets |

### 12.3 Scalability

| Dimension | Current | Path to Scale |
|---|---|---|
| Vehicles per edge node | 1 | By design (single stream per node) |
| Edge nodes per cloud receiver | Unlimited (stateless ingest) | event_id dedup handles overlap |
| Events per second | ~2 (at 2 fps) | GPU inference → 30 fps → ~15 events/sec |
| LLM calls per minute | 3 (rate-limited) | Increase with paid tier |

### 12.4 Cost Considerations

| Component | Estimated Cost | Optimization |
|---|---|---|
| LLM narration (Anthropic Haiku) | ~$0.001/event | Skip for low-risk events; rate budget |
| LLM enrichment (vision) | ~$0.005/event | Skip when perception degraded; circuit breaker |
| Cloud hosting | ~$20/month (single node) | Edge-first reduces cloud compute |
| Cellular bandwidth (edge→cloud) | ~$0.10/MB | 2KB events vs 1GB raw video = negligible |

---

## 13. Observability and Analytics

### 13.1 Logging

| Log Event | Module | Metadata |
|---|---|---|
| Stream connected/disconnected | `stream.py` | URL, resolution, timestamp |
| Event emitted | `server.py` | event_id, event_type, risk_level |
| LLM call (success/failure/skip) | `llm.py`, `llm_obs.py` | model, tokens, latency, error |
| Feedback received | `feedback.py` | event_id, verdict |
| Drift alert | `drift.py` | precision, worst_type, window_size |
| Agent invocation | `agents.py` | agent_type, event_id, steps, duration |
| Audit event | `audit.py` | action, resource, actor, outcome, ip |
| Retention sweep | `retention.py` | items_deleted per category |
| Edge batch published | `edge_publisher.py` | batch_size, status_code |

### 13.2 Metrics

| Metric | Type | Owner | Alert Threshold |
|---|---|---|---|
| `detection_precision` | Gauge | Drift monitor | < 70% over 50 labels |
| `llm_error_rate` | Gauge | LLM observer | > 20% in 5-minute window |
| `llm_cost_per_hour` | Counter | LLM observer | > $1.00/hour |
| `llm_p95_latency_ms` | Gauge | LLM observer | > 5,000ms |
| `events_emitted_total` | Counter | Server | 0 events in 30 minutes |
| `perception_state` | Gauge | Quality monitor | Degraded > 10 minutes |
| `active_learning_pending` | Gauge | Drift monitor | > 500 samples |
| `safety_score_min` | Gauge | Road registry | < 50 for any vehicle |

### 13.3 Alerts

| Alert | Trigger | Channel | Severity |
|---|---|---|---|
| Precision drop | < 70% over 50 labels | Slack | Warning |
| LLM down | Both providers failing > 5 minutes | Slack | Critical |
| High-risk event | risk_level = "high" | Slack (instant) | High |
| Vehicle safety score critical | score < 50 | Slack | Warning |

### 13.4 Analytics Events

| Event | Trigger | Properties | Source of Truth |
|---|---|---|---|
| `event_detected` | New safety event emitted | event_id, type, risk, confidence, scene | Server |
| `event_narrated` | LLM narration completed | event_id, model, tokens, latency_ms | LLM observer |
| `event_enriched` | Vision enrichment completed | event_id, plate_hash, readable | LLM observer |
| `feedback_submitted` | Operator submits verdict | event_id, verdict | Feedback service |
| `agent_invoked` | Agent tool-calling loop starts | agent_type, event_id | Agent executor |
| `drift_alert` | Precision drops below threshold | precision, worst_type | Drift monitor |

---

## 14. Testing Strategy

### 14.1 Unit Tests

| Component | Coverage | Tool |
|---|---|---|
| TTC computation | Correct TTC from bbox growth rates | `tools/eval_detect.py` |
| Distance estimation | Correct distance from focal-length model | `tools/eval_detect.py` |
| Scene classification | Correct scene from detection density + ego speed | `tools/eval_detect.py` |
| Risk classification | Correct risk level from TTC + scene thresholds | `tools/eval_detect.py` |
| Plate hashing | Deterministic hash; salt changes hash | manual test |
| Episode dedup | Same pair emits once; different pairs emit independently | manual test |

### 14.2 Integration Tests

| Flow | Coverage |
|---|---|
| Frame → Detection → Tracking → Event | End-to-end pipeline with test video |
| Event → Narration → SSE | LLM enrichment and dashboard delivery |
| Event → Edge Publish → Cloud Receive | HMAC signing, transmission, dedup |
| Feedback → Drift → Active Learning | Feedback verdict flows to precision and sampling |

### 14.3 Evaluation Harness (`tools/eval_detect.py`)

| Mode | What It Does |
|---|---|
| Single clip | Runs detection on a clip, compares to ground truth, reports P/R/F1 |
| Suite | Runs multiple clips, generates markdown report, detects regressions (>3% drop) |
| Compare | A/B comparison of two evaluation runs with delta highlighting |

### 14.4 Failure and Edge Case Tests

| Test | Scenario |
|---|---|
| LLM both providers fail | Verify detection continues; narration returns None |
| Empty video stream | Verify server starts without crash; endpoints return empty data |
| Malformed feedback | Verify 400 error returned; no crash |
| Concurrent feedback on same event | Verify both appended; latest wins for drift |
| HMAC signature mismatch | Cloud receiver rejects batch with 401 |
| Retention sweep on empty dirs | Verify no crash; returns zero counts |
| Agent max iterations | Verify hard stop at 5 steps; returns partial result |

### 14.5 Test Data and Environment

- **Test clips:** Stored in `data/test_suite/` with manifest JSON
- **Ground truth:** JSON files with labeled events per clip
- **Environment:** Local Python 3.10+ with `pyproject.toml` dependencies (`pip install -e ".[dev]"`)

---

## 15. Rollout, Migration, and Rollback

### 15.0 Phase Plan Matrix

| Phase | Goal | Included BRD IDs | Technical Scope | Dependencies | Exit Criteria | Status |
|---|---|---|---|---|---|---|
| P1 | Core detection + operator tools | BR-01 to BR-08 | Detection, tracking, risk, LLM, redaction, SSE, Slack, edge/cloud, chat | YOLOv8, LLM API | Events detected and narrated from live stream; Slack alerts fire; feedback accepted | Done |
| P2 | Observability + compliance + road | BR-09 to BR-14 | Drift, active learning, LLM obs, audit, retention, road registry | P1 complete | Drift report shows precision; audit trail logs access; road summary API works | Done |
| P3 | AI agents | BR-15 to BR-17 | Coaching, investigation, report agents | P2 complete | All three agents return structured results; max 5 iterations enforced | Done |

### 15.1 Rollout Strategy

- **Feature flags:** Each subsystem is independently toggleable via environment variables:
  - LLM: `ANTHROPIC_API_KEY` / `AZURE_OPENAI_*` (absent = disabled)
  - Slack: `SLACK_WEBHOOK_URL` (absent = disabled)
  - Edge publish: `ROAD_CLOUD_ENDPOINT` (absent = disabled)
  - Agents: Available when LLM is configured
  - Retention: `ROAD_RETENTION_*` env vars with sane defaults

- **Phased rollout:** Deploy edge node per vehicle → verify single-vehicle operation → add vehicles → enable cloud aggregation

### 15.2 Migration Plan

No data migration required for v1.0 (in-memory + JSONL storage). When persistent database is introduced:
- Schema migration scripts will be required
- Backward-compatible JSON event format ensures no breaking changes
- JSONL files can be bulk-imported into new storage

### 15.3 Rollback Plan

| Trigger | Rollback Action | Data Impact |
|---|---|---|
| Detection precision < 50% | Revert YOLO weights to previous version | None |
| LLM costs exceed budget | Set rate limit to 0 or remove API keys | Narration stops; detection continues |
| Privacy breach detected | Disable edge publisher; audit unredacted access | Events stop flowing to cloud |
| Agent producing bad coaching | Disable agent endpoints (remove from server routes) | No agent results; detection continues |

---

## 16. Operational Runbook (Day-2)

### 16.1 On-Call Guide

| Symptom | Likely Cause | First Action |
|---|---|---|
| No events emitting | Stream disconnected or YOLO model failed to load | Check `[server]` logs; verify stream URL; check `yolov8n.pt` exists |
| Narration always "None" | LLM API keys missing or both providers down | Check env vars; check `llm_obs` error rate; verify API key validity |
| Precision dropping | Data drift (new scene type, weather, camera angle) | Review drift report; check perception quality; export AL samples for relabeling |
| Slack alerts not firing | Webhook URL missing or Slack API issue | Check `SLACK_WEBHOOK_URL` env; test webhook manually |
| Edge→cloud delivery failing | Network issue or HMAC secret mismatch | Check edge node connectivity; verify `ROAD_CLOUD_HMAC_SECRET` matches on both sides |
| Audit log growing too large | Retention sweep not running | Check retention loop; trigger manual `/api/retention/sweep` with admin bearer token |

### 16.2 Manual Recovery Procedures

- **Restart server:** `uvicorn road_safety.server:app --host 0.0.0.0 --port 8000`
- **Flush edge queue:** Delete `data/outbound_queue.jsonl`
- **Reset drift state:** Restart server (in-memory state resets)
- **Force retention sweep:** `POST /api/retention/sweep` with `Authorization: Bearer <ROAD_ADMIN_TOKEN>`
- **Export active learning:** `POST /api/active_learning/export` with bearer token → returns zip path

### 16.3 Support/CS Notes

- Events are deduplicated per tracked pair — same pair emitting once is correct behavior, not a bug
- "No narration" on events is expected when LLM is unavailable — detection still works
- Thumbnails with `_public` suffix are safe-to-share versions; deployments can still require signed `exp/token` URLs
- Internal thumbnails require DSAR token (`X-DSAR-Token`)

---

## 17. Risks and Mitigations

| Risk | Impact | Probability | Mitigation | Owner |
|---|---|---|---|---|
| YOLO false positives in new environments | Alert fatigue | Medium | Scene-adaptive thresholds + feedback loop | ML team |
| LLM provider deprecates model | Narration breaks | Low | Multi-provider failover; model version pinned | ML team |
| GDPR audit reveals PII in logs | Fine up to 4% global revenue | Low | Structural PII isolation; audit trail | Compliance |
| Edge node hardware failure | Events lost for one vehicle | Medium | Local JSONL queue survives restarts; at-least-once delivery | DevOps |
| LLM hallucination in coaching | Operator follows bad advice | Medium | Structured output schema; human-in-the-loop | ML team |
| Drift goes undetected | Model silently degrades | Low | Rolling precision monitor; Slack alerts on drop | ML team |

---

## 18. BRD to TRD Traceability Matrix

| BRD Req ID | BRD Summary | Phase | TRD Section | Coverage | Status |
|---|---|---|---|---|---|
| BR-01 | Real-time detection from live video | P1 | §6.3, §8, §12.1 | Detection pipeline, state model, latency target | Mapped |
| BR-02 | TTC + distance risk classification with adaptive thresholds | P1 | §7.2, §8, §6.2 | SafetyEvent fields, risk engine ownership | Mapped |
| BR-03 | Live operator feed with thumbnails, badges, narration | P1 | §9.1, §11.1 | SSE endpoint, UX rules | Mapped |
| BR-04 | PII-redacted thumbnails | P1 | §10.2, §8.4 | Privacy requirements, invariants | Mapped |
| BR-05 | Operator feedback (tp/fp) | P1 | §9.2, §7.3 | Feedback schema, API contract | Mapped |
| BR-06 | Tiered Slack alerts | P1 | §13.3 | Alert definitions | Mapped |
| BR-07 | Chat copilot with RAG | P1 | §9.1, §9.2 | Chat endpoint, request/response schema | Mapped |
| BR-08 | Edge→cloud with HMAC-signed batches | P1 | §10.3, §8.5 | Security controls, idempotency | Mapped |
| BR-09 | Active learning export | P2 | §9.1, §14.3 | Export endpoint, eval harness | Mapped |
| BR-10 | Drift monitoring with precision trends | P2 | §13.2, §13.3 | Metrics, alerts | Mapped |
| BR-11 | LLM cost/latency/error observability | P2 | §13.1, §13.2 | Logging, metrics | Mapped |
| BR-12 | Audit trail for sensitive access | P2 | §7.4, §10.1 | AuditRecord schema, permissions | Mapped |
| BR-13 | Data retention with auto-expiry | P2 | §10.2, §12.2 | Privacy requirements, reliability | Mapped |
| BR-14 | Road-wide summary with vehicle scores | P2 | §7.5, §9.1, §9.2 | VehicleState schema, road endpoints | Mapped |
| BR-15 | AI coaching agent | P3 | §9.1, §8.4, §9.2 | Agent endpoint, invariants, request schema | Mapped |
| BR-16 | AI investigation agent | P3 | §9.1, §8.4, §9.2 | Agent endpoint, invariants, request schema | Mapped |
| BR-17 | AI report agent | P3 | §9.1, §9.2 | Agent endpoint, request schema | Mapped |

---

## 19. Open Questions and Blockers

| ID | Question / Gap | Impacted Section | Severity | Owner | Status |
|---|---|---|---|---|---|
| Q-01 | Persistent database choice (SQLite vs PostgreSQL vs Redis) | §5.2, §7.6 | Medium | Architecture | Deferred — JSONL sufficient for v1.0 throughput envelope |
| Q-02 | Authentication/authorization middleware (SSO, RBAC) | §10.1 | Medium | Security | Deployment-specific — reverse-proxy auth recommended for v1.0 |
| Q-03 | GPU inference optimization path (TensorRT, ONNX) | §12.3 | Low | ML team | Open — future work |
| Q-04 | Multi-tenant isolation for SaaS deployment | §2.2 | Low | Architecture | Open — out of scope for v1.0 |

---

## 20. Decisions Log

| ID | Date | Decision | Rationale | Alternatives Considered |
|---|---|---|---|---|
| D-01 | 2026-04-01 | Use YOLOv8n (nano) for detection | Must run on CPU at 2 fps; nano is smallest variant | YOLOv8s (too slow on CPU), YOLOv8m (much too slow) |
| D-02 | 2026-04-01 | LLM on metadata only (no video frames) | Cost: $0.005/frame vs $0.001/event; safety: no video in LLM context | Full video-to-LLM pipeline (too expensive, privacy risk) |
| D-03 | 2026-04-05 | Edge-first architecture | Privacy boundary: PII never leaves device; bandwidth: 2000x reduction | Cloud-first (bandwidth cost, privacy risk) |
| D-04 | 2026-04-08 | Salted SHA-256 for plate text | Enables cross-event correlation without storing raw PII | Encryption (key management overhead), discard (loses correlation) |
| D-05 | 2026-04-08 | Dual-thumbnail architecture | Structural guarantee: wrong thumbnail cannot leak PII | Single thumbnail with ACL (configuration error = leak) |
| D-06 | 2026-04-10 | Multi-provider LLM failover | Zero operator intervention during provider outages | Single provider with retry (single point of failure) |
| D-07 | 2026-04-10 | Self-consistency for ALPR | Eliminates hallucinated plates (15-20% error rate with single call) | Single call (cheaper but unreliable), no ALPR (loses value) |
| D-08 | 2026-04-12 | Three bounded agents (≤5 tools each) | 68% parameter hallucination when tool catalog > 10 | Single mega-agent (tool overload), two agents (insufficient separation) |
| D-09 | 2026-04-12 | 5-iteration hard stop for agents | Prevents runaway loops and cost explosions | No limit (risk of infinite loop), 3 (too restrictive for investigation) |
| D-10 | 2026-04-12 | Decaying penalty model for driver scoring | Score recovers over time; punishes recent events more heavily | Rolling window (loses history), cumulative (never recovers) |
| D-11 | 2026-04-15 | JSONL for audit/feedback/queue | Simple, append-only, no DB dependency; meets v1.0 throughput envelope | SQLite (overhead), PostgreSQL (infrastructure), Redis (volatility) |
| D-12 | 2026-04-15 | In-memory road registry with periodic snapshot | Operational state is reconstructible from event stream; avoids hard dependency on Redis/DB at v1.0 scale | Redis (adds infrastructure dependency), DB (premature for current scale) |

---

## 21. Handoff to Implementation Document

Complete this checklist before creating or approving the implementation doc:

- [x] Scope and non-goals are explicit and conflict-free (§2, §3)
- [x] BRD Requirement Inventory (§0.2) is complete and reviewed
- [x] Coverage Checksum (§0.3) passes
- [x] Shared platform standards compliance (§3.4) is complete
- [x] Architecture, ownership, and state model are complete (§6, §8)
- [x] API/data/auth contracts are complete and version-safe (§7, §9, §10)
- [x] Failure behavior and retry/idempotency rules are explicit (§8.5)
- [x] Observability and analytics contracts are defined (§13)
- [x] Testing strategy covers critical paths and edge cases (§14)
- [x] Rollout/migration/rollback plan is actionable (§15)
- [x] Open blockers are resolved or formally accepted (§19 — all Medium/Low, none block v1.0)
