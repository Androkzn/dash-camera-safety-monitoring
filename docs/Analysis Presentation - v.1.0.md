# Analysis, Improvements & Bugfixes — Presentation Summary

**Audience:** reviewers / interviewers who want to see *how I analyze a codebase and decide what to fix*, not a changelog.

**How to read each entry:**
- **Problem** — what was wrong and how I spotted it.
- **Fix** — what I actually did.
- **Impact** — what this unlocks or prevents.
- **Alternatives** — what I rejected and why.

For deep technical detail see [Improvements and Refactoring - v.1.0.md](Improvements%20and%20Refactoring%20-%20v.1.0.md).

---

## Table of contents

- [Frontend (FE)](#frontend-fe)
  - [Project structure](#fe-project-structure)
  - [Giant / massive files](#fe-giant-files)
  - [Network layer](#fe-network)
  - [State management](#fe-state)
  - [Hooks](#fe-hooks)
  - [UI / reusable components](#fe-ui)
  - [Type safety](#fe-typesafety)
  - [Performance](#fe-performance)
  - [Error boundaries & lifecycle](#fe-errors)
  - [Documentation / discoverability](#fe-docs)
  - [Critical bugfixes](#fe-bugfixes)
  - [Best practices applied](#fe-best-practices)
  - [Best judgments](#fe-judgments)
- [Backend (BE)](#backend-be)
  - [Project structure](#be-project-structure)
  - [Giant / massive files](#be-giant-files)
  - [Network / API organization](#be-network)
  - [Performance & concurrency](#be-performance)
  - [Type safety](#be-typesafety)
  - [State management](#be-state)
  - [Privacy & security](#be-security)
  - [Resilience (LLM, external calls)](#be-resilience)
  - [Observability](#be-observability)
  - [Documentation / discoverability](#be-docs)
  - [Critical bugfixes](#be-bugfixes)
  - [Best practices applied](#be-best-practices)
  - [Best judgments](#be-judgments)

---

# <a name="frontend-fe"></a>Frontend (FE)

## <a name="fe-project-structure"></a>Project structure

**Problem.** Pages, hooks, shared widgets, and API calls were scattered. No clear ownership boundary, so a change in the admin grid could silently break the watchdog drawer.

**Fix.** Reorganized `frontend/src/` into **feature folders** + a `shared/` folder:
- `features/admin`, `features/settings`, `features/watchdog`, `features/validation`, `features/dashboard`, `features/tests`, `features/monitoring` — each owns its own components, hooks, and `api.ts`.
- `shared/ui` for reusable widgets, `shared/hooks` for cross-feature hooks, `shared/lib` for fetch + format, `shared/config/runtime.ts` for all tunables (poll intervals, thresholds).
- **Import rule:** a feature imports from `shared/` or itself — *never* from another feature. If two features need the same thing, promote to `shared/`.

**Impact.** One engineer can own a feature end-to-end. Reviews are per-folder. Breaking one feature cannot silently leak into another.

**Alternatives.**
- *Group by type (`/components`, `/hooks`, `/pages`):* classic React layout but doesn't scale — a single feature ends up spread across four folders.
- *Full monorepo (`nx`/`turborepo`):* overkill for one app.

---

## <a name="fe-giant-files"></a>Giant / massive files

**Problem.** `SettingsPage.tsx` (431 LoC) and `MultiSourceGrid.tsx` (341 LoC) did everything: fetching, validation, rendering, dialogs. Impossible to test or review.

**Fix.** Decomposed into small, named components with single responsibilities:
- Settings: `SettingsPage`, `Tunable` (one edit widget), `TunablesColumn`, `ImpactCard`, `OpsDeltas`, `ApplyResultBanner`, `SeverityBars`, `LivePreviewCard`, `SettingsHeader` + hooks (`useSettings`, `useSettingsApply`, `useImpact`) + `utils/` (`formatting`, `steps`, `validation`).
- Admin grid: `MultiSourceGrid`, `StreamTile`, `StreamImage`, `HealthStrip`, `AdminEventCard`.

**Impact.** Each piece is unit-testable. Junior contributors can edit a `Tunable` without understanding the whole settings flow.

**Alternatives.**
- *Class components with inheritance:* not idiomatic in modern React.
- *Keep one file but split with comments:* doesn't help testability or review scope.

---

## <a name="fe-network"></a>Network layer

**Problem.** Every component rolled its own `fetch(...)`. Error shapes differed per caller. No `AbortSignal` handling — a component unmounting while a request was in flight would still write to state (React warning, noisy logs, occasional bugs in StrictMode double-invoke).

**Fix.** One central `shared/lib/fetchClient.ts`:
- `apiFetch(url, opts)` — default `cache: "no-store"`, threads an `AbortSignal` through, infers content type.
- `HttpApiError` — structured error class carrying `status`, `body`, `retryAfterSec` (for HTTP 429 rate-limit responses) and 422 validation details.
- Every `useQuery` / `useMutation` passes through `apiFetch` — uniform error shape everywhere.

**Impact.** Unmount cancellation is automatic. Rate-limit / validation errors render consistently. Debugging network issues is reading one file, not twenty.

**Alternatives.**
- *`axios`:* bigger bundle; native `fetch` + tiny wrapper is enough.
- *Generated client from OpenAPI:* considered for the future (see BE type safety).

### Transport choice: just poll

**Problem.** Every major browser caps HTTP/1.1 at 6 concurrent connections per origin. A long-lived push transport (`multipart/x-mixed-replace`) would hold one TCP connection open per tile, so ≥6 tiles + SSE deadlocks the browser on plain HTTP. HTTP/2 behind a reverse proxy dissolves the cap (all streams multiplex over one TCP connection), but that adds a "production **must** use an HTTP/2 proxy" deploy asterisk.

**Why polling works here.** The pipeline runs at `TARGET_FPS` (default 2). The edge produces a new frame every ~500 ms. A ~400 ms poll cycle catches every frame the edge emits. Push delivery can't beat that ceiling — the latency floor is inference cadence, not transport.

**Fix.** Every tile polls `GET /admin/frame/{id}` every ~400 ms. `has_viewers()` on `StreamSlot` is a single `time.monotonic()` delta against the last poll: any hit within 2 s keeps the annotated-JPEG encode path hot.

**Impact.** One code path. No deploy asterisk — polling scales fine on HTTP/1.1 because each request closes its connection promptly. No dev/prod drift.

**Alternatives considered.**
- *Server-told capability (`GET /admin/video_caps` returns `push`|`poll`).* More honest than a client-side protocol guess, but still more code than "just poll."
- *WebRTC / HLS.* Relevant at ≥10 fps or multi-tenant public fan-out. Not this product's regime.

**When this stops being right.** ≥10 fps sources, or >30 tiles on cellular. At that point WebRTC is the honest answer.

---

## <a name="fe-state"></a>State management

**Problem.** Hand-rolled `setInterval` polling, local `useState` caches, manual revalidation. No dedupe across components asking for the same resource. Data got stale and no one knew when to refresh.

**Fix.** Adopted **TanStack Query** (React Query) for all server state:
- `useQuery` with `refetchInterval` for polling (auto-cancels on unmount, pauses when tab is hidden via `refetchIntervalInBackground: false`).
- `useMutation` + `onMutate` rollback for optimistic UI.
- `invalidateQueries(...)` replaces manual "refetch this list after that POST."
- Deleted the legacy `shared/hooks/usePolling.ts` to make the rule enforceable.

**Impact.** Shared cache means one network call serves N components. Background refresh + stale-while-revalidate is free. No more zombie state writes.

**Alternatives.**
- *Redux Toolkit + RTK Query:* ceremony tax per slice, weaker Suspense/AbortSignal integration.
- *SWR + Zustand:* lighter, but less polished mutation / rollback story.

---

## <a name="fe-hooks"></a>Hooks

*Concept: a React "hook" is a reusable function that lets a component subscribe to state or side effects (e.g. `useState`, `useEffect`).*

**Problem.** Every page had its own SSE subscription (`EventSource` = a browser API that holds an open HTTP connection for server-sent events). Four open pages = four connections to the same feed. State was duplicated across components.

**Fix.** Shared hooks in `shared/hooks/`:
- `useSSE` — one connection per stream, with automatic backoff + reconnect.
- `useEventStream` (context-based) — **single** EventSource for the main safety-event feed, shared across the app via `<EventStreamProvider>`. Specialized feeds (e.g. `/admin/detections`) still use their own hook — the rule is "don't duplicate ownership of the *same* stream."
- `useLiveStatus`, `useUptimeTicker` — small, focused hooks with clear contracts.

**Impact.** One subscriber per tab instead of N. Backoff and reconnect logic lives in one place.

**Alternatives.**
- *Redux middleware handling the WebSocket:* works, but SSE is one-way and simpler; doesn't need a store.

---

## <a name="fe-ui"></a>UI / reusable components

**Problem.** Each feature hand-rolled its own buttons, cards, badges, empty states. Inconsistent styling and duplicated code.

**Fix.** `shared/ui/` with a small library: `Button`, `Card`, `Dialog`, `EmptyState`, `ErrorBoundary`, `ErrorList`, `EventFilterBar`, `Input`, `Pill`, `RiskBadge`, `Section`, `Skeleton`, `Spinner`, `Tabs`, `Tag`, `Dot`. Plus `shared/layout/` for `PageChrome`, `PageLayout`, `TopBar`, and `shared/events/` for cross-feature event cards / dialog.

**Impact.** Visual consistency. A style change lands in one file. Features shrink — they compose primitives instead of re-inventing them.

**Alternatives.**
- *Full design system (MUI, Chakra, shadcn):* heavy, opinionated styling. Small bespoke library fits the product's look.
- *Tailwind-only with no components:* ends up duplicating class-name soup per feature.

---

## <a name="fe-typesafety"></a>Type safety

**Problem.** `shared/types/common.ts` was a 334-line hand-mirrored copy of backend shapes. Every backend rename silently desynced and produced a runtime parse error in dev. "Source of truth in two places" = no source of truth.

**Fix.** Backend Pydantic models (`backend/api/models.py`) → `scripts/generate_ts_types.py` → `frontend/src/shared/types/generated.ts` (588 LoC, machine-written). `common.ts` is now a 50-line re-export shim so existing imports keep working. `start.py` runs codegen *before* every Vite build — a backend rename fails the build loudly, not at runtime.

**Impact.** Wire contract physically generated from the backend. Field renames caught at compile time. Also: `risk_level` and `stream_type` become string-literal unions; `bbox` becomes a 4-tuple (safe under `noUncheckedIndexedAccess`).

**Alternatives.**
- *`openapi-typescript` against FastAPI's OpenAPI:* needs `response_model=` on every handler first (partial coverage today).
- *`pydantic2ts` or `datamodel-code-generator`:* another dependency. The bespoke 60-line script gives full control.

---

## <a name="fe-performance"></a>Performance

**Problem.** One bundle for everything meant `/settings` loaded the watchdog + admin + validation code. Slow first paint, and a bug in one feature could crash the whole SPA.

**Fix.**
- **Lazy routes** via React Router: each page is its own chunk (`import()` boundary), wrapped in `<RouteShell>` (ErrorBoundary + Suspense).
- **Optimistic UI** with an 8-second "stuck-busy" escape on live-source mutations — UI reflects the intent immediately but unblocks if the server doesn't respond.
- **Idle encode skip (BE side):** tiles with no viewers stop burning JPEG encode cycles (`StreamSlot.has_viewers`).
- **Background polling pause:** `refetchIntervalInBackground: false` means hidden tabs stop hammering the server.

**Impact.** Settings page no longer pays for the watchdog bundle. Per-feature errors are contained. Idle tabs cost ~zero.

**Alternatives.**
- *SSR via Next.js:* nothing to SSR on a live operator dashboard — every page is authenticated + streaming.
- *Service worker + stale cache:* adds complexity for little gain on an always-online operator app.

---

## <a name="fe-errors"></a>Error boundaries & lifecycle

**Problem.** One rendering error = white screen for the whole app. No visibility into which feature failed.

**Fix.**
- `<RouteShell>` wraps every lazy route with its own `ErrorBoundary` + `Suspense` — a crash in one page shows a fallback, others keep running.
- `AbortSignal` on every fetch — no writes to unmounted components.
- `DialogProvider` centralizes modal state so dialogs don't fight each other.

**Impact.** Partial failure instead of total failure. Cleaner dev console in StrictMode.

---

## <a name="fe-docs"></a>Documentation / in-file discoverability

**Problem.** Tracing a SafetyEvent from the SSE channel → React hook → rendered component meant grepping `/api/*` string literals and chasing `useQuery` keys by hand. JSDoc coverage was uneven: some files carried rich headers, most had none. Onboarding leaned on `CLAUDE.md` plus tribal knowledge about *which hook hits which endpoint*.

**Fix.** One-pass annotation of **126 frontend files** (everything under `frontend/src/` except the machine-written `shared/types/generated.ts`, which is clobbered on every launch). For each file:

- **File-level header** naming the BE endpoint it calls (or SSE channel it subscribes to), the parent page/feature it belongs to, and its role in that feature.
- **JSDoc above every exported symbol** (component, hook, function, type) describing props, child elements rendered, the endpoint-and-effect it triggers, and the FE hook that mediates the call.
- **Inline notes** on non-trivial logic: SSE reconnect/backoff, optimistic-mutation lifecycle (`onMutate` / `onError` / `onSettled` rollback), poll-cadence choice and *why* the 6-connection HTTP/1.1 cap forces a non-push transport, severity-color mapping, stale-time deduplication, portal mount targets for dialogs.

Run in parallel across 20 agents; zero code changes; `tsc -b --noEmit` clean after the pass.

**Impact.** `grep "/api/events/stream"` now returns a JSDoc block explaining who consumes it, with what hook, in which component. IDE hover on `useLiveStatus` surfaces the full contract (endpoint, cache key, refetch cadence, consumer pages) without jumping to the definition. New contributors read one file instead of three to understand any page.

**Alternatives.**
- *Architecture doc only* (what `docs/architecture.md` already does): global view, but no per-file wayfinding once you've clicked into code.
- *Typedoc site*: more infra, and the current comments already surface in VS Code / WebStorm tooltips — a generated site would be a second target to keep in sync.
- *No annotations*: `CLAUDE.md`'s default style ("names should explain themselves"). Overrode it here deliberately to optimize FE↔BE traceability for new contributors.

---

## <a name="fe-bugfixes"></a>Critical bugfixes

1. **Zombie state writes on unmount** → threaded `AbortSignal` through `apiFetch` + every `useQuery`. React no longer warns, no noisy logs in StrictMode.
2. **Stuck "applying…" state** on settings apply when network hiccuped → 8-second escape timer with explicit failure banner.
3. **Browser connection exhaustion** at 5+ tiles → polling-only transport (see Network).
4. **Duplicated SSE subscribers** — every open tab opened N connections → shared `<EventStreamProvider>`.
5. **Silent type drift** between backend and frontend → codegen pipeline (see Type safety).
6. **White-screen crashes** from one feature breaking all others → per-route error boundaries.

---

## <a name="fe-best-practices"></a>Best practices applied

- **Feature folders > type folders.** Ownership beats categorization.
- **One place for the fetch layer.** Errors, abort signals, headers — one file.
- **Server state ≠ client state.** Use a cache (TanStack) for the first; `useState`/context for the second.
- **Codegen the contract.** Never hand-mirror backend types.
- **Lazy-load per route.** Isolate bundles and failures.
- **Cancel on unmount.** Always thread `AbortSignal`.
- **Document the rules.** `.claude/rules/frontend.md` encodes the import rule, the SSE-ownership rule, and the "no hand-rolled polling" rule.

---

## <a name="fe-judgments"></a>Best judgments (what I chose *not* to do)

- **No Redux.** Server state is a cache problem, not a store problem. TanStack Query is the right tool.
- **No design system import.** Bespoke `shared/ui/` fits the product; heavy libraries would fight the operator-dashboard aesthetic.
- **No SSR.** Operator SPA with live streams — nothing to server-render.
- **No WebRTC.** At 2 fps inference, frame-period latency is invisible; the infra tax is not worth it.
- **Templates UI trimmed back** (backend endpoints retained) — shipped the core apply+impact loop first rather than over-extending an unproven UX.

---

---

# <a name="backend-be"></a>Backend (BE)

## <a name="be-project-structure"></a>Project structure

**Problem.** Everything lived in `road_safety/server.py` — a 1,535-line file mixing 37 HTTP routes with business logic, live state, and domain models. Every feature was a merge conflict.

*(Concept: in Python, a "package" is a folder with `__init__.py`; a "module" is a single `.py` file. A "router" in FastAPI groups related endpoints.)*

**Fix.** Split into feature packages under `backend/`:
- `backend/core/` — perception, orientation policy, validator, stream, depth, egomotion, quality.
- `backend/perception/` — hot-path inference + event emit + broadcast.
- `backend/services/` — LLM, drift, watchdog, impact, ops_sampler, templates, registry, redact, agents.
- `backend/api/` — routers + Pydantic models + feedback + settings endpoints.
- `backend/integrations/` — edge-to-cloud publisher, Slack.
- `backend/compliance/` — audit log + retention.
- `backend/security/` — SSRF guard + rate limit.
- `backend/rendering/` — clip + frame encoders.
- `backend/domain/` — `Episode`, `StreamSlot` (extracted from shared state).
- `backend/state.py` — singletons only.
- `backend/server.py` — **194 lines** (from 1,535). Pure composition root: wires routers + lifespan.

**Impact.** Each module is independently testable. New features land as new files, not 300-line diffs inside `server.py`. A new engineer reads `server.py` in one screen.

**Alternatives.**
- *Hexagonal / clean architecture (`domain`/`application`/`infrastructure`):* 3× file count, ceremony tax. Overkill for a single-process POC with one perception backend.
- *Keep monolith + `APIRouter` blocks in the same file:* same merge-conflict magnet, routers can't mount into isolated test apps.

---

## <a name="be-giant-files"></a>Giant / massive files

**Problem.**
- `server.py` at 1,535 LoC.
- `watchdog.py` at ~1,800 LoC — rules + AI + storage + orchestration in one file.
- `state.py` at 791 LoC — singletons and domain classes intermingled.

**Fix.**
- `server.py` → 194 LoC (composition root).
- `watchdog.py` → `backend/services/watchdog/` package: `model.py` (dataclass + fingerprinting), `rules.py` (deterministic detectors), `ai.py` (Claude hypothesis layer, strictly additive), `storage.py` (JSONL I/O), `api.py` (background loop + `stats()`).
- `state.py` 791 → 400 LoC — `Episode` and `StreamSlot` moved to `backend/domain/`, re-exported for backward compat.

**Remaining candidates.** `backend/services/impact.py` (825 LoC), `backend/api/settings.py` (659 LoC — SSE + ticket exchange could move out).

**Impact.** Each concern is testable in isolation. Rules can be exercised without booting the loop. AI layer can be stubbed for offline tests.

---

## <a name="be-network"></a>Network / API organization

**Problem.** 37 routes as `@app.get(...)` decorators in one file. Impossible to test one route without booting the whole app. Shared little beyond the `app` singleton and intermingled with business logic.

**Fix.** 15 feature routers under `backend/api/routers/` + 2 function-mounted routers:

| Router | Responsibility | LoC |
| --- | --- | ---: |
| `live.py` | live status / events / clips | 344 |
| `sources.py` | multi-source CRUD + start/pause | 208 |
| `admin_health.py` | admin health strip | 145 |
| `sse.py` | `/api/live/stream` broadcaster | 130 |
| `watchdog.py` | incident queue | 129 |
| `spa.py` | SPA fallback | 110 |
| `agents.py` | coaching / investigation | 101 |
| `admin_video.py` | snapshot frame endpoint | 94 |
| ...plus 7 smaller routers | | |

Pydantic response models in `backend/api/models.py`.

**Impact.** `tests/test_settings_api.py` can mount *only* the settings router in a fresh `FastAPI()` — no perception boot, no global state. That was impossible before.

**Alternatives.**
- *Group by HTTP verb / layer (reads vs. writes):* a single feature now spans 3 files.
- *One router per resource collapsed into 4–5 big ones:* `live.py` would re-drift toward 800 LoC.
- *Class-based controllers:* non-idiomatic FastAPI, breaks dependency-override testing.

---

## <a name="be-performance"></a>Performance & concurrency

**Problem.** Hot path (perception running at `TARGET_FPS` per source) read mutable config dicts. Every gate evaluation touched a shared dict — races risked partial reads; a lock-per-read would burn CPU under contention.

**Fix.** `SettingsStore` with **snapshot isolation**:
- Writers take a short `RLock`, validate the diff, build a new immutable `MappingProxyType` (Python's read-only dict view), atomically rebind the module-level reference, release the lock.
- Readers call `STORE.snapshot()` — **lock-free**. They hold the snapshot that was current at call time even if a write completes mid-handler.
- Subscribers notified *outside* the lock, each in its own `try/except` — a buggy listener can't poison the apply chain.
- `last_known_good` captured before each apply → `POST /api/settings/rollback` is a pointer swap.

**Invariants.** A reader never blocks a writer; a writer never blocks readers; no one ever sees a partially-applied diff.

**Impact.** Hot path stays nanosecond-cheap. Operators can tune thresholds at runtime without even a microsecond of contention.

**Alternatives.**
- *Mutex per read:* wrong order of magnitude.
- *Copy-on-write dict behind a queue:* still allocates, adds queue latency.
- *SQLite per access:* microseconds per read, reintroduces partial-read window.

---

## <a name="be-typesafety"></a>Type safety

*(Concept: Python is dynamically typed — a variable's type isn't declared. "Static typing" tools like `mypy` and `pyright` read annotations at build time and catch type mismatches before runtime.)*

**Problem.** No static type checking. Refactor errors (renaming a field on a model) only surfaced at runtime, sometimes hours into a session.

**Fix.** Two-tier typing on the boundary:
- **Tier 1 — `backend.api.models` (contract):** fully strict `mypy`. This is the wire shape; it must be airtight.
- **Tier 2 — `backend/api` routers + `backend/services/llm.py` (failure-sensitive):** strict minus `disallow_untyped_defs` — handler returns are already constrained by FastAPI's `response_model=`.
- **Third-party stubs** relaxed (`cv2`, `numpy`, `ultralytics`) because typing their shapes is weeks of cleanup for marginal value.
- Companion tool: `pyright` at `basic` mode over the included tree (`make typecheck`) for IDE feedback.

**Why narrow?** The two most failure-sensitive surfaces — the request/response contracts and the LLM resilience layer — were worth typing first. Typing the perception hot path against `numpy`/`cv2` would burn weeks for marginal benefit.

**Impact.** Wire boundary is type-checked end-to-end (Pydantic in + mypy-strict on the slice + codegen → TypeScript-strict on FE). Refactors surface at build, not at runtime.

**Alternatives.**
- *`pyright` only:* different tool, different guarantees; doesn't plug into Pydantic via plugin.
- *`mypy --strict` everywhere:* blocks weeks of work for marginal gain on `numpy`-heavy code.
- *No static typing (runtime Pydantic only):* refactor errors surface in production, not in CI.

---

## <a name="be-state"></a>State management

**Problem.** Live state (per-source streams, episodes, viewer counts) lived as dict-of-dicts in `server.py`, mutated from everywhere.

**Fix.**
- `backend/domain/stream_slot.py` — one `StreamSlot` class per live source: tracks viewers (`mark_polled`, `has_viewers` via 2 s poll TTL), per-slot `detection_enabled` toggle.
- `backend/domain/episode.py` — one `Episode` per active incident.
- `backend/state.py` — thin module owning the singletons (`STORE`, slot registry) and nothing else.
- `SettingsStore` for config (see Performance).

**Impact.** State changes go through typed methods, not ad-hoc dict mutation. Tests construct a fresh `StreamSlot` directly without booting the world.

**Alternatives.**
- *Global module-level dicts:* what we had; doesn't scale.
- *Redis / external KV:* adds deploy complexity for a single-process edge.
- *Pydantic models for state:* validation cost on every write — state is hot, config is cold; different tools for different rates of change.

---

## <a name="be-security"></a>Privacy & security

### Plate hashing at ingest (not egress)

**Problem.** License plate text is PII under GDPR. "Scrub at egress" is fragile — every new consumer must remember to strip. A forgotten `return event` leaks PII.

**Fix.** `enrich_event()` in `backend/services/llm.py` hashes the plate and **strips `plate_text` / `plate_state` before** the event ever reaches an in-memory buffer. `perception/emit.py` keeps a defense-in-depth `pop()`, but the primary invariant — no raw plate in any buffer — is enforced at ingest. Encoded in `CLAUDE.md` + `.claude/rules/python.md` so agent edits can't accidentally reintroduce the field.

**Impact.** The plate is read once, in a controlled LLM call, and nowhere else. A future buffer consumer **cannot** leak because the field doesn't exist in memory.

**Alternatives.**
- *Egress scrub:* convention-enforced; one miss = leak.
- *KMS-encrypted at rest, decrypt at egress:* per-event KMS call on hot path; plate still briefly cleartext in memory.

### SSRF guard + rate limits

**Problem.** Operator-supplied URLs (adding a stream source) could point to internal services (`localhost:5432`, metadata IPs, etc.) — classic Server-Side Request Forgery.

**Fix.** `backend/security/ssrf.py::validate_public_url()` rejects private/loopback/link-local. `backend/security/rate_limit.py` caps per-IP clip renders.

**Impact.** An operator (or attacker via operator UI) cannot make the edge fetch internal endpoints.

### Edge → cloud HMAC

**Problem.** A cloud receiver that accepts unsigned batches is a PII firehose for anyone who guesses the URL.

**Fix.** Every edge→cloud batch signed with an HMAC over the payload. Receiver verifies before touching the payload; deduplicates on event id (retries are safe).

**Impact.** TLS proves you're talking to the right server; HMAC proves the batch was minted by an authorized edge. Works air-gapped with a shared secret — no IdP dependency.

**Alternatives.**
- *TLS alone:* anyone with the URL can forge.
- *OAuth client-credentials per edge:* needs an IdP reachable from every edge (incompatible with air-gap).
- *Managed broker (SQS/Kafka):* vendor-coupled, doesn't work on-prem.

---

## <a name="be-resilience"></a>Resilience — single LLM egress, failover, cost ceiling

**Problem.** A perception system that calls Claude from five places is one outage away from being unable to start. No single place to put a circuit breaker, cost cap, or retry policy.

**Fix.** All LLM calls route through `backend/services/llm.py`. Nothing else imports an SDK (enforced by `.claude/rules/python.md`). The module owns:
- Provider selection (Anthropic + Azure OpenAI with automatic failover — Azure wins when both are configured).
- Per-minute + per-hour rate budget.
- Circuit breaker on consecutive failures (so one outage doesn't cascade).
- Cost tracking (`/api/llm/stats`).
- Plate-hashing in `enrich_event` (the privacy invariant).
- "No-network" stub mode for tests.

**Impact.** When Anthropic returns 529 "overloaded," watchdog still runs (rules-only), enrichment falls back to the secondary provider or skips, and the cost meter prevents a runaway retry loop.

**Alternatives.**
- *Let each consumer call its SDK directly:* breaker state fragments, cost tracking impossible, privacy invariant reimplemented N times.
- *LangChain / LlamaIndex:* heavy dependency; opinionated chains we don't need.
- *Sidecar LLM service:* extra deploy, extra network hop, re-opens the plate-handling surface across processes.

---

## <a name="be-observability"></a>Observability — watchdog as an incident queue

**Problem.** "Error logs" get tuned out within a week. Operators need *what to do*, not *what happened*.

**Fix.** Watchdog findings carry `severity`, `category`, `impact`, `likely_cause`, `owner`, `evidence`, `investigation_steps`, `debug_commands`, `runbook`, `priority_score`, `source` (`rule` | `ai`), `cause_confidence`. Grouped by `fingerprint` — repeated symptoms collapse into one ticket. Two layers:
- **Rules** (`rules.py`) — deterministic, always available.
- **AI hypothesis** (`ai.py`, Claude) — strictly additive; deduplicated against rules (rules win on same fingerprint *or* title).

**Design invariant:** monitoring never *depends* on the LLM. If Anthropic is unreachable, AI layer returns `[]` and rules carry on.

**Impact.** Operators get "paste this `curl` to reproduce" instead of a log wall. Monitoring survives provider outages.

**Alternatives.**
- *Ship to Datadog / Sentry:* mature UI, but no perception-domain knowledge and needs outbound internet (incompatible with air-gap).
- *Rules only:* misses novel patterns.
- *LLM-first with rules as fallback:* violates the "monitoring must survive outages" invariant.

---

## <a name="be-docs"></a>Documentation / in-file discoverability

**Problem.** `backend/api/models.py` is the wire-contract source of truth, but nothing on each Pydantic class told a reader *which FE hook* consumed it. Routers mentioned path + `response_model` but not the downstream React component. Perception gates had terse constants with no rationale — `if ttc < 0.6` didn't explain *why 0.6* or which false-positive class it was killing. Coverage was bimodal: `services/llm.py` and `services/watchdog/` were exhaustively documented; many smaller modules had empty docstrings.

**Fix.** One-pass annotation of **85 backend files** across `backend/`, `cloud/`, `tools/`, and `scripts/`. For each file:

- **Module docstring** naming upstream producers and downstream consumers — where the data comes from, and specifically which endpoint → FE hook → React component path surfaces it.
- **Per-function / per-class docstrings** covering inputs, outputs, side effects (DB writes, SSE fan-out, audit log, cloud publish), and privacy / security notes.
- **Inline comments** on non-trivial logic: the plate-hash invariant at ingest (stripped *before* any buffer), HMAC signature computation and replay defence, circuit-breaker state transitions (closed → open → half-open), frame-encode viewer gating, watchdog fingerprint-collision resolution (rule wins over AI on fingerprint *or* title match), and gate rationale — *why* this threshold, not just *what it is*.
- **Pydantic docstrings** name the producer endpoint and the consuming FE hook without touching field definitions (no new `Field(...)` wrappers, preserving `scripts/generate_ts_types.py` output byte-for-byte).

Run in parallel across 19 agents; zero executable code changes; `py_compile` clean across all touched modules; TS codegen re-run emits a byte-identical `generated.ts`.

**Impact.** Each module reads as its own mini design doc. Tracing a field from `SafetyEvent` in Pydantic to its render site in `EventDialog` now follows breadcrumbs in both directions — from `models.py` forward to the hook, or from the hook back to the router and the service that populates the payload. Threshold tuning during settings work has the *why* beside the number, so operators and new engineers can adjust without rediscovering the false-positive class a gate was designed to kill.

**Alternatives.**
- *Sphinx auto-API:* docstrings are a prerequisite, not an alternative — this pass is what makes a future Sphinx build worthwhile. Defer until docstring coverage stabilises.
- *Inline TODOs + external wiki:* external docs drift the moment the code changes. Inline comments live with the code and survive refactors via `git blame`.
- *No annotations*: project `CLAUDE.md` defaults to "names explain themselves." Traded that philosophy here for onboarding velocity and FE↔BE wayfinding.

---

## <a name="be-bugfixes"></a>Critical bugfixes

1. **Hot-path config race** → `SettingsStore` snapshot isolation (atomic pointer swap).
2. **Lost-update on concurrent settings edits** → `If-Match: expected_revision_hash` returns HTTP 409; per-token/IP 5-second apply cooldown; single-use 30-second SSE tickets.
3. **PII leak risk in event buffers** → plate hash at ingest + stripped fields.
4. **Cascading LLM outages** → circuit breaker + provider failover + rules-only watchdog fallback.
5. **Forged cloud ingest** → HMAC-signed batches + idempotent receiver.
6. **SSRF via stream URL input** → `validate_public_url()` guard.
7. **Inference CPU burn on idle tiles** → `StreamSlot.has_viewers`: encode only when watched.
8. **`psutil`-backed ops sampler** replaced ad-hoc FPS/CPU estimates — feeds ImpactMonitor with real numbers.
9. **Watchdog orchestration loop previously blocked on LLM** → AI hypothesis moved to async, rules-first pattern.
10. **Subscriber-crash poisoning apply chain** → each subscriber wrapped in `try/except`; errors counted but don't roll back the apply.

---

## <a name="be-best-practices"></a>Best practices applied

- **Composition root.** `server.py` wires things; it doesn't contain logic.
- **One egress per concern.** All LLM calls via `services/llm.py`; all settings writes via `SettingsStore.apply_diff()`.
- **Immutable snapshots for hot-path config.** Readers never block writers; no partial reads.
- **Rules before AI.** Deterministic floor; AI is additive and deduplicated.
- **Message auth, not just transport auth.** HMAC on the edge→cloud channel.
- **Defense in depth.** Plate-hash at ingest *and* a defensive `pop()` at egress.
- **Static typing on the boundary.** Wire contract is mypy-strict.
- **Pydantic models as source of truth.** Codegen to TypeScript.
- **Atomic diffs with rollback.** `last_known_good` + pointer swap.
- **Document invariants.** `CLAUDE.md` + `.claude/rules/*.md` turn tribal knowledge into enforceable text.

---

## <a name="be-judgments"></a>Best judgments (what I chose *not* to do)

- **No auth in the POC.** Half-built auth is worse than none (operators assume it protects things, and it doesn't). Documented the gap in `README` and `CLAUDE.md`; HMAC and audit log still in place for the channels that matter.
- **No full hexagonal architecture.** Would triple the file count for a single-process POC.
- **No feature-flag SaaS (LaunchDarkly/Unleash).** Doesn't do statistical impact gating, which is the actual product differentiator.
- **No etcd/Consul for config.** Overkill for one process; SQLite for durable history is enough.
- **No OpenAPI-to-TS generator yet.** Requires `response_model=` discipline on 100% of handlers first.
- **No full-codebase mypy strict.** Typing `numpy`/`cv2` shapes is weeks of cleanup for marginal benefit.
- **Dashcam code fully stripped, not flag-gated.** Two products fighting in one repo produce contradictory gate behavior. `dashcam-last-known-good` branch gives the archaeology path back.

---

## Summary table — analysis framework

| Common area | What I look for | Frontend fix | Backend fix |
| --- | --- | --- | --- |
| Project structure | Cross-feature coupling | Feature folders + import rule | Feature packages under `backend/` |
| Giant files | One file owns many concerns | `SettingsPage`, `MultiSourceGrid` decomposed | `server.py` 1535→194; `watchdog.py` split |
| Network | Ad-hoc fetches, no abort | `apiFetch` + `HttpApiError` + `AbortSignal` | 15 routers + HMAC ingest + SSRF guard |
| State management | Hand-rolled caches | TanStack Query | `SettingsStore` snapshot isolation |
| Hooks / lifecycle | Duplicated subscriptions | Shared `useSSE`, single `EventStream` | `StreamSlot` viewer tracking |
| UI / reusable | Copy-pasted widgets | `shared/ui/` library | (n/a) |
| Type safety | Types drift from reality | Generated TS from Pydantic | Two-tier `mypy` on boundary |
| Performance | Paying for what you don't see | Lazy routes, background pause | Encode-on-demand, lock-free reads |
| Error handling | One bug breaks everything | Per-route `ErrorBoundary` | Circuit breaker + rules-only fallback |
| Privacy / security | Trust boundaries | (n/a) | Plate-hash at ingest, HMAC, SSRF |
| Observability | Logs vs. actionable incidents | (n/a) | Watchdog fingerprinted findings |
| Code discoverability | File-to-endpoint map, tribal wire-contract knowledge | 126 files annotated (JSDoc + FE→BE map) | 85 files annotated (endpoint→consumer docstrings, gate rationale) |

---

## How I'd present this

1. **Framework first** — show the table above. "These are the common areas I audit."
2. **Pick two deep dives** — the ones that show judgment, not just work. I use: **`SettingsStore` snapshot isolation** (BE concurrency) and **polling-only transport** (FE network: 6-conn cap + 2 fps perception).
3. **Volunteer a gap** — "auth is the biggest product-readiness gap, and here's why I didn't half-build it." Engineering judgment > feature enthusiasm.
4. **One alternative per topic** — "I considered X; rejected because Y." Proves I chose, didn't just do.
