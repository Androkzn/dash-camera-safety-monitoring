# Improvements — index

Four companion documents proposing concrete, prioritized improvements to
fleet-safety-demo, each grounded in the actual codebase (not generic best
practice) and cited against official sources.

| Doc | Focus | Length |
|-----|-------|--------|
| [production-scale-plan.md](./production-scale-plan.md) | Consolidated execution blueprint for national-scale rollout: categories, impact, bottlenecks, phased order | ~40 action points |
| [settings-console-plan.md](./settings-console-plan.md) | Canonical end-to-end plan for the Settings Console: hardening, runtime settings, baseline/impact engine, templates, UI, and benchmark lane | v1.1 |
| [frontend-execution-plan.md](./frontend-execution-plan.md) | Frontend-only detailed execution plan: workstreams, dependencies, gates, KPIs, risks | ~30 action points |
| [backend-execution-plan.md](./backend-execution-plan.md) | Backend-only detailed execution plan: workstreams, dependencies, gates, KPIs, risks | ~35 action points |
| [frontend.md](./frontend.md) | React 19 + Vite SPA: SSE, types, perf, a11y, tests | ~20 recommendations |
| [backend.md](./backend.md) | FastAPI + YOLOv8 + LLM service: caching, observability, MLOps, compliance | ~20 recommendations |
| [integration.md](./integration.md) | FE↔BE↔cloud seam: SSE protocol, contracts, auth, HMAC, tracing | ~20 recommendations |

## How to read

Use [production-scale-plan.md](./production-scale-plan.md) first for planning.
Then branch into the dedicated execution docs:

- [frontend-execution-plan.md](./frontend-execution-plan.md)
- [backend-execution-plan.md](./backend-execution-plan.md)

The plan set provides explicit mapping:

- Why each change is needed.
- Benefit expected.
- Problem solved.
- Suggested phase order for rollout.

Then use the domain docs for implementation specifics.

Each domain doc opens with a **TL;DR** of the highest-leverage items, then a
**P0 bugs** section (real defects found during analysis — fix-first), then
prioritized strategic recommendations (P0/P1/P2), then a **90-day phased
rollout**, then a **what I would NOT recommend** section (signals deliberate
restraint).

Every recommendation follows the same template:

> **Pattern.** What to adopt.
> **Why for this project.** Anchored to specific files / behaviors observed in the codebase.
> **Adoption path.** Concrete file, snippet, or command.
> **Trade-off.** Cost, risk, or constraint.
> **Citation.** Official-doc URL.

## Confirmed P0 defects (fix this week)

| ID | Doc | File | Issue |
|----|-----|------|-------|
| **B1** | [integration.md](./integration.md#p0-bugs-found-during-review) | [server.py:1609,1618](../../road_safety/server.py#L1609) | Watchdog DELETE / POST-delete are unauthenticated — anyone reachable on the listening port can wipe operator state |
| **B2** | [backend.md](./backend.md#p0-bugs-found-during-review) | [services/llm.py](../../road_safety/services/llm.py) | `_HAIKU_BUCKET._lock = asyncio.Lock()` is bound to whichever loop happens to instantiate first; awaiting the bucket from the perception thread risks `RuntimeError: ... bound to a different event loop` on contended frames |
| **B3** | [backend.md](./backend.md#p0-bugs-found-during-review) | [server.py:883-885](../../road_safety/server.py#L883) | `state.recent_events.append/pop(0)` runs in the perception thread; SSE handlers concurrently iterate via `list(state.recent_events)`. Single ops are GIL-safe; the read+slice composite is not |

These are independent of the strategic improvements and should be fixed before any of the larger refactors land.

## Recently shipped (perf — 2026-04-19)

| Change | File | Effect |
|--------|------|--------|
| Auto-select YOLO accelerator (CUDA → MPS → CPU) with `ROAD_YOLO_DEVICE` override | [core/detection.py](../../road_safety/core/detection.py) `load_model()` | Multi-source demo on Apple Silicon: uvicorn CPU **205 % → 54 %** with 6 streams + detection (yolov8s, 2 fps). Closes the silent-CPU-fallback footgun for Mac dev / demo hosts. |
| Per-slot `detection_enabled` toggle + `POST /api/live/sources/{id}/detection` | [server.py](../../road_safety/server.py) (`StreamSlot`, `_on_frame`) | Operator can keep watching N cameras while running YOLO on a subset. Manual escape valve for compute saturation. |
| Focus-aware Admin grid (tap-to-maximize, mini strip, `SelectedStreamHeader`) | `frontend/src/components/admin/{MultiSourceGrid,SelectedStreamHeader}.tsx` | Single source of truth for `focusedId`; `useLiveSources` polled once at the page level. Foundation for the auto-shed priority signal in R-PERF. |

See [backend.md §R4 + R-PERF](./backend.md#r4--h-yolov8-accelerator-selection--export--half-precision-for-the-deployment-target) for the remaining work (auto-shedding, detecting-slot cap, shared MJPEG fan-out, `perf.cpu_saturated` watchdog rule, device surfacing in `/api/admin/health`).

## Reading order for reviewers

1. This README (you are here).
2. [production-scale-plan.md](./production-scale-plan.md) — category and impact
   blueprint for production planning.
3. [settings-console-plan.md](./settings-console-plan.md) — canonical cross-cutting
   implementation plan for the Settings Console.
4. [frontend-execution-plan.md](./frontend-execution-plan.md) — FE delivery
   workstreams and milestones.
5. [backend-execution-plan.md](./backend-execution-plan.md) — BE delivery
   workstreams and milestones.
6. [integration.md](./integration.md) — protocol and contract seam across FE/BE/cloud.
7. [backend.md](./backend.md) — AI/ML runtime, observability, and compliance details.
8. [frontend.md](./frontend.md) — React real-time UX, resilience, and type safety details.

## Out of scope for these docs

- Restating what already works well — see [docs/architecture.md](../architecture.md) and [docs/challenges.md](../challenges.md) for the existing-decision rationale.
- Generic "add observability" / "add tests" platitudes — every recommendation here names the specific tool, file, and trade-off.
- Speculative features (DMS, FNOL clip export, ELD integration). Those are tracked in [challenges.md §8 Out of scope](../challenges.md) and intentionally left there.

## Conventions

- **Pri tags:** `[H]` ship this quarter · `[M]` next 2 quarters · `[L]` opportunistic / when calmer.
- **Effort:** rough engineer-days assuming familiarity with the codebase.
- **Citations:** prefer official docs (react.dev, fastapi.tiangolo.com, docs.anthropic.com, datatracker.ietf.org, owasp.org). Engineering-blog citations are flagged with the source.
