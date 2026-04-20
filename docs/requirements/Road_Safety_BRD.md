# Road Safety AI Platform — Business Requirements Document (BRD)

**Feature:** Road Safety — Live Event Detection + LLM Copilot
**Product:** Road Safety AI Platform (Python / FastAPI)
**Document Type:** Business Requirements Document (BRD)
**Version:** v1.1
**Status:** APPROVED
**Created:** 2026-04-15
**Last Updated:** 2026-04-15
**Author(s):** Andrei Tekhtelev

---

## 0. Version History

| Version | Date | Author | Description of Changes |
|---|---|---|---|
| v0.1 | 2026-04-01 | A. Tekhtelev | Initial draft — core detection + narration |
| v0.5 | 2026-04-08 | A. Tekhtelev | Added edge/cloud, drift, privacy modules |
| v1.0 | 2026-04-15 | A. Tekhtelev | Added agents, road readiness, LLM observability, retention |
| v1.1 | 2026-04-15 | A. Tekhtelev | Documentation sync: ALPR policy gate, signed public-thumbnail option, score-decay scheduler controls |

---

## 1. Feature Overview

### 1.1 Summary

- **Feature Name:** Road Safety AI Platform — Real-Time Event Detection & LLM Copilot
- **Status:** Approved
- **Objective:** Build a production-grade road safety system that detects pedestrian-vehicle and vehicle-vehicle proximity events in real-time from live video, classifies risk using physical-unit kinematics, narrates events with LLM, and provides operator tools (chat copilot, AI coaching agents, drift monitoring) — while maintaining GDPR/CCPA compliance and road-scale readiness.
- **Stakeholders:** Road Safety Operators, Road Managers, Compliance Officers, ML/AI Engineers
- **Success Metrics (KPIs):**
  - Event detection precision ≥ 70% (measured via operator feedback)
  - High-risk event recall ≥ 80%
  - LLM enrichment skip rate < 30% under normal conditions
  - Bandwidth reduction ≥ 2,000x vs raw video
  - No raw plate text or unredacted thumbnails in shared event channels; any external enrichment path must be explicitly enabled and governed
  - Operator feedback velocity ≥ 5 verdicts/hour during active review

### 1.2 Scope & Dependencies

- **Type:** Standalone platform with edge/cloud split
- **Prerequisites (Upstream):**
  - YOLOv8 pretrained weights (`yolov8n.pt`)
  - LLM API access (Anthropic and/or Azure OpenAI)
  - Live video source (HLS, RTSP, or local mp4)
- **Impacts (Downstream):**
  - Slack channels receiving tiered alerts
  - Cloud receiver aggregating events from edge nodes
  - Labeling tools (Label Studio / CVAT) consuming active-learning exports

### 1.3 Assumptions & Constraints

- **Assumptions:**
  - Camera is forward-facing dashcam or fixed-position traffic camera
  - Stream provides ≥ 2 fps of frames suitable for YOLO inference
  - LLM API keys may or may not be present (system must degrade gracefully)
  - Single vehicle per edge node; road aggregation at cloud layer

- **Constraints:**
  - **Regulatory:** GDPR Art. 4 (PII), Art. 5(1)(e) (retention), Art. 30 (records of processing); CCPA PI definitions
  - **Platform:** Python 3.10+, runs on commodity x86 / ARM CPUs (GPU optional, not required)
  - **Budget:** Must operate within Anthropic Haiku free/low-tier rate limits (≤ 5 req/min)
  - **Latency:** Event emission within 2 seconds of real-time frame capture

### 1.4 Risks & Mitigation

| Risk Type | Description | Impact | Mitigation Strategy |
|---|---|---|---|
| Technical | LLM rate limits cause enrichment failures | Degraded narration quality | Circuit breaker + token-bucket rate limiter + multi-provider failover |
| Technical | YOLO false positives on crowded scenes | Alert fatigue, operator distrust | Scene-adaptive thresholds + episode dedup + operator feedback loop |
| Compliance | PII leakage through thumbnails or event data | Regulatory fines, reputational damage | Dual-thumbnail architecture, plate hashing, DSAR gating, audit trail |
| Operational | Model drift from new camera angles / weather | Silent precision degradation | Rolling precision monitor + Slack alerts + active learning pipeline |
| Scalability | Single-process architecture limits road growth | Cannot support multi-vehicle roads | Road-ready data model with vehicle/driver identity from day one |
| AI Agents | Agent hallucination or runaway tool loops | Incorrect coaching / wasted cost | Bounded tool sets (≤5), hard stop at 5 iterations, structured output schema |

