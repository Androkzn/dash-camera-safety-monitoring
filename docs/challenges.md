# Industry Challenges & How We Address Them

This document maps the parts of the fleet safety problem space this project **explicitly targets** to the concrete engineering decisions behind them. It also lists — at the end — the parts of the space this project does **not** address, because an honest scope is more useful to a reviewer than a generous one.

**What this project is**: a forward-facing road-conflict detection component (vehicle↔vehicle and vehicle↔pedestrian near-miss detection) with production-grade false-positive suppression, privacy-by-construction redaction, LLM enrichment resilience, and a drift / active-learning feedback loop. It is designed to be a building block inside a larger fleet platform (DMS + telematics + coaching + insurance).

**What this project is not**: a complete commercial fleet-safety product. See [§8 Out of scope](#8-out-of-scope-deliberately) for what would need to be built or integrated to make it one.

---

## 1. False Positives & Alert Fatigue

### The Challenge

False positives are the #1 complaint from road operators. Basic detection systems fire on every close proximity event without understanding context — a stop sign that doesn't apply to the driver's lane, normal close-quarters maneuvering in a parking lot, or highway following distances that look dangerously close at city-street thresholds. The result: drivers learn to ignore the system, and the safety value drops to zero.

Even state-of-the-art systems generate enough false alerts that inconsistent coaching and reduced driver trust remain the dominant operational problems across the industry.

### How We Address It

| Solution | Module | Mechanism |
|---|---|---|
| **Multi-gate TTC** | `road_safety/core/detection.py` (`estimate_ttc_sec`, `estimate_pair_ttc`) | Time-to-collision is published only when the trailing window satisfies all of: ≥4 samples spanning ≥1.5 s, monotonic bbox-height growth, absolute pixel delta above the jitter floor, non-trivial track-centre motion, and scale ratio above the noise floor. Pair-TTC additionally requires monotonic distance decrease, ≥1 track moving, and minimum closing rate. Aligned with SSAM / SAFE-UP / PET methodology. |
| **Ego-relative motion gate** | `road_safety/server.py`, `road_safety/core/egomotion.py` | TTC is discarded when neither track shows a positive approach residual against the optical-flow ego-motion estimate. Bbox jitter on stationary objects cannot fire a TTC-driven alarm. |
| **Depth-aware proximity** | `road_safety/server.py`, `road_safety/core/detection.py` | Vehicle-vehicle interactions are gated on monocular 3D inter-object distance, not image-plane bbox proximity. Two cars more than 8 m apart in depth are not a close interaction even when their bboxes overlap due to perspective. |
| **Scene-adaptive thresholds** | `road_safety/core/context.py` | Classifies the scene as urban / highway / parking / unknown from rolling detection density + ego-speed proxy. Highway widens TTC thresholds (2.8 s high) because 65 mph needs more reaction time; parking tightens them (0.8 s high) because close proximity at 3 mph is normal. |
| **Speed-aware risk floor** | `road_safety/server.py` (`_classify_with_scene`) | When ego speed is below 2 m/s and no track is actively approaching, risk is capped at medium. Close-quarters proximity in stopped traffic is normal, not a conflict. A genuine approach by another moving object overrides the cap. |
| **Sustained-risk episode model** | `road_safety/server.py` (`Episode.final_risk`) | A track-pair interaction opens an episode, accumulates risk-frame counts across frames, and on flush downgrades the peak risk if it lacks sustained support (≥2 high-risk frames over ≥1.0 s for high-tier emission). The peak-severity thumbnail is preserved regardless. |
| **Per-pair cooldown** | `road_safety/server.py` | After an episode emits, the pair enters an 8-second cooldown. Same pair reappearing immediately (e.g. traffic-light cycle) does not re-fire. |
| **Perception-quality gating** | `road_safety/core/quality.py` | When the camera is degraded (low light, blur, overexposure), TTC and pixel-distance multipliers tighten conservatively and low-confidence events are suppressed rather than generating noisy alerts. |
| **High-risk Slack quality gate** | `road_safety/integrations/slack.py` | Immediate Slack alerts fire only when the underlying episode clears minimum duration, sustained-frame, and confidence thresholds. Events that fail the gate route to the hourly medium digest — never silently dropped. |
| **Operator feedback loop** | `road_safety/api/feedback.py`, `road_safety/services/drift.py` | Operators can mark events as true or false positive. Drift monitor tracks rolling precision and posts a Slack regression warning when precision degrades; disputed events feed the active-learning sampler for relabeling. |

**Key design principle:** alerting is gated by independent layers — multi-gate TTC, ego-motion residual, depth-aware proximity, scene-adaptive thresholds, low-speed floor, sustained-risk episode model, and Slack quality gate. A real conflict satisfies all of them; a noisy frame fails at the first.

---

## 2. Edge/Cloud Latency & Bandwidth

### The Challenge

Real-time safety requires sub-second latency, but edge devices (in-vehicle hardware) have limited compute. Cloud has unlimited power but adds 30-80ms round-trip on 4G LTE. A single 1080p dashcam streaming continuously over 8 hours generates ~28 GB/day — transmitting that from thousands of vehicles is economically impossible on cellular networks. Even event-based recording produces 1-2 GB/day/camera.

### How We Address It

| Solution | Module | Mechanism |
|---|---|---|
| **Edge-first architecture** | `road_safety/integrations/edge_publisher.py`, `cloud/receiver.py` | All perception, tracking, risk classification, and PII redaction run on-device. Only typed JSON events (~2 KB) + redacted thumbnails (~8 KB) cross the wire. Bandwidth reduction: **2,000-10,000x** vs raw video. |
| **Lightweight model** | `road_safety/core/detection.py` | YOLOv8n (nano variant) — the smallest YOLO model, designed for edge inference. Runs comfortably at 2 fps on laptop CPU. |
| **HMAC-signed batched delivery** | `road_safety/integrations/edge_publisher.py` | Events queue locally in append-only JSONL. Batches of up to 20 events are HMAC-signed and POSTed together. Survives network outages — queue drains on reconnect with exponential backoff. |
| **Selective LLM enrichment** | `road_safety/server.py`, `road_safety/services/llm.py` | Vision enrichment (ALPR) is policy-gated (`ROAD_ALPR_MODE=third_party`) and further skipped when perception is degraded (blurry image = wasted API call) or for low-risk events (review SLA is weekly batch, ALPR adds no value). |
| **At-least-once delivery** | `road_safety/integrations/edge_publisher.py`, `cloud/receiver.py` | Write-ahead JSONL queue on edge; `event_id` dedup on cloud. No data loss during connectivity gaps. |

**Key design principle:** the edge boundary is the privacy and bandwidth boundary. Everything identifiable stays on-device; only event metadata and blurred thumbnails leave.

---

## 3. LLM Reliability in Production

### The Challenge

LLM integration in safety-critical systems faces compounding risks: rate limiting degrades service, hallucination in narration or enrichment produces incorrect information, costs scale linearly with event volume, and single-provider outages take down the entire AI layer.

Research shows multimodal LLMs generate hallucinatory content when visual input is ambiguous or degraded — particularly problematic for OCR tasks like plate reading, where models fall back on linguistic priors rather than accurate visual-textual reasoning.

### How We Address It

| Solution | Module | Mechanism |
|---|---|---|
| **Multi-provider failover** | `road_safety/services/llm.py` | If the primary LLM provider (Anthropic or Azure OpenAI) returns an error, the completion path automatically retries through the secondary provider. Zero operator intervention needed. |
| **Client-side rate budget** | `road_safety/services/llm.py` (`_TokenBucket`) | A token-bucket rate limiter (3 req/min sustained) refuses LLM calls *before* they trigger 429 errors. Cheaper than handling failures and faster to recover. |
| **Circuit breaker** | `road_safety/services/llm.py` | Vision enrichment tracks consecutive failures. After 3 failures, the breaker opens for 60 seconds, halving API load during rate-limit storms. |
| **Self-consistency for ALPR** | `road_safety/services/llm.py` (`_merge_self_consistency`) | Two independent vision calls at different temperatures (0.0 and 0.3). If plate readings disagree, output is set to null + "partial" readability rather than guessing. Eliminates hallucinated plates. |
| **Prompt injection defense** | `road_safety/services/llm.py` | OWASP LLM01:2025 compliant: image content is marked as UNTRUSTED USER DATA. Injection patterns in vision output are detected and scrubbed. |
| **Graceful degradation** | `road_safety/services/llm.py`, `road_safety/server.py` | No API key = templated summaries. Rate budget exhausted = skip silently. Circuit open = skip silently. The system never stops detecting events because the LLM is down. |
| **Cost observability** | `road_safety/services/llm_obs.py` | Every LLM call is instrumented: input/output tokens, latency, estimated USD cost, success/failure, skip reason. Exposed via `/api/llm/stats` with P50/P95 latency and error rate. |

**Key design principle:** the LLM is an enrichment layer, not a critical path. Detection and risk classification work with zero LLM calls. Narration and enrichment add value when available and degrade silently when not.

---

## 4. Privacy & Regulatory Compliance (GDPR/CCPA)

### The Challenge

In-vehicle dashcams capture faces, license plates, and location data — all classified as PII under GDPR and CCPA. Cumulative GDPR enforcement has exceeded €5.8 billion across 2,245+ actions since 2018, with individual penalties reaching €1.2 billion (Meta, 2025). License plates create tracking profiles that trigger data protection obligations. Driver-facing cameras raise biometric data concerns in jurisdictions requiring explicit consent. Organizations must demonstrate what personal data they hold, who accessed it, and when (GDPR Art. 30).

### How We Address It

| Solution | Module | Mechanism |
|---|---|---|
| **Dual-thumbnail architecture** | `road_safety/services/redact.py` | Every event produces two thumbnails: `{event_id}.jpg` (internal, unredacted, local disk only) and `{event_id}_public.jpg` (faces blurred, plates blurred, safe for shared channels). SSE, Slack, and cloud payloads use only the public version. The internal copy stays local unless optional external vision enrichment is enabled. |
| **Optional signed public-thumbnail access** | `road_safety/server.py` | `_public` thumbnails can require short-lived HMAC query params (`exp` + `token`) when `ROAD_PUBLIC_THUMBS_REQUIRE_TOKEN=1`. Access allow/deny decisions are audit-logged. |
| **Face blurring** | `road_safety/services/redact.py` | Upper 35% of every person bounding box is Gaussian-blurred. Deliberately over-blurs — false-redact is the correct failure mode vs false-leak. |
| **Plate blurring** | `road_safety/services/redact.py` | Lower-middle strip (55-95% height, 15% horizontal inset) of every vehicle bounding box is blurred. |
| **Plate text hashing** | `road_safety/services/redact.py` (`hash_plate`) | Raw plate text from ALPR is immediately converted to a salted SHA-256 hash (`plate_{hash[:16]}`). Enables cross-event correlation ("same vehicle seen 3 times in 20 min") without storing the actual plate string anywhere. Salt is per-deployment. |
| **DSAR-gated access** | `road_safety/server.py` | Unredacted thumbnails require an `X-DSAR-Token` header. Without the token, access is denied with a 403. Denied attempts are audit-logged. |
| **Audit trail** | `road_safety/compliance/audit.py` | Every access to sensitive resources is logged: unredacted thumbnail access, feedback submissions, active-learning exports, chat queries, agent invocations. Each record includes timestamp, actor, action, resource, outcome, and IP. GDPR Art. 30 / SOC 2 ready. |
| **Configurable data retention** | `road_safety/compliance/retention.py` | Automatic hourly sweeps delete data past retention windows: thumbnails (30d), feedback (90d), active-learning samples (60d), outbound queue (7d). All configurable via environment variables. GDPR Art. 5(1)(e) compliance — data kept only as long as necessary. |
| **PII scrub at LLM boundary (structural)** | `road_safety/services/llm.py` (`_hash_and_strip_plate`) | `enrich_event()` hashes the plate string and **removes** `plate_text` / `plate_state` from the returned dict before it ever reaches a shared buffer. A future caller that forgets to scrub at egress cannot leak, because the raw plate was never in the event object to begin with. `server.py` keeps a defence-in-depth `pop()` as a second line. |
| **Optional external vision enrichment disclosure** | `road_safety/server.py` | External ALPR is policy-gated: set `ROAD_ALPR_MODE=third_party` to allow the Anthropic vision pass; default is `off`. When enabled, the internal thumbnail is sent to Anthropic for ALPR and must be treated as a processor integration (not zero-external-PII). |

**Key design principle:** shared channels stay redacted by default, and the raw plate never enters any in-memory event buffer. Privacy is enforced at ingest, not at egress — the code path makes a leak structurally impossible, not just procedurally discouraged.

**Jurisdictional note:** this redaction + retention implementation is calibrated for GDPR and CCPA. A DMS extension (driver-facing camera) would fall under **BIPA** in Illinois and would require an additional consent-capture module before being production-safe. See the *Note on Privacy Jurisdiction* below.

---

## 5. Model Drift & Continuous Improvement

### The Challenge

Computer vision models degrade in production. Weather changes, new camera angles, seasonal lighting shifts, road expansion to new geographies — all cause the training distribution to diverge from production data. Without monitoring, precision silently degrades until operators lose trust. Retraining requires labeled data, which is expensive to collect and curate.

Over 70% of organizations report experiencing substantial data drift within the first six months of deploying ML models to production, yet most road operators lack any drift monitoring at all.

### How We Address It

| Solution | Module | Mechanism |
|---|---|---|
| **Rolling precision monitoring** | `road_safety/services/drift.py` (`DriftMonitor`) | Joins operator feedback (tp/fp verdicts) with emitted events. Computes rolling-window precision, sliced by risk level and event type. Identifies which specific event type or risk band is driving degradation. |
| **Trend detection** | `road_safety/services/drift.py` | Compares current window against the prior non-overlapping window. Reports "improving", "stable", or "degrading" with a +/- 5% noise band. |
| **Slack alerts on precision drop** | `road_safety/server.py`, `road_safety/services/drift.py` | When precision drops below 70% threshold with sufficient labels, a Slack warning fires identifying the worst-performing event type. |
| **Decision-boundary active learning** | `road_safety/services/drift.py` (`ActiveLearningSampler`) | Events with confidence in [0.35, 0.50] are sampled at 50% probability — these are the examples the model is most uncertain about, providing the highest information-per-label for retraining. |
| **Disputed-sample capture** | `road_safety/services/drift.py` | When an operator marks verdict=fp, the event is always captured for relabeling. Confidently-wrong events are the second-highest-value training data after decision-boundary samples. |
| **Label Studio / CVAT export** | `road_safety/services/drift.py` | Pending active-learning samples are bundled into a zip with a manifest JSON, ready for direct import into standard labeling tools. Internal (unredacted) thumbnails are used for labeling fidelity. |
| **Minimum-bucket guards** | `road_safety/services/drift.py` | Buckets with fewer than 3 labels report "insufficient" instead of a noisy precision number. 1/1 is not 100% precision — it's one data point. |
| **Feedback-coverage metric (selection-bias guard)** | `road_safety/services/drift.py` (`DriftReport.feedback_coverage`) | Operator feedback is inherently biased — operators label alerts that bothered them, not a uniform sample. A precision of 0.9 from 5% coverage is a weaker signal than 0.9 from 60% coverage. The drift report surfaces `feedback_coverage`, `labeled_events`, and `total_events_in_window` so dashboards and alert logic can weight precision by how representative the sample is. |

**Key design principle:** the feedback loop is a first-class feature, not an afterthought. Operator verdicts flow directly into precision monitoring and training data selection, but the system reports coverage honestly so a high-precision-from-thin-sample result doesn't masquerade as health.

---

## 6. Scaling to Multi-Vehicle Roads

### The Challenge

Scaling from a single camera to thousands of vehicles requires vehicle identity, road-wide aggregation, driver scoring, and multi-tenant isolation. Industry deployments operate over a million video systems concurrently — the data model must support road-scale operations from day one.

The video telematics market reached ~6.1 million active units in North America in 2024, projected to reach ~13.8 million by 2029 (and ~17 million when North America + Europe are combined). Traditional monolithic systems designed for small fleets cannot handle modern deployments.

### How We Address It

| Solution | Module | Mechanism |
|---|---|---|
| **Vehicle/road identity** | `road_safety/services/registry.py`, `road_safety/server.py` | Every event carries `vehicle_id`, `road_id`, and `driver_id` from environment configuration. Events are attributable to a specific vehicle and driver from the moment they are created. |
| **Per-vehicle state tracking** | `road_safety/services/registry.py` (`RoadRegistry`) | In-memory registry maintains per-vehicle event counts (by risk and type), safety scores, and feedback precision. |
| **Driver safety scoring** | `road_safety/services/registry.py`, `road_safety/server.py` | Decaying penalty model: high-risk events deduct 10 points, medium 3, low 1, from a max score of 100. Recovery runs on a scheduled loop controlled by `ROAD_SCORE_DECAY_INTERVAL_SEC` (0.5 points/interval by default). |
| **Road-wide aggregation API** | `road_safety/server.py` | `/api/road/summary` provides aggregate event counts, risk breakdowns, and identifies the lowest-scoring vehicle. `/api/road/drivers` ranks drivers worst-first for manager attention. |
| **Edge/cloud split** | `road_safety/integrations/edge_publisher.py`, `cloud/receiver.py` | Each vehicle runs its own edge node. Events flow to a central cloud receiver via HMAC-signed HTTPS. Cloud deduplicates on `event_id`. |

**Key design principle:** single-vehicle and multi-vehicle deployments use the same data model. Adding vehicles is a configuration change, not a code change.

---

## 7. Operator-Assist Agents (capability, not a top industry pain)

### Framing

Agent orchestration is **not** currently among the top operational complaints fleet safety managers raise. That list is dominated by alert fatigue, driver coaching workflow, and insurance/claim evidence — see §8 for the items this project doesn't yet address. Agents are included here as a **forward-looking capability**, not because fleets lose sleep over pilot failure rates today.

The engineering challenge this section *does* address is narrower and real: when agents are included, they fail when given too many tools, unbounded step budgets, or unstructured output contracts. The patterns below avoid those failure modes so the agents already in this codebase degrade safely and stay cheap.

### How We Address It

| Solution | Module | Mechanism |
|---|---|---|
| **Single-responsibility agents** | `road_safety/services/agents.py` | Three focused agents, each with a bounded tool set: Coaching (3 tools), Investigation (5 tools), Report (5 tools). No agent has more than 5 tools — well below the overload threshold. |
| **Structured JSON output** | `road_safety/services/agents.py` | Each agent's system prompt specifies an exact JSON schema for the output. No prose, no markdown — structured data that downstream systems can consume programmatically. |
| **Idempotent tool calls** | `road_safety/services/agents.py` | Every tool is a pure function: `get_event`, `get_policy`, `get_feedback`, `get_drift_report`, `count_by_type`. Same input always produces the same output. |
| **Hard stop condition** | `road_safety/services/agents.py` | Maximum 5 iteration steps. If the agent hasn't produced a final answer by step 5, it returns with what it has rather than looping indefinitely. |
| **Observability** | `road_safety/services/llm_obs.py`, `road_safety/compliance/audit.py` | Agent LLM calls are instrumented with the same cost/latency tracking as all other LLM calls. Agent invocations are audit-logged with the event_id being investigated. |
| **Coaching agent** | `road_safety/services/agents.py` | Given a safety event, retrieves the event details and road policy, then generates a structured coaching note: what happened, why it matters, what the driver should do differently, and the relevant policy reference. |
| **Investigation agent** | `road_safety/services/agents.py` | Correlates an event with recent similar events, operator feedback, and drift data to produce a root-cause hypothesis with confidence level. |
| **Report agent** | `road_safety/services/agents.py` | Queries event counts, feedback, and drift data across the session to produce a structured safety summary with top issues and recommendations. |

**Key design principle:** agents are tools for operators, not autonomous decision-makers. They gather evidence, synthesize, and recommend — the operator makes the call.

---

## 8. Out of Scope (deliberately)

A commercial fleet safety product sold by Netradyne, Lytx, Motive, Samsara, Geotab or Verizon Connect does substantially more than this project. Calling these out explicitly is more useful than implying coverage.

| Area | Why fleets care | What's missing here | How we'd extend |
|---|---|---|---|
| **In-cab Driver Monitoring (DMS)** — drowsiness, distraction, phone use, seatbelt | Consistently marketed as the single biggest crash-prevention lever; 80% reductions in distracted-driving incidents are attributed to DMS, not to forward-facing cameras | No inward camera path; no face/gaze/eye-closure model; no phone-in-hand detector | A second pipeline on a driver-facing camera using a face-landmark model (MediaPipe FaceMesh or similar) for EAR-based drowsiness + gaze off-road + phone-object overlap. Paired with a **Driver Privacy Mode** toggle for off-duty periods (BIPA / Samsara-precedent concern). |
| **Insurance / FNOL workflow** — clip evidence, exoneration, carrier integration | Claim-handling cost is the commercial driver for most fleet camera purchases; insurers increasingly demand structured FNOL records | No FNOL payload builder, no carrier shipping, no MP4 clip export — only JPG thumbnails. (An earlier `integrations/fnol.py` skeleton was removed when found unused; resurrect from git history if rebuilding.) | Add a rolling pre/post-roll MP4 buffer on the edge, H.264-encode on event flush, include in the FNOL payload's `clip_url`. Add a "submit to insurer" operator action that POSTs to a configurable carrier endpoint. |
| **Telematics fusion** — GPS, IMU, CAN-bus, harsh-brake, tailgating-in-seconds | Most commercial signals (harsh-brake rate, driver score composition, insurance discounts) come from IMU + GPS, not vision | Ego-speed is an optical-flow **proxy**; `gps`/`imu` fields on events are placeholders | Ingest NMEA + accelerometer from a USB GPS / OBD-II dongle; wire the real speed into the scene classifier, harsh-brake event type, and FNOL payload. Mark speed_source="gps" vs "optical_flow_proxy" so downstream knows. |
| **ELD / DVIR / HOS compliance** | FMCSA-mandated for trucking; ELD integration is table stakes for commercial fleets | No integration | Add adapters for common ELD providers (Motive, Samsara, Geotab ELD APIs) so events can be joined with hours-of-service context. |
| **Driver coaching UX + consent** | Real coaching happens in the cab and on the driver's phone, not in a web dashboard | Coaching agent emits structured JSON; no driver-facing UI, no in-cab audio/haptic feedback, no off-duty consent toggle | In-cab feedback app (phone / tablet) + consent lifecycle (enrollment, off-duty mute, data-access requests). |
| **Multi-tenant auth / RBAC** | Real fleets have operators, safety managers, DPOs, and drivers with different data rights | Single-token DSAR; admin token recently added | JWT-based RBAC; per-tenant rate limits on the cloud receiver; per-vehicle ACLs. |
| **Training / retraining pipeline** | Continuous improvement requires a real MLOps loop | Active-learning sampler exports labeling-ready zips but training is out of scope | Wire the exports into a labeling queue → dataset versioning → retraining → shadow-deploy → evaluation harness loop. |

---

## A Note on Privacy Jurisdiction

§4 Privacy is framed around GDPR because it is the most restrictive widely-enforced regime and "if you pass GDPR you tend to pass the rest." In real US deployments the operative regimes are different:

- **BIPA (Illinois)** — biometric data requires explicit written consent before capture; driver-facing cameras that infer facial features are directly exposed. Recent precedent (*Samsara BIPA litigation*) means any DMS extension to this project would need a consent-flow module.
- **State-by-state recording laws** — two-party consent states (California, Florida, Washington, Illinois, …) affect in-cab audio capture; this project currently captures no audio, which is the right default.
- **CCPA/CPRA (California)** — aligns loosely with GDPR on access / deletion but with different definitions of "sensitive personal information." The dual-thumbnail + salted-hash architecture here satisfies both regimes.
- **CCPA/PIPEDA/UK-GDPR** — similar concepts to GDPR, same mechanisms apply.

Implication: the current redaction and retention implementation is conservative enough for EU and US civilian deployments; a DMS extension would require additional consent plumbing before being production-safe in Illinois.

---

## Summary Matrix

| Challenge | Industry reality | Our coverage | Key modules |
|---|---|---|---|
| **False positives** | Consistently the #1 operational complaint from fleet safety managers | 7-layer gating: TTC gates, ego-motion, depth, scene-adaptive, episodes, perception quality, feedback loop | `detection.py`, `context.py`, `quality.py`, `drift.py` |
| **Edge/cloud bandwidth** | Real cost constraint: 1 GB/day/camera on cellular is unaffordable at fleet scale | 2,000-10,000x reduction, edge-first processing, batched HMAC-signed delivery | `edge_publisher.py`, `cloud/receiver.py` |
| **LLM reliability** | Emerging — few production dashcams run LLMs today, but this is the shape when they do | Multi-provider failover, circuit breaker, self-consistency, rate budget, structural plate hashing at LLM boundary | `llm.py`, `llm_obs.py` |
| **Privacy compliance** | EU/GDPR: mature enforcement. US/BIPA: active litigation around biometric capture | Dual thumbnails, plate hashing at ingest, DSAR gating, audit trail, auto-retention | `redact.py`, `audit.py`, `retention.py` |
| **Model drift** | Real but under-monitored — most fleets rely on vendor-owned models | Rolling precision, trend detection, active learning, **feedback-coverage metric to guard against selection bias** | `drift.py`, `feedback.py` |
| **Fleet scaling** | 6M+ active units in NA alone; data model must support per-vehicle identity from day one | Vehicle/road/driver identity baked in, driver scoring, road-wide aggregation | `registry.py` |
| **Operator-assist agents** | Forward-looking capability, not a top operational pain today | Bounded tools (≤5), structured JSON output, 5-step hard stops, observability | `agents.py` |
| **DMS, FNOL, telematics, ELD, coaching UX** | Dominant commercial value in real fleet products | Out of scope — see §8 for the extension paths | — |
