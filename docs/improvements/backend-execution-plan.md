# Backend execution plan (detailed)

This is the delivery plan for backend improvements only.
It is structured for independent execution from frontend work.

Primary references:

- `docs/improvements/backend.md`
- `docs/improvements/integration.md`
- `docs/improvements/production-scale-plan.md`

---

## Objectives

1. Eliminate critical runtime and authorization failure modes.
2. Make the backend observable and diagnosable at fleet scale.
3. Improve delivery integrity from edge to cloud.
4. Build durable modular architecture for long-term evolution.
5. Prepare governance/compliance foundations for regulated deployments.

---

## Scope

In scope:

- FastAPI route surface, auth gates, lifecycle, and app structure.
- Perception loop runtime safety and thread/async bridging.
- LLM enrichment and narration service cost/reliability controls.
- Edge outbox, cloud ingest integrity, replay and ordering guarantees.
- Backend testing and operational metrics.

Out of scope:

- Frontend rendering and interaction concerns.
- UI-only accessibility work.
- Non-core feature expansions unrelated to stability or scale.

---

## Workstreams

### BE-WS1: Critical security and auth controls

Items:

1. Enforce admin authorization on all watchdog mutation endpoints.
2. Add rate limits on LLM-costly endpoints (`/chat`, `/api/agents/*`).
3. Define and enforce header-only auth policy for admin surfaces.
4. Plan transition from static admin token to JWT/JWKS model.

Why needed:

- Unauthorized mutation and unbounded LLM calls are immediate operational risk.

Success criteria:

- Unauthorized requests to admin mutations are consistently rejected.
- Endpoint rate budgets prevent abuse without impacting normal ops traffic.

Dependencies:

- None for initial hardening.

---

### BE-WS2: Runtime correctness (threading + async safety)

Items:

1. Audit all thread-to-async boundary calls.
2. Use correct cross-thread coroutine scheduling (`run_coroutine_threadsafe`).
3. Remove loop-binding failure patterns in shared async primitives.
4. Add locking/deque protection for shared recent event buffers.

Why needed:

- Cross-loop and race bugs surface under burst load and are hard to debug live.

Success criteria:

- Stress tests show no cross-loop runtime errors or torn replay snapshots.
- SSE consumers and producer thread coexist safely at burst rates.

Dependencies:

- None, but should run before broader refactors.

---

### BE-WS3: Backend modular architecture

Items:

1. Split monolithic `server.py` into domain APIRouters.
2. Move startup/shutdown orchestration into `lifespan` app factory.
3. Replace implicit global state usage with typed app state dependency access.
4. Define module boundaries: API, core pipeline, services, integrations.

Why needed:

- Large mixed-responsibility module increases regression risk and slows delivery.

Success criteria:

- Clear import boundaries and deterministic startup order.
- Router-level tests can run with lightweight dependency injection.

Dependencies:

- BE-WS2 should land first to reduce moving-target risk.

---

### BE-WS4: Contract integrity and schema discipline

Items:

1. Introduce/complete Pydantic response models on key API routes.
2. Normalize event payload models and discriminated unions for SSE.
3. Stabilize OpenAPI as the contract source of truth.
4. Add contract test checks (schemathesis + snapshot discipline).

Why needed:

- Unstructured dict responses create drift and downstream integration failures.

Success criteria:

- Contract changes are explicit and CI-visible.
- SSE and REST payloads have predictable typed structure.

Dependencies:

- BE-WS3 helpful but not strictly required for first wave.

---

### BE-WS5: Observability and SRE instrumentation

Items:

1. Add Prometheus metrics endpoint and custom backend KPIs.
2. Add OpenTelemetry tracing across request and processing stages.
3. Propagate trace context through thread boundaries and outbound calls.
4. Standardize structured logs for correlation with traces/metrics.

Why needed:

- At national fleet scale, no observability means no dependable operations.

Success criteria:

- Slow-path investigations use trace ID end-to-end.
- Core SLOs (latency, queue depth, error rates) are measurable in dashboards.

Dependencies:

- BE-WS2 for safe instrumentation in concurrency-heavy paths.

---

### BE-WS6: Edge-cloud delivery guarantees

Items:

1. Add nonce-based replay protection on cloud receiver.
2. Add outbox sequence and watermark tracking semantics.
3. Add queue size limits and explicit overflow policy.
4. Harden request signing/canonicalization roadmap.

Why needed:

- Scale stresses delivery semantics; duplicates, replays, and ordering must be
  observable and controlled.

Success criteria:

