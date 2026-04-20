# Production-scale improvement blueprint

This document reorganizes the existing improvement proposals into a single
execution blueprint for a real deployment at national scale.

It is designed to answer three questions quickly for each item:

1. Why this is needed now.
2. What benefit it gives.
3. What concrete problem it removes.

It also groups work by category and impact so planning is easier.

---

## Scale assumptions (planning baseline)

Use these as order-of-magnitude planning numbers:

- **Fleet size:** 1,000 to 10,000 active cameras.
- **Frame processing target:** 2 FPS per camera at the edge.
- **Total processed frames:** 2,000 to 20,000 frames/sec fleet-wide.
- **Safety events (average):** 0.2 to 2 events/min/camera (bursty).
- **Event throughput:** 3.3 to 333 events/sec fleet-wide.
- **Event payload (JSON):** 2 KB to 6 KB each (without thumbnails).
- **Thumbnail payload:** typically 20 KB to 120 KB each.

Implication: most bottlenecks will come from **burst behavior**, not steady
state averages.

---

## Bottleneck map (nationwide operation)

### 1) Control-plane security bottlenecks

- Unauthenticated state mutation endpoints can be abused at internet speed.
- Shared static admin token does not support per-user attribution or revocation.
- LLM-facing endpoints without strict rate limits can exhaust budget quickly.

### 2) Real-time event transport bottlenecks

- SSE reconnects without deterministic replay can lose context during network
  flaps.
- Proxy buffering and idle timeout behavior can produce "looks connected but
  stale" dashboards.

### 3) Runtime concurrency bottlenecks

- Thread-to-async boundary mistakes can cause loop-binding runtime failures.
- Shared mutable buffers used by producer thread and SSE readers can race under
  load.

### 4) Edge-to-cloud delivery bottlenecks

- Without replay protection, signed payloads can be replayed within timestamp
  window.
- Queue growth during prolonged outage can consume disk and degrade device
  health.

### 5) Contract and schema bottlenecks

- Hand-maintained FE types drift from backend response shape.
- Untyped SSE payload parse path can fail silently and degrade UI correctness.

### 6) Observability and incident response bottlenecks

- No end-to-end trace context means slow-path root cause analysis is weak.
- Missing high-signal metrics prevents SLO-driven scaling decisions.

### 7) Frontend scalability bottlenecks

- Frequent state updates during event bursts can block input responsiveness.
- No route-level crash isolation can blank entire operator surfaces.

### 8) MLOps and governance bottlenecks

- Drift signals without closed retraining loop do not reduce model risk.
- Compliance artifacts missing for regulated deployments create launch risk.

---

## Improvement categories with impact

Impact levels used below:

- **Critical:** security/compliance outage or data integrity risk.
- **High:** reliability, scale, or major cost risk.
- **Medium:** quality and maintainability acceleration.
- **Low:** useful polish, defer when roadmap is busy.

### Category A: Security and access control (Critical)

#### A1. Enforce admin auth on watchdog delete endpoints

- **Why needed:** state-destructive endpoints are currently exposed without
  strong auth checks.
- **Benefit:** prevents unauthorized clearing of operator findings.
- **Problem solved:** remote or local actor cannot wipe monitoring state.
- **Impact:** Critical.
- **Source:** `integration.md` B1.

#### A2. Add per-endpoint rate limiting for `/chat` and `/api/agents/*`

- **Why needed:** these routes can trigger expensive LLM calls.
- **Benefit:** protects availability and cost envelope during abuse.
- **Problem solved:** prevents budget drain and degraded response for real users.
- **Impact:** Critical.
- **Source:** `integration.md` R6.1.

#### A3. Move admin auth from static shared token to short-lived JWT

- **Why needed:** static token has no user identity, no revocation granularity.
- **Benefit:** per-user attribution, key rotation, and stronger audit quality.
- **Problem solved:** forensic blind spots and high blast radius on secret leak.
- **Impact:** High.
- **Source:** `integration.md` R4.1.

---

### Category B: Real-time transport and protocol resilience (High)

#### B1. Implement SSE `id` + `Last-Event-ID` replay

- **Why needed:** disconnect/reconnect currently risks missing event windows.
- **Benefit:** deterministic continuity after brief network/proxy failures.
- **Problem solved:** stale dashboards and context loss during reconnect.
- **Impact:** High.
- **Source:** `integration.md` R1.1.

#### B2. Heartbeat timeout watchdog in FE + backend keepalive cadence

