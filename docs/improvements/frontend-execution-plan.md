# Frontend execution plan (detailed)

This is the delivery plan for frontend improvements only.
It is structured for independent execution from backend work.

Primary references:

- `docs/improvements/frontend.md`
- `docs/improvements/integration.md` (SSE and contract seams)
- `docs/improvements/production-scale-plan.md`

---

## Objectives

1. Keep operator UI responsive during event bursts.
2. Remove integration fragility from schema drift.
3. Improve failure containment and security posture in the browser.
4. Establish practical test coverage for real-time workflows.

---

## Scope

In scope:

- React app architecture and state flow.
- SSE client reliability behavior.
- API client typing strategy.
- Client-side security controls for generated content.
- FE tests and CI checks.

Out of scope:

- Backend route/model implementation details.
- Cloud receiver and edge delivery internals.
- Legal/compliance backend artifacts.

---

## Workstreams

### FE-WS1: Runtime resilience and UX stability

Items:

1. Add route-level and app-level `ErrorBoundary`.
2. Add SSE heartbeat stall detection and reconnect watchdog.
3. Add visibility-aware SSE pause/resume logic.
4. Add stale-data indicator when stream is disconnected/recovering.

Why needed:

- Prevent full-app blank screen on local render failures.
- Prevent "connected but stale" operator view during sleep/proxy failures.

Success criteria:

- Simulated render exception does not blank unrelated route.
- Stream stall recovers automatically within target timeout.

Dependencies:

- None (can start immediately).

---

### FE-WS2: Event-stream scalability

Items:

1. Move stream buffer ownership to external store (`useSyncExternalStore`).
2. Wrap non-urgent event burst updates with `startTransition`.
3. Virtualize event lists where list growth can exceed stable thresholds.

Why needed:

- Reduce input lag and render thrash under high event ingress.

Success criteria:

- Chat input remains responsive during synthetic burst replay.
- Event panels maintain smooth scroll/update behavior at max configured buffer.

Dependencies:

- FE-WS1 recommended first (safer behavior during refactor).

---

### FE-WS3: Type-safe integration contract

Items:

1. Generate API types from backend OpenAPI (`openapi-typescript`).
2. Migrate API client to generated `paths` via `openapi-fetch`.
3. Replace hand-maintained API shape definitions where redundant.
4. Add runtime schema validation for SSE payloads (Valibot or equivalent).

Why needed:

- Stop silent FE/BE schema drift and malformed stream payload failures.

Success criteria:

- `types` generation is reproducible in CI.
- Breaking API changes fail typecheck or contract tests before release.

Dependencies:

- Backend must expose stable `response_model` contracts for endpoints.

---

### FE-WS4: Security hardening (client side)

Items:

1. Add safe markdown/render path for LLM-generated chat content.
2. Sanitize rendered HTML with DOMPurify before any HTML insertion path.
3. Keep rendering pipeline default-safe for future markdown features.

Why needed:

- LLM output is untrusted input and can become an XSS vector over time.

Success criteria:

- Security test payloads are neutralized in rendered output.
- No direct unsanitized HTML injection path remains in chat rendering.

Dependencies:

- FE-WS1/FE-WS2 independent.

---

### FE-WS5: Quality gates and tests

Items:

1. Set up Vitest + React Testing Library + MSW baseline.
2. Add tests for SSE reconnect, backoff, and message cap behavior.
3. Add page smoke tests for Admin/Dashboard/Monitoring render stability.
4. Add one E2E happy path for live event render.

Why needed:

- Real-time regressions are hard to detect without focused automated tests.

Success criteria:

- CI runs deterministic FE test suite with no flaky core tests.
- Key real-time behavior is covered by non-snapshot tests.

Dependencies:

- FE-WS1 and FE-WS3 changes should land before finalizing coverage.

---

## Delivery phases (frontend-only)

### Phase FE-0 (Week 1): Risk floor

- FE-WS1 (Error boundaries + heartbeat stall watchdog).
- FE-WS4 (safe chat rendering path).

Exit gate:

- UI does not blank globally on route error.
- Stream freeze simulation recovers without page refresh.

---

### Phase FE-1 (Week 2-3): Contract and stream core

- FE-WS3 (OpenAPI codegen + API client migration).
- FE-WS2 (external store + transition updates).

Exit gate:

- All API calls compile against generated contract types.
- Burst replay test passes with acceptable input responsiveness.

---

### Phase FE-2 (Week 4): Test and reliability closure

- FE-WS5 complete.
- FE-WS1 visibility-aware reconnect polish.

Exit gate:

- FE suite stable in CI.
- Core stream scenarios covered by automated tests.

---

### Phase FE-3 (Deferred/optional): Performance polish

- Virtualization expansion where needed.
- Route code splitting and bundle analysis optimization.

Exit gate:

- Bundle and runtime metrics meet defined thresholds.

---

## Dependencies on backend plan

Hard dependencies:

- Stable OpenAPI response models for generated typing workflow.
- SSE protocol support for `Last-Event-ID` replay behavior.

Soft dependencies:

- Backend heartbeat cadence for stream-health watchdog tuning.
- Consistent event shape/versioning strategy.

---

## KPI targets

Use these to measure frontend plan success:

- **Input responsiveness under burst:** no noticeable typing lag during burst simulation.
- **Stream recovery time:** reconnect and resume within watchdog target window.
- **Contract break detection:** schema drift detected pre-release by type/test gates.
- **Crash containment:** route-level fallback instead of full app failure.

---

## Risks and mitigations

1. **Risk:** Store refactor introduces subtle state bugs.
   - **Mitigation:** feature flag store path, parallel shadow read in dev.

2. **Risk:** Generated types create temporary migration friction.
   - **Mitigation:** endpoint-by-endpoint migration with compatibility wrappers.

3. **Risk:** SSE validation rejects valid but evolving payloads.
   - **Mitigation:** use tolerant schema defaults and explicit version markers.

4. **Risk:** Test suite flakiness on SSE timing.
   - **Mitigation:** deterministic fake timers and controlled mock stream harness.

---

## Suggested implementation order (ticket-ready)

1. FE-WS1.1 Error boundaries.
2. FE-WS1.2 Heartbeat stall watchdog.
3. FE-WS4.1 Safe markdown/sanitization path.
4. FE-WS3.1 OpenAPI type generation pipeline.
5. FE-WS3.2 API client migration.
6. FE-WS2.1 External stream store.
7. FE-WS2.2 Transition-wrapped burst updates.
8. FE-WS5.1 Vitest/RTL/MSW baseline.
9. FE-WS5.2 Real-time behavior tests.
10. FE-WS1.3 Visibility-aware stream lifecycle polish.

This order minimizes user-facing risk first, then enables scalable maintainability.