### 1.5 Glossary

- **TTC:** Time-to-collision — estimated seconds until two objects collide, derived from bbox scale growth
- **Episode:** A single sustained interaction between two tracked objects (emits once at peak severity)
- **DSAR:** Data Subject Access Request — GDPR mechanism for individuals to request their data
- **Active Learning:** ML strategy of selectively labeling the most informative samples for retraining
- **Decision Boundary:** Confidence range [0.35, 0.50] where the model is most uncertain
- **Drift:** Degradation of model precision over time due to data distribution shift

---

## 2. Requirements & UX

### 2.1 Functional Requirements (User Stories)

| ID | Requirement |
|---|---|
| FR-01 | As an operator, I can view a live feed of safety events with thumbnails, risk badges, and narrations so I can prioritize my review. |
| FR-02 | As an operator, I can ask questions about recent events and road policy via a chat copilot so I can make informed decisions. |
| FR-03 | As an operator, I can mark events as true positive or false positive so the system tracks its own precision. |
| FR-04 | As a road manager, I can receive tiered Slack alerts (high=instant, medium=hourly, low=daily) so I am not overwhelmed. |
| FR-05 | As a road manager, I can view a road-wide safety summary with per-vehicle scores and driver rankings. |
| FR-06 | As a road manager, I can request an AI coaching note for any event to generate a structured coaching document. |
| FR-07 | As a road manager, I can request an AI investigation of any event to find patterns and root causes. |
| FR-08 | As a road manager, I can request an AI safety report summarizing all recent activity. |
| FR-09 | As a compliance officer, I can access an audit trail showing who accessed sensitive data and when. |
| FR-10 | As a compliance officer, I can verify that data retention policies are enforced automatically. |
| FR-11 | As an ML engineer, I can export active-learning samples for relabeling in Label Studio / CVAT. |
| FR-12 | As an ML engineer, I can view drift reports showing precision trends by event type and risk level. |
| FR-13 | As an ML engineer, I can view LLM cost, latency, and error rate metrics for operational monitoring. |
| FR-14 | As an operator, the system detects pedestrian-vehicle and vehicle-vehicle proximity events in real-time from live video. |
| FR-15 | As an operator, events are risk-classified using physical-unit TTC and ground-plane distance with scene-adaptive thresholds. |
| FR-16 | As an operator, events include PII-redacted thumbnails (faces and plates blurred) for safe sharing. |
| FR-17 | As a system, the edge node publishes events to the cloud receiver via HMAC-signed HTTPS with at-least-once delivery. |

### 2.2 Non-Functional Requirements

| Category | Requirement |
|---|---|
| **Performance** | Event detection + emission within 2 seconds of frame capture at 2 fps |
| **Performance** | LLM narration P95 latency < 3 seconds |
| **Performance** | Chat copilot P95 latency < 5 seconds |
| **Reliability** | System operates continuously without LLM (graceful degradation to templated summaries) |
| **Reliability** | Edge node survives network outages (local queue, at-least-once delivery) |
| **Security** | HMAC-SHA256 signed payloads for edge→cloud; TLS for confidentiality |
| **Security** | Sensitive operational endpoints require bearer-token access control |
| **Security** | OWASP LLM01:2025 — image content treated as untrusted user data |
| **Security** | Optional signed public-thumbnail access (`exp/token`) when enabled by policy |
| **Privacy** | Shared event channels (SSE, Slack, cloud) exclude raw plate text and unredacted thumbnails; optional third-party enrichment must be separately governed |
| **Privacy** | Third-party ALPR is policy-gated (`ROAD_ALPR_MODE=third_party`) and off by default |
| **Privacy** | Plate text retained only as salted SHA-256 hash |
| **Privacy** | Unredacted thumbnails gated by DSAR token |
| **Privacy** | Configurable data retention with automatic expiry |
| **Scalability** | Road-ready data model (vehicle_id, road_id, driver_id on every event) |
| **Observability** | Per-call LLM token cost, latency percentiles, error/skip rates |
| **Observability** | Rolling precision monitoring with drift alerts |
| **Observability** | Audit trail for all sensitive data access |