- Replay attempts are rejected.
- Out-of-order and late arrivals are detectable and measured.
- Outages do not cause unbounded edge resource growth.

Dependencies:

- None for initial replay/cache protections.

---

### BE-WS7: LLM cost and reliability controls

Items:

1. Enable prompt caching for high-frequency static system blocks.
2. Expose cache hit/write token telemetry for validation.
3. Keep circuit-breaker/rate-budget protections robust under load.
4. Add safe output and injection hardening controls per security mapping.

Why needed:

- LLM path can become both a cost hotspot and reliability hotspot.

Success criteria:

- Measurable token-cost reduction after caching rollout.
- LLM failure modes degrade gracefully without breaking detection pipeline.

Dependencies:

- BE-WS5 metrics/tracing strongly recommended to validate impact.

---

### BE-WS8: Model lifecycle and compliance readiness

Items:

1. Close drift to retraining loop with registry-backed promotion policy.
2. Define shadow evaluation criteria for candidate model promotion.
3. Produce AI risk register/model card/post-market docs for launch readiness.
4. Map OWASP LLM controls to code-level safeguards and evidence.

Why needed:

- Operational AI quality and governance cannot rely on ad-hoc process.

Success criteria:

- Model promotion is criteria-based and auditable.
- Compliance artifacts are reviewable and updateable on cadence.

Dependencies:

- BE-WS5 observability and BE-WS4 contract discipline improve evidence quality.

---

## Delivery phases (backend-only)

### Phase BE-0 (Week 1): Immediate risk closure

- BE-WS1.1 auth gaps on watchdog mutations.
- BE-WS2 cross-loop and shared-buffer fixes.
- BE-WS1.2 endpoint rate limiting.

Exit gate:

- Critical security/runtime findings closed and verified by tests.

---

### Phase BE-1 (Week 2-4): Structural and contract foundation

- BE-WS3 router/lifespan modularization.
- BE-WS4 response models and contract discipline.
- Initial BE-WS5 metrics endpoint.

Exit gate:

- Monolith decomposition complete for highest-change surfaces.
- OpenAPI contract becomes primary interface source.

---

### Phase BE-2 (Week 5-8): Operability at scale

- BE-WS5 tracing/logging correlation.
- BE-WS6 replay/outbox/watermark hardening.
- BE-WS7 prompt caching and telemetry.

Exit gate:

- End-to-end traceability in place.
- Edge-cloud delivery anomalies are observable and controlled.

---

### Phase BE-3 (Week 9-12): Identity and governance maturity

- BE-WS1.4 JWT/JWKS migration.
- BE-WS8 model lifecycle and compliance artifacts.
- Optional multi-tenant groundwork where roadmap requires.

Exit gate:

- Per-user privileged action attribution.
- Governance package ready for enterprise/regulatory review.

---

## KPI targets

Use these to measure backend plan success:

- **Security:** unauthorized mutation attempts rejected 100%.
- **Reliability:** no loop-binding runtime failures under stress tests.
- **Delivery integrity:** replay attempts rejected; queue overflow controlled.
- **Operability:** p95 endpoint and processing latency measurable by trace.
- **Cost efficiency:** LLM token spend reduction observed post-caching rollout.

---

## Risks and mitigations

1. **Risk:** Router split introduces regressions in shared state assumptions.
   - **Mitigation:** migrate one route domain at a time with parity tests.

2. **Risk:** Observability rollout adds overhead on constrained edge devices.
   - **Mitigation:** tune sampling and export strategy by environment tier.

3. **Risk:** Contract strictness exposes many existing shape inconsistencies.
   - **Mitigation:** phased model adoption with temporary compatibility shims.

4. **Risk:** JWT migration complexity delays delivery.
   - **Mitigation:** keep break-glass static token path during transition period.

---

## Suggested implementation order (ticket-ready)

1. BE-WS1.1 Watchdog mutation auth enforcement.
2. BE-WS2.1 Thread/async bridge audit and fixes.
3. BE-WS2.2 Shared recent-event synchronization fix.
4. BE-WS1.2 Endpoint rate limiting rollout.
5. BE-WS6.1 Nonce replay protection in cloud receiver.
6. BE-WS3.1 Initial router/lifespan extraction.
7. BE-WS4.1 Response models on critical endpoints.
8. BE-WS5.1 `/metrics` + core custom KPIs.
9. BE-WS5.2 OTel trace propagation end-to-end.
10. BE-WS7.1 Prompt caching plus token telemetry.

This sequence closes immediate risks first, then scales reliability and
operational maturity.