- **Why needed:** silent stalls are not reliably detected by `onerror` alone.
- **Benefit:** auto-recovery from half-open or frozen stream sessions.
- **Problem solved:** "connected but frozen" UI state.
- **Impact:** High.
- **Source:** `integration.md` R1.2, `frontend.md` R3.

#### B3. Proxy hardening for SSE paths

- **Why needed:** buffering and edge timeout defaults vary by provider.
- **Benefit:** stable long-lived streaming behavior in real networks.
- **Problem solved:** delayed or coalesced event delivery under proxies.
- **Impact:** Medium.
- **Source:** `integration.md` R1.4.

---

### Category C: Backend runtime integrity and modularity (Critical/High)

#### C1. Fix thread-to-async loop crossing for LLM path

- **Why needed:** loop-bound locks and wrong scheduling API cause runtime errors.
- **Benefit:** stable event enrichment under burst and contention.
- **Problem solved:** cross-event-loop failure class in production.
- **Impact:** Critical.
- **Source:** `backend.md` B2.

#### C2. Protect shared recent-event buffer with lock or deque pattern

- **Why needed:** producer and readers access shared list concurrently.
- **Benefit:** deterministic replay snapshots and no torn reads.
- **Problem solved:** race conditions under high event rate.
- **Impact:** High.
- **Source:** `backend.md` B3.

#### C3. Split `server.py` into routers + lifespan-managed app factory

- **Why needed:** oversized module mixes routing, lifecycle, and orchestration.
- **Benefit:** cleaner ownership boundaries, easier testing, safer iteration.
- **Problem solved:** high change risk and brittle startup side effects.
- **Impact:** High.
- **Source:** `backend.md` R2.

---

### Category D: Edge-cloud delivery correctness (High)

#### D1. Replay protection via nonce cache on cloud receiver

- **Why needed:** timestamp-only freshness check allows replay in acceptance
  window.
- **Benefit:** stronger integrity guarantees for signed ingestion.
- **Problem solved:** duplicate malicious submissions within valid timestamp.
- **Impact:** High.
- **Source:** `integration.md` R8.1.

#### D2. Outbox sequence and watermark semantics

- **Why needed:** current dedupe handles duplicates but not ordering quality.
- **Benefit:** out-of-order detection and better fleet reliability analytics.
- **Problem solved:** invisible late-arriving data and weak recovery diagnosis.
- **Impact:** High.
- **Source:** `integration.md` R9.1.

#### D3. Queue capacity guardrails and explicit shedding policy

- **Why needed:** prolonged outage can create unbounded local queue growth.
- **Benefit:** predictable disk usage and controlled degradation.
- **Problem solved:** edge node disk exhaustion during cloud downtime.
- **Impact:** High.
- **Source:** `integration.md` R9.2.

---

### Category E: Contracts, typing, and integration safety (High)

#### E1. Generate FE API types from backend OpenAPI

- **Why needed:** hand-maintained types drift from backend reality.
- **Benefit:** compile-time contract safety and faster API refactors.
- **Problem solved:** silent data-shape mismatch bugs in UI.
- **Impact:** High.
- **Source:** `integration.md` R2.1, `frontend.md` R6.

#### E2. Runtime-validate SSE payloads

- **Why needed:** typed cast after `JSON.parse` is not runtime-safe.
- **Benefit:** robust handling of malformed or schema-shifted messages.
- **Problem solved:** hidden rendering bugs from invalid event frames.
- **Impact:** High.
- **Source:** `integration.md` R2.2, `frontend.md` R7.

#### E3. Contract tests against live OpenAPI

- **Why needed:** API drift should fail CI before deployment.
- **Benefit:** early detection of response/status regressions.
- **Problem solved:** integration breakage discovered only at runtime.
- **Impact:** Medium.
- **Source:** `integration.md` R3.1, R3.2.

---

### Category F: Observability and SRE (High)

#### F1. End-to-end OpenTelemetry trace context

- **Why needed:** without distributed traces, latency root cause is guesswork.
- **Benefit:** cross-system request lineage from browser to cloud ingest.
- **Problem solved:** inability to explain slow path and reliability incidents.
- **Impact:** High.
- **Source:** `integration.md` R7.1, R7.2, `backend.md` R3.

#### F2. Prometheus metrics + custom fleet KPIs

- **Why needed:** route metrics alone do not expose event-plane behavior.
- **Benefit:** SLO-driven operations and better capacity planning.
- **Problem solved:** blind spots in subscriber count, queue depth, dedupe rate.
- **Impact:** High.
- **Source:** `integration.md` R12.2, R13.1.