### 2.3 Edge Cases

| Scenario | Handling |
|---|---|
| No LLM API key configured | Narration falls back to templated summary; chat disabled cleanly |
| ALPR policy left at default (`ROAD_ALPR_MODE=off`) | External ALPR calls are skipped by design; narration and detection still run |
| Stream resolution fails | Server starts without live stream; batch endpoints still functional |
| Camera degraded (blur, low light, overexposed) | Perception quality monitor tightens thresholds, skips vision enrichment |
| Public-thumbnail signing enabled but URL token invalid | Server denies public thumbnail request (403) and audit-logs denial |
| LLM rate limit exhausted | Token bucket refuses calls proactively; circuit breaker opens after 3 failures |
| Both LLM providers down | All narration/enrichment returns None; detection continues unaffected |
| Network outage (edge→cloud) | Events queue locally; drain on reconnect with exponential backoff |
| Operator stops providing feedback | Drift monitor reports "insufficient" for low-label buckets; no false precision |
| Object loses tracking mid-episode | Episode flushes on idle timeout; new untracked detections degrade to per-frame dedup |

### 2.4 Acceptance Criteria

| ID | Criterion |
|---|---|
| AC-01 | System detects pedestrian-vehicle proximity events with risk classification from a live video stream |
| AC-02 | Events are deduped per tracked pair; same pair emits only once per episode |
| AC-03 | Risk thresholds adapt to scene context (highway vs urban vs parking) |
| AC-04 | Thumbnails are PII-redacted before any external egress |
| AC-05 | Operator can submit tp/fp feedback and drift monitor updates precision |
| AC-06 | Active learning samples are exported for labeling |
| AC-07 | LLM narration degrades gracefully when unavailable |
| AC-08 | Edge→cloud delivery uses HMAC-signed batches with at-least-once semantics |
| AC-09 | AI coaching agent produces structured JSON coaching note given an event_id |
| AC-10 | AI investigation agent correlates events with history and produces root-cause hypothesis |
| AC-11 | Road summary API returns aggregated stats across vehicles |
| AC-12 | Data retention sweep removes expired artifacts automatically |
| AC-13 | Audit trail logs all access to unredacted thumbnails, feedback, and agent invocations |

---

## 3. Technical Documentation

### 3.1 Architecture & Integration

See `docs/architecture.md` for the full edge/cloud diagram and data flow.

**Key integration points:**
- `road_safety/server.py` — FastAPI app, main orchestrator
- `cloud/receiver.py` — separate FastAPI ingest app (port 8001)
- `road_safety/services/llm.py` — Anthropic / Azure OpenAI with failover
- `road_safety/services/agents.py` — tool-calling AI agents
- `road_safety/services/registry.py` — multi-vehicle registry

### 3.2 Data Schema

**Primary entities:**

| Entity | Storage | Key Fields |
|---|---|---|
| Safety Event | In-memory list (edge), SQLite (cloud) | `event_id`, `vehicle_id`, `event_type`, `risk_level`, `ttc_sec`, `distance_m`, `confidence` |
| Feedback | JSONL (`data/feedback.jsonl`) | `event_id`, `verdict` (tp/fp), `note`, `operator_ts` |
| Active Learning Sample | JSON files (`data/active_learning/pending/`) | `event_id`, `reason`, `confidence`, `event_json` |
| Audit Record | JSONL (`data/audit.jsonl`) | `ts`, `action`, `resource`, `actor`, `outcome`, `ip` |
| Road Vehicle State | In-memory (`road_safety/services/registry.py`) | `vehicle_id`, `road_id`, `driver_id`, `safety_score`, `events_by_risk` |

### 3.3 API Interfaces

See `docs/requirements/Road_Safety_TRD.md` §9 for the endpoint contract and auth expectations.

### 3.4 Testing Strategy

| Layer | Coverage |
|---|---|
| **Unit (tools/eval_detect.py)** | Precision/recall/F1 against labeled ground truth; per-risk and per-event-type breakdowns |
| **Suite (tools/eval_detect.py --suite)** | Multi-clip evaluation with markdown report and regression detection (>3% drop) |
| **Compare (tools/eval_detect.py --compare)** | A/B comparison of two evaluation runs to flag regressions |

---

## 4. Operations & Lifecycle

### 4.1 Module Inventory

| Module | Purpose | Status |
|---|---|---|
| `road_safety/core/stream.py` | Video stream reader (HLS/RTSP/webcam/file) | Shipped |
| `road_safety/core/detection.py` | YOLOv8 + ByteTrack detection and tracking | Shipped |
| `road_safety/server.py` | FastAPI orchestrator, SSE, REST APIs | Shipped |
| `road_safety/services/llm.py` | LLM narration, enrichment, chat with failover | Shipped |
| `road_safety/core/context.py` | Scene-adaptive risk thresholds | Shipped |
| `road_safety/core/egomotion.py` | Optical flow ego-motion estimation | Shipped |
| `road_safety/core/quality.py` | Perception quality monitor | Shipped |
| `road_safety/services/redact.py` | PII redaction (face/plate blur, plate hash) | Shipped |
| `road_safety/integrations/edge_publisher.py` | HMAC-signed edge→cloud delivery | Shipped |
| `cloud/receiver.py` | Cloud ingest with dedup | Shipped |
| `road_safety/api/feedback.py` | Operator feedback API | Shipped |
| `road_safety/services/drift.py` | Drift monitor + active learning sampler | Shipped |
| `road_safety/integrations/slack.py` | Tiered Slack alerting | Shipped |
| `road_safety/services/digest.py` | Hourly/daily digest schedulers | Shipped |
| `road_safety/services/agents.py` | AI coaching, investigation, report agents | Shipped |
| `road_safety/services/registry.py` | Multi-vehicle registry + driver scoring | Shipped |
| `road_safety/services/llm_obs.py` | LLM cost/latency observability | Shipped |
| `road_safety/compliance/retention.py` | GDPR-compliant data retention sweeps | Shipped |
| `road_safety/compliance/audit.py` | Compliance audit trail | Shipped |
| `tools/eval_detect.py` | Precision/recall evaluation harness | Shipped |
| `tools/analyze.py` | Offline batch analysis | Shipped |
| `frontend/src/*` + `static/*` fallback | Operator dashboard UI | Shipped |

### 4.2 Rollout Strategy

- **Feature Flags:** LLM enrichment, Slack alerts, edge publisher, and agents are all configurable via environment variables. Each subsystem can be enabled/disabled independently.
- **Rollback Plan:** Each module is additive and loosely coupled. Disabling any module (by removing its env vars) does not affect core detection and event emission.

### 4.3 Post-Release Monitoring

| Metric | Source | Alert Threshold |
|---|---|---|
| Detection precision | `/api/drift` | < 70% over 50 labels |
| LLM error rate | `/api/llm/stats` (admin token) | > 20% |
| LLM cost/hour | `/api/llm/stats` (admin token) | > $1.00/hour |
| Event emission rate | `/api/live/status` | 0 events in 30 minutes (if stream is active) |
| Perception state | `/api/live/perception` | Degraded state > 10 minutes |
| Active learning queue | `data/active_learning/pending/` | > 500 pending samples |

---

## 5. Challenges & Solutions Reference

See `docs/challenges.md` for detailed industry challenge mapping:

| # | Challenge | Key Solution |
|---|---|---|
| 1 | False Positives & Alert Fatigue | Scene-adaptive thresholds, episode model, quality gating |
| 2 | Edge/Cloud Latency & Bandwidth | Edge-first architecture, 2000-10000x bandwidth reduction |
| 3 | LLM Reliability in Production | Multi-provider failover, circuit breaker, self-consistency |
| 4 | Privacy & Regulatory Compliance | Dual thumbnails, plate hashing, DSAR gating, audit, retention |
| 5 | Model Drift & Continuous Improvement | Rolling precision, active learning, disputed sampling |
| 6 | Scaling to Multi-Vehicle Roads | Road identity, driver scoring, road-wide aggregation |
| 7 | AI Agent Orchestration | Bounded tools, structured output, hard stops, observability |