#### F3. Structured logging convergence

- **Why needed:** mixed print/log style hinders queryability and correlation.
- **Benefit:** searchable, machine-friendly logs with trace correlation.
- **Problem solved:** slower incident triage and weaker forensics.
- **Impact:** Medium.
- **Source:** `backend.md` R7.

---

### Category G: Frontend runtime resilience and UX under load (High)

#### G1. Move SSE state model to external store + transition updates

- **Why needed:** high-frequency state pushes can block operator input.
- **Benefit:** smoother interaction during event bursts.
- **Problem solved:** chat/admin control stutter under stream load.
- **Impact:** High.
- **Source:** `frontend.md` R1, R2.

#### G2. Route-level error boundaries

- **Why needed:** unhandled render error can blank full app.
- **Benefit:** localized failure containment and graceful fallback.
- **Problem solved:** total UI outage from single-page rendering fault.
- **Impact:** High.
- **Source:** `frontend.md` R4.

#### G3. Sanitize LLM-rendered output

- **Why needed:** LLM text is untrusted and may become HTML-rendered later.
- **Benefit:** XSS risk reduction and future-safe rendering path.
- **Problem solved:** script injection via generated assistant text.
- **Impact:** High.
- **Source:** `frontend.md` R5, `backend.md` R6.

---

### Category H: Model operations and compliance readiness (High/Medium)

#### H1. Close drift-to-retraining loop with model registry

- **Why needed:** drift metrics alone do not improve model quality.
- **Benefit:** measurable, governed model promotion process.
- **Problem solved:** persistent performance decay without corrective pipeline.
- **Impact:** High.
- **Source:** `backend.md` R11.

#### H2. Prompt caching on dominant LLM instruction blocks

- **Why needed:** static system prompts are repeatedly billed during bursts.
- **Benefit:** significant recurring inference cost reduction.
- **Problem solved:** avoidable token spend in high-volume periods.
- **Impact:** High.
- **Source:** `backend.md` R1.

#### H3. EU AI Act and OWASP LLM control mapping artifacts

- **Why needed:** regulated rollout requires documentation and control evidence.
- **Benefit:** lower legal/compliance launch risk and clearer audit posture.
- **Problem solved:** delayed pilots due to missing governance package.
- **Impact:** Medium to High (depends on target market).
- **Source:** `backend.md` R10, R6.

---

## Prioritized execution plan by impact

### Phase 0 (0-2 weeks): critical risk closure

1. A1 unauthenticated watchdog delete fix.
2. C1 async loop bridge correctness audit.
3. C2 shared recent-event concurrency fix.
4. A2 rate limiting on LLM-exposed endpoints.
5. D1 replay protection for cloud ingestion.

### Phase 1 (2-6 weeks): reliability and scale foundations

1. B1 SSE replay protocol.
2. B2 heartbeat stall detection FE+BE.
3. C3 backend modularization (`server.py` split).
4. E1 OpenAPI type generation for FE.
5. F2 metrics endpoint and fleet KPIs.

### Phase 2 (6-12 weeks): operational maturity

1. F1 full trace context propagation.
2. D2 outbox sequence/watermark.
3. D3 queue guardrails and shedding.
4. G1 frontend streaming store refactor.
5. H2 prompt caching telemetry rollout.

### Phase 3 (12+ weeks): enterprise/regulatory expansion

1. A3 JWT + JWKS + identity attribution.
2. H1 model registry and shadow promotion flow.
3. H3 compliance packet (risk register, model card, post-market plan).
4. Optional tenant-scale upgrades (Postgres receiver path, OIDC SSO).

---

## Key architecture decisions to preserve

These should stay as-is unless requirements fundamentally change:

- Keep SSE for server-to-client event push (fits traffic shape well).
- Keep edge-first inference architecture (latency/privacy/cost advantages).
- Keep outbox + idempotent consumer semantics (strong practical reliability).
- Keep in-process model inference for single-camera-per-node baseline.

---

## How to use this blueprint

For each work item, require this template in the PR description:

- **Improvement ID:** (example: C1)
- **Category:** (A-H)
- **Why now:** one sentence tied to observed bottleneck.
- **Expected benefit:** measurable metric (latency, cost, error-rate, MTTR).
- **Problem solved:** explicit failure mode removed.
- **Impact:** Critical/High/Medium/Low.
- **Rollback strategy:** one short paragraph.

This keeps execution focused on outcomes, not only implementation details.
