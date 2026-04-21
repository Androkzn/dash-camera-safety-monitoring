# Analysis Presentation — Brief

**Audience.** Reviewers / interviewers. Shows *how I audit a codebase and decide what to fix*, not a changelog.

**Per-entry shape.** Problem → Fix → Impact → Alternatives.

For full detail: [`Improvements and Refactoring - v.1.0.md`](Improvements%20and%20Refactoring%20-%20v.1.0.md). For the long-form presentation: [`Analysis Presentation - v.1.0.md`](Analysis%20Presentation%20-%20v.1.0.md).

---

## Audit framework — common areas I check

| Area | What I look for |
| --- | --- |
| Project structure | Cross-feature coupling, missing ownership boundaries |
| Giant / massive files | One file owns many concerns |
| Network | Ad-hoc fetches, no abort, inconsistent errors |
| State management | Hand-rolled caches, ad-hoc mutation |
| Hooks / lifecycle | Duplicated subscriptions, leaks on unmount |
| UI / reusable components | Copy-pasted widgets, style drift |
| Type safety | Types drift from runtime reality |
| Performance | Paying CPU/bandwidth for things no one sees |
| Error handling | One bug breaks everything |
| Privacy / security | Trust boundaries, PII leak surface |
| Resilience | One outage cascades through the app |
| Observability | Logs vs. actionable incidents |

---

# Frontend (FE)

## 1. Project structure

#### Problem

Pages, hooks, widgets, and API calls lived wherever the last contributor put them. No ownership boundary. **The smoking gun:** a refactor to `MultiSourceGrid.tsx` (admin) silently broke the watchdog page because watchdog had loosely copied the same component. Two versions drifted; nobody caught it until staging.

A junior couldn't tell whether `useStreamControl` belonged to admin or was shared. Copies proliferated instead of promotions.

#### Fix

Seven feature folders — [admin](../frontend/src/features/admin/), [dashboard](../frontend/src/features/dashboard/), [monitoring](../frontend/src/features/monitoring/), [settings](../frontend/src/features/settings/), [tests](../frontend/src/features/tests/), [validation](../frontend/src/features/validation/), [watchdog](../frontend/src/features/watchdog/) — each owning its pages, components, hooks, `api.ts`, and `types.ts`. Cross-feature primitives live in [shared/](../frontend/src/shared/) (`ui/`, `hooks/`, `lib/`, `layout/`, `events/`, `config/`, `types/`).

The load-bearing rule, [.claude/rules/frontend.md](../.claude/rules/frontend.md): **a feature may import from `shared/` or itself, never another feature.** If two features need the same hook, it moves to `shared/hooks/`. No exceptions.

#### Non-obvious details

1. **Watchdog is a feature *and* cross-cutting.** It lives in `features/watchdog/` but its provider mounts in [app/providers.tsx](../frontend/src/app/providers.tsx) alongside core infrastructure. Other features consume it via its exported hook, never import its components directly — the rule holds.
2. **Shared types vs. feature types.** `shared/types/generated.ts` holds the wire contract; per-feature `types.ts` holds feature-local discriminated unions. Promotion path: if two features import it, it moves up.
3. **Promotion takes 5 minutes.** Predictable imports mean "move this to shared" is a grep + mv, not an archaeology dig.

#### Impact

- **Ownership is explicit.** One engineer = one feature folder. PR title `admin: refactor MultiSourceGrid` tells the right people to look.
- **Cross-feature coupling is visible in the diff.** An import from `features/watchdog/` in an admin file = rule violation at review time.
- **Delete-feature is a clean operation.** Remove `features/watchdog/` + one line in providers.tsx, everything else stays.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Type folders** (`/pages`, `/hooks`, `/components`) | One feature spans three folders. "Is this hook admin-specific?" requires a path parse; feature folders answer in the directory name. |
| **Monorepo (Nx, Turborepo)** | Workspace tax for a single Vite app. Feature rule + path aliases give the same isolation at 1% the infra. |
| **No rule, just convention** | "Please put shared things in shared/." Works for 3 people; breaks at 10. Rule enforced by review + folder structure is load-bearing. |

**One-liner.** *"Features own their pages; shared owns primitives. The no-cross-feature-import rule makes ownership explicit and breakage visible in the diff."*

## 2. Giant / massive files

#### Problem

**SettingsPage.tsx** (431 LoC) mixed page layout, form state, validation, schema loading, impact prediction, and apply orchestration. **MultiSourceGrid.tsx** (341 LoC) did grid layout, tile rendering, focus state, and bulk start/pause fan-out. Testing either required booting the whole page and mocking 5 endpoints. Adding a new tunable meant reading the full flow first.

#### Fix

**Settings decomposed** ([features/settings/](../frontend/src/features/settings/)):
- [SettingsPage.tsx](../frontend/src/features/settings/SettingsPage.tsx) — 200-LoC orchestrator. Calls three hooks, composes three presentational components. No fetching, no validation logic.
- [Tunable.tsx](../frontend/src/features/settings/components/Tunable.tsx) — compound component (Tunable.Label / .Control / .Meta via context).
- [ImpactCard](../frontend/src/features/settings/components/ImpactCard.tsx), [OpsDeltas](../frontend/src/features/settings/components/OpsDeltas.tsx), [ApplyResultBanner](../frontend/src/features/settings/components/ApplyResultBanner.tsx) — pure presentation.
- Hooks own the state machines: [useSettings](../frontend/src/features/settings/hooks/useSettings.ts), [useSettingsApply](../frontend/src/features/settings/hooks/useSettingsApply.ts) (draft → apply lifecycle with optimistic rollback), [useImpact](../frontend/src/features/settings/hooks/useImpact.ts).

**Admin decomposed** ([features/admin/components/](../frontend/src/features/admin/components/)):
- [MultiSourceGrid.tsx](../frontend/src/features/admin/components/MultiSourceGrid.tsx) — 184 LoC layout only; focus state lifted to `AdminPage`.
- [StreamTile](../frontend/src/features/admin/components/StreamTile.tsx), [StreamImage](../frontend/src/features/admin/components/StreamImage.tsx), [SelectedStreamHeader](../frontend/src/features/admin/components/SelectedStreamHeader.tsx) — reusable pieces.
- [useLiveSources](../frontend/src/features/admin/hooks/useLiveSources.ts), [useStreamControl](../frontend/src/features/admin/hooks/useStreamControl.ts) — fetch + mutation hooks.

#### Non-obvious details

1. **Pure components are easy to refactor.** `ImpactCard` takes `report` as a prop; to change the severity palette, edit one file without understanding `useImpact`'s polling cadence.
2. **Hooks own state machines; components own layout.** `useSettingsApply` holds ~200 LoC of draft-to-effective mirroring, 422/409/429 error classification, privacy-confirm flow, optimistic rollback. Tested by mocking query responses — no component mount.
3. **Compound components for rich compositions.** `Tunable` uses context so children don't need spec+onChange drilled through props.

#### Impact

- **Unit-testable pieces.** `OpsDeltas` tested with a fixture in <1s — no mock server.
- **Junior-friendly edits.** "Add an impact metric" = edit one 123-LoC pure file.
- **Reusable.** `StreamImage` could ship in the dashboard or a settings preview without dragging the grid with it.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Split with comments** (`// Section A`) | Grep-friendly, but one file, one test, one module boundary. No reusability or testability gain. |
| **Class components + inheritance** | Non-idiomatic React. Harder to test (instantiate + spy on methods). Hooks own shared logic cleanly. |
| **Keep monolith + refactor discipline** | A new feature always balloons the existing page. Six months in, it's 600 LoC again. Proactive decomposition is cheaper. |

**One-liner.** *"Broke 431- and 341-LoC files into single-responsibility components and hooks — each testable in isolation, reusable across features, small enough for a junior to edit safely."*

## 3. Network

#### Problem — what we had before

```tsx
// Before: each feature rolled its own fetch
fetch(`/api/source/${id}`)
  .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
  .then(setData)
  .catch(err => setError(err.message));
```

Every feature doing this paid the same taxes:

1. **Different error shapes.** Admin threw `Error(msg)`, Settings expected `{ status, detail }`, Watchdog a raw status. No uniform handler.
2. **No `AbortSignal`.** Unmount → response arrives → `setState` on dead component. Memory leak + React warnings.
3. **Stale HTTP cache hits.** No `cache: "no-store"` → Firefox/Safari returning 2-min-old responses on a real-time console.
4. **No structured 422/429/409 handling.** Validation errors came back as `{errors:[...]}` but each caller re-parsed. 429 `Retry-After` header went unread.
5. **Parsing dance per feature.** Five files, five ways to handle errors.

#### Fix — one helper, three shapes

[fetchClient.ts:26-37](../frontend/src/shared/lib/fetchClient.ts#L26-L37) — `fetchJson` for lightweight reads. Non-2xx → `Error(msg)`, callers stay terse.

[fetchClient.ts:44-63](../frontend/src/shared/lib/fetchClient.ts#L44-L63) — `postJson` wraps `fetchJson` with JSON-serialized body + auto Content-Type.

[fetchClient.ts:113-170](../frontend/src/shared/lib/fetchClient.ts#L113-L170) — `apiFetch` for endpoints where UI cares about error details. Throws structured `HttpApiError`:

```ts
interface HttpApiError extends Error {
  status: number;
  body: unknown;
  retryAfterSec?: number;
}
```

Composes with TanStack Query: `queryFn: ({ signal }) => fetchJson(url, { signal })` — unmount aborts in-flight request.

#### Non-obvious details

1. **`cache: "no-store"` is the default.** Real-time ops console cannot afford Firefox/Safari returning cached responses.
2. **`HttpApiError` carries `body`, not just `status`.** 422 validation `{key, reason}` tuples surface to the UI without per-caller JSON.parse.
3. **`Retry-After` parsing is first-class.** 429 produces "Retry in 12s" + `err.retryAfterSec` for dialogs.
4. **Content-Type auto-set when body present.** One less footgun.
5. **Three helpers, not one.** Reads stay terse (`fetchJson`); writes that care about structured errors upgrade to `apiFetch`.

#### Impact

- Uniform error shape across every feature's `api.ts` — one place to read how errors work.
- Auto-cancel on unmount via `AbortSignal`.
- Debugging network issues = read one 97-line file.
- 422/429 surface their details (field errors, retry countdown) without per-feature plumbing.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **`axios`** | ~15 KB gzipped, no benefit over native `fetch` + our thin wrapper. |
| **Generated client from OpenAPI** | Needs `response_model=` on 100% of FastAPI handlers first. Future work. |
| **`ky` / `wretch`** | Another dep for a wrapper we already have. |
| **Per-feature fetch wrappers** | Where we started. Five files drift in five directions. |

**One-liner.** *"Three fetch helpers + `HttpApiError` standardize error handling app-wide. 97 lines own the story: structured errors, Retry-After, AbortSignal, no-store cache."*

### 3a. Live video transport — just poll *(transport choice)*

#### The problem

Every major browser caps **HTTP/1.1 at 6 concurrent connections per origin**. A long-lived push transport (e.g. `multipart/x-mixed-replace`) holds one TCP connection open per tile — 8 tiles + 1 SSE feed = 9 persistent connections > 6 → the browser silently queues new requests behind the streams, SSE stalls, status polls hang, UI freezes mid-frame. It *looks* like the backend died; it hasn't. HTTP/2 behind a reverse proxy dissolves the cap (all streams multiplex over one TCP connection), so production would be fine but `npm run dev` deadlocks at tile #5.

#### Fix — polling-only

Perception runs at `TARGET_FPS = 2`. New source frame every ~500ms. Poll cadence is ~400ms. At that regime **push delivery is theatrical below the inference period** — you cannot deliver a frame that hasn't been produced yet. One poll cycle picks up every JPEG the edge emits.

Every tile polls `GET /admin/frame/{id}` every ~400ms with a `?t=<now>` cache-buster. Four lines of loop, same `<img>` tag, no module-load decision, no override, no capability negotiation.

[StreamImage.tsx:47-66](../frontend/src/features/admin/components/StreamImage.tsx#L47-L66) — the whole component, stripped to essentials:

```tsx
export function StreamImage({ source, className, onError }: StreamImageProps) {
  const baseUrl = `/admin/frame/${encodeURIComponent(source.id)}`;
  const [tick, setTick] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(
      () => setTick(Date.now()),
      POLL_INTERVAL_MS.streamImageFrame,
    );
    return () => window.clearInterval(id);
  }, [source.id]);

  return <img src={`${baseUrl}?t=${tick}`} alt={source.name} className={className} onError={onError} />;
}
```

One `useEffect`, one `<img>`, one cache-buster. Server-side: `GET /admin/frame/{id}` hands back a single JPEG and closes the connection — the browser's 6-conn cap is irrelevant because no request is long-lived.

`has_viewers` on `StreamSlot` is a single recent-poll timestamp check: any hit within 2s counts as "watched" and keeps the annotated-JPEG encode path hot; longer than that and the perception loop stops spending CPU on a frame nobody sees.

#### Impact

- **One code path, one endpoint.** No transport branch in the codebase.
- **No deploy asterisk.** Edge runs equally well on plain HTTP/1.1 or behind any proxy.
- **No dev/prod drift.** The thing on your laptop is the thing in prod.
- **Same UX.** At 2 fps, nobody can tell the difference between ~200ms push latency and ~400ms poll cadence.
- **Backend idle savings preserved** — `has_viewers` short-circuits JPEG encode on idle tiles.

#### Alternatives considered

| Option | Verdict |
| --- | --- |
| **Server-told capability** (`GET /admin/video_caps` returns `"push"` or `"poll"`) | More honest than a client-side protocol guess, but still two endpoints, multipart framing, viewer counting. More code than "just poll." |
| **WebRTC** | Sub-100ms — but at 2 fps source rate, it's chasing latency below the frame period. Plus STUN/TURN/SFU infra, SDP, ICE. Operational cost not justified. |
| **HLS / DASH** | 6–10s baseline latency — kills the "live" feel the UI exists for. |
| **Polling everywhere (what we picked)** | ~400ms cadence matches the perception period; one endpoint; no deploy note; same code locally and in prod. |

#### When this stops being right

Polling's ceiling: **~10 fps sources** or **>30 simultaneous tiles on cellular**. At that point the per-poll HTTP overhead adds up and push actually earns its complexity. Realistic escape hatches in order:

1. **Server-told capability + a push transport** behind a feature flag for the specific deployment that needs it.
2. **H.264 over WebSocket** — one codec, efficient framing, still single-transport.
3. **WebRTC** — if latency genuinely matters and you're willing to run the signalling/TURN infrastructure.

Not our ceiling today; documented here so the next person reading this doesn't have to rederive the decision.

**One-liner.** *"At 2 fps, push latency below the inference period is invisible — polling at ~400ms catches every frame the edge produces. One endpoint, one code path, no deploy-time HTTP/2 constraint."*

## 4. State management — why TanStack Query beats hand-rolled polling

### Problem — what we had before

```ts
useEffect(() => {
  const id = setInterval(async () => {
    const data = await fetch("/api/live/status").then(r => r.json());
    setStatus(data);
  }, 5000);
  return () => clearInterval(id);
}, []);
```

Every component doing this paid the same taxes:

1. **N components = N requests.** TopBar polls `/api/live/status`, DashboardPage polls it, SettingsPage polls it — three `setInterval`s, three network calls every 5s, for the same bytes.
2. **Zombie writes.** Request fires, component unmounts, response comes back → `setState` on a dead component (or worse, on a component now showing a different source, so you paint stale data into the wrong context).
3. **Background-tab waste.** Laptop in another space keeps hammering the edge node at 5s cadence for a UI nobody is looking at.
4. **No focus revalidation.** Operator Alt-Tabs back after 10 minutes → they stare at 10-minute-old data until the next tick.
5. **Mutation invalidation is manual.** Apply a settings diff → nothing else knows the effective values changed. Each consumer needs its own `setEffective(newVal)` plumbing, or a bus, or a context.

### Fix — what the repo does now

[queryClient.ts:25-37](../frontend/src/shared/lib/queryClient.ts#L25-L37) configures one cache for the whole app. Every page reads from it. [useLiveStatus.ts:46-51](../frontend/src/shared/hooks/useLiveStatus.ts#L46-L51) is the entire polling hook:

```ts
useQuery({
  queryKey: ["shared", "liveStatus"],
  queryFn: ({ signal }) => fetchJson<LiveStatus>("/api/live/status", { signal }),
  refetchInterval: 5000,
});
```

What you get **for free** from those four lines:

| Property | Why it matters |
| --- | --- |
| **Dedupe by `queryKey`** | TopBar + Dashboard + Settings all call `useLiveStatus()` → one `fetch` in flight, three subscribers. |
| **`signal` → AbortController** | Unmount cancels the in-flight request. No zombie writes, no wasted bytes. |
| **Pause on hidden tab** | `refetchInterval` honours `document.visibilityState`. Background tabs go quiet. |
| **Refetch on window focus** | Operator returns → fresh data before they can react. |
| **`staleTime: 5_000`** | Mount a fourth subscriber within 5s → it gets the cached value instantly, no network hit. |
| **`gcTime: 5 * 60_000`** | Route away and back within 5 min → cache is warm, no flash of loading. |
| **`retry: 1`** | One transient network blip doesn't paint an error state. |
| **Exposed `refetch`, `isLoading`, `isError`, `error`** | Standard shape across every hook — no per-feature reinvention. |

### Mutations + cache invalidation

Settings is the clearest win. [useSettings.ts:133-151](../frontend/src/features/settings/hooks/useSettings.ts#L133-L151):

```ts
const apply = useCallback(async (diff, opts) => {
  const result = await settingsApi.apply(diff, { ... });
  await effectiveQuery.refetch();                                    // pull new truth
  void qc.invalidateQueries({ queryKey: settingsQueryKeys.impact }); // preview rebaselines
  return result;
}, [...]);
```

`invalidateQueries` marks a key stale → every mounted subscriber on that key auto-refetches. No event bus, no prop drilling, no "did everyone remember to update?" The `ImpactCard` re-renders with the new baseline because it happens to be subscribed to `settingsQueryKeys.impact`, not because `apply` knew about it.

### Optimistic UI with rollback

The `onMutate` / `onError` / `onSettled` lifecycle of `useMutation` gives this pattern cleanly:

```ts
useMutation({
  mutationFn: toggleStream,
  onMutate: async (next) => {
    await qc.cancelQueries({ queryKey: streamKey });            // cancel races
    const prev = qc.getQueryData(streamKey);                    // snapshot
    qc.setQueryData(streamKey, (old) => ({ ...old, active: next })); // paint now
    return { prev };                                            // ctx for rollback
  },
  onError: (_err, _vars, ctx) => qc.setQueryData(streamKey, ctx.prev), // rollback
  onSettled: () => qc.invalidateQueries({ queryKey: streamKey }),      // reconcile
});
```

Hand-rolled: you'd track three states (`prevValue`, `optimisticValue`, `inFlight`), wire rollback by hand, and probably still race with a concurrent poll. Here it's declarative.

### Why the `usePolling.ts` deletion matters

A rule enforced by convention ("please use TanStack Query") rots. A rule enforced by **deleting the alternative** is load-bearing — a new contributor who reaches for `setInterval` has no shared helper to import, grep finds nothing, and [`.claude/rules/frontend.md`](../.claude/rules/frontend.md) explicitly says "do not reintroduce it." That's the difference between a style guide and a guarantee.

### Alternatives, concretely

- **Redux Toolkit + RTK Query.** Works, but every endpoint is a slice with reducers and selectors; caching is first-class but Suspense/AbortController integration is rougher; the ceremony isn't justified for a POC with ~20 endpoints.
- **SWR + Zustand.** SWR's read story is comparable; mutations are thinner (no `onMutate` rollback ergonomics), and you end up pairing it with Zustand for anything non-trivial — so now you have two libraries doing what one would.
- **Custom hook on top of `fetchClient`.** Where we started. Every tax above comes back.

TanStack Query is the one that makes the rule ("no hand-rolled polling") cheap enough to actually follow.

**One-liner.** *"We replaced N hand-rolled pollers with one shared cache. Same UI, a third of the network calls, mutation invalidation is now declarative instead of something every feature has to remember."*

## 5. Hooks / lifecycle — single SSE owner

#### Problem — four tabs, four connections, four zombies

Every component that wanted the safety feed mounted its own `new EventSource("/stream/events")`. Two tabs on the same page = two concurrent connections, two backoff policies, two onmessage buffers — all consuming the same feed. The edge node held N sockets for what should have been one. Event ordering drifted across components: page A saw an incident 200ms before page B. On network errors, unmount left connections half-dead.

```tsx
// Antipattern — pre-refactor
useEffect(() => {
  const es = new EventSource("/stream/events");
  es.onmessage = (ev) => setEvents((prev) => [JSON.parse(ev.data), ...prev]);
  return () => es.close();
}, []);
```

#### Fix — one provider, many consumers

[shared/events/EventStreamProvider.tsx](../frontend/src/shared/events/EventStreamProvider.tsx) mounts once at the app root in [app/providers.tsx:34](../frontend/src/app/providers.tsx#L34) and owns the single `/stream/events` EventSource. Split contexts fan out data + connection status separately so consumers subscribe narrowly.

Low-level primitive: [useSSE.ts](../frontend/src/shared/hooks/useSSE.ts) — generic hook, exponential backoff (2s → ×1.5 → cap 30s, reset on open), StrictMode-safe cleanup via a `stopped` flag.

**The rule: one owner per stream URL, not one EventSource per app.** [features/admin/hooks/useDetections.ts](../frontend/src/features/admin/hooks/useDetections.ts) opens its own EventSource to `/admin/detections` — different stream, own hook. No overlap.

#### Non-obvious details

1. **Provider lives outside the router.** Mounted in `providers.tsx` above `<BrowserRouter>`, so route transitions never kill the connection.
2. **Backoff resets on successful open.** Otherwise a recovered reconnect would start its next failure at the 30s cap.
3. **Multiple streams are fine** — the rule is one owner per *URL*, not "only one EventSource."
4. **Consumers are listeners, not connection owners.** Context exposes subscribe/unsubscribe, never the raw EventSource.
5. **StrictMode double-invoke safety.** `stopped` flag prevents a pending reconnect timer from opening a socket after cleanup fires — the classic race on dev unmount.

#### Impact

- One open connection per distinct stream per tab.
- Backoff/reconnect logic in one file.
- Consistent event ordering — everyone reads from the same buffer.
- "Is the SSE alive?" is one question, not N.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Redux middleware on WebSocket** | SSE is one-way. Redux + WS adds framing/reconnect code that SSE gives for free. |
| **Shared Worker** | Real cross-tab dedupe, but devtool support is uneven. Complexity not justified for a POC. |
| **Long polling** | Higher latency, HTTP overhead per poll, same connection-cap pressure. |
| **Per-feature EventSources** (what we had) | N pages × M features = chaos. Zombie connections on fast navigation. |

**One-liner.** *"Hoisted the single `/stream/events` EventSource to the app root, replaced N per-page duplicates with context subscribers, backoff in one place. Same UX, one connection, consistent event order."*

## 6. UI / reusable components

#### Problem

Each feature hand-rolled its own Button, Card, Dialog, Badge. `SettingsPage` had inline buttons with feature-local CSS modules; `AdminPage` had hardcoded `#2563eb`; five pages used five different border radiuses. Changing the primary color meant grepping across three folders. One miss = shipped inconsistency.

#### Fix

All primitives in [shared/ui/](../frontend/src/shared/ui/): `Button`, `Card`, `Dialog`, `Input`, `Pill`, `RiskBadge`, `Section`, `Skeleton`, `Spinner`, `Tabs`, `Tag`, `EmptyState`, `ErrorBoundary`, `ErrorList`, `Dot`, `EventFilterBar`. Re-exported from [index.ts](../frontend/src/shared/ui/index.ts) so features import once: `import { Button, Card, Pill } from "../../shared/ui"`.

Colors driven by CSS variables (`var(--border)`, `var(--high)`) in per-component `.module.css` files — a palette change lands in one file.

Layout primitives split off to [shared/layout/](../frontend/src/shared/layout/) (`PageChrome`, `TopBar`, `PageLayout`). Cross-feature event rendering in [shared/events/](../frontend/src/shared/events/) (`EventCard`, `EventDialog`, `FeedbackButtons`) — admin and dashboard consume the same card, no duplication.

#### Non-obvious details

1. **CSS Modules, not Tailwind.** Each component owns scoped class names. No global cascades, no "which file defines `.btn`?" mystery.
2. **Graceful degradation on unknown data.** `RiskBadge` accepts `level: string`, not `level: "high" | "medium" | "low"` — a future backend `"critical"` falls back to low-severity style rather than crashing.
3. **Compound philosophy varies by component.** `Pill` is a lightweight container (45 LoC) — caller styles the contents. `RiskBadge` is complete — maps event risk level to colors.

#### Impact

- **Consistency enforced by code.** Primary-color tweak lands in `Button.module.css`, ships everywhere.
- **No widget sprawl.** New page can't hand-roll a Dialog — there's only one Dialog to reach for.
- **Features compose primitives, not inherit duplication.** SettingsPage owns zero presentational components.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **MUI / Chakra / shadcn** | +100–300 KB bundle, opinionated design system, upkeep cost. Bespoke components stay lean and on-brand. |
| **Tailwind + utility classes** | Class-name soup per feature; "is it `flex gap-2` or `space-x-2`?" CSS Modules give one source of truth per component. |
| **Styled-components / Emotion** | Runtime CSS-in-JS adds bundle + per-render cost. CSS Modules are static. |

**One-liner.** *"Primitives in a single shared barrel — color change in one file ships everywhere. Features compose building blocks instead of inventing their own."*

## 7. Type safety — Pydantic → TS codegen

#### Problem

Two sources of truth. Backend [backend/api/models.py](../backend/api/models.py) declared Pydantic models; frontend `shared/types/common.ts` (334 LoC, hand-maintained) mirrored them. They drifted immediately. A backend rename — `plate_text` → `plate_hash_hex` — got mirrored; two weeks later a component still imported the old name and compiled cleanly (TS didn't know the field was gone at runtime). SSE pushed `plate_hash_hex`, the component read `.plate_text`, got `undefined`, painted empty.

Worse: adding a new model type to `models.py` wasn't an error in `common.ts` — it just silently wasn't there.

```python
class EventModel(BaseModel):
    plate_hash_hex: str          # renamed
    risk_level: Literal["high", "medium", "low"]
```
```ts
export interface SafetyEvent {
  plate_text: string;             // ❌ backend never sends this
  risk_level: "high" | "medium" | "low";
}
// Component compiles; crashes at runtime.
```

#### Fix

[scripts/generate_ts_types.py](../scripts/generate_ts_types.py) (391 LoC) walks `EXPORTED_MODELS` in [backend/api/models.py](../backend/api/models.py), converts each Pydantic class's JSON Schema to TypeScript, and writes [frontend/src/shared/types/generated.ts](../frontend/src/shared/types/generated.ts) (599 LoC, machine-written).

**Load-bearing**: [start.py:163](../start.py#L163) runs it *before* `npm run build`. A backend rename fails the next build — caught at compile time, not in prod. `common.ts` shrank to a 50-line re-export shim.

The codegen handles: `$ref` (cross-model refs), `oneOf`/`anyOf` (unions + `Optional[X]`), `enum` (Pydantic Literals), `type: object` (interfaces), `prefixItems` (fixed tuples). It uses pydantic's `model_json_schema(mode="serialization")` so emitted TS matches what the API actually sends.

#### Non-obvious details

1. **No npm dependency.** Hand-rolled walker — full control, no Node prereq for the Python side.
2. **Build-time enforcement.** `start.py` regenerates before the frontend build; this is what "source of truth" looks like at scale.
3. **Literal unions, not strings.** `Literal["high", "medium", "low"]` → `"high" | "medium" | "low"`. Passing `"critical"` is a compile error.
4. **Tuples preserve indexing.** `tuple[float, float, float, float]` (bbox) → `[number, number, number, number]`. `bbox[0]` works under `noUncheckedIndexedAccess`.
5. **Name overrides strip `Model` suffix.** `EventModel` → `SafetyEvent` via `TS_NAME_OVERRIDES` — frontend stays idiomatic without losing pydantic conventions.
6. **Optional handling.** `field: X | None = None` → `field?: X` (absence = `undefined`, never explicit `null`).

#### Impact

- Wire contract is generated, not mirrored.
- Field rename in Pydantic → TS compile error on next build.
- Literal unions catch programmer errors at edit time (IDE), not at runtime.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **openapi-typescript** | Needs `response_model=` on 100% of FastAPI handlers first. Future work. |
| **pydantic2ts** | Requires Node at type-check time. Bespoke 60-line walker gives full control over naming + format. |
| **Hand-written + linters** | The status quo. Human error is silent. |

**One-liner.** *"Backend models in Pydantic → TypeScript via a 391-LoC codegen run before every build. A field rename fails `npm run build`, not production."*

## 8. Performance

#### Problem

One bundle loading every feature — admin tiles, settings, watchdog, validation — upfront. New code on any route slowed first paint globally. One feature's unhandled error crashed the whole SPA with no recovery path. Background tabs kept polling every 5s, hammering the edge for UI nobody was watching.

#### Fix

**Lazy routes** ([app/router.tsx:22-34](../frontend/src/app/router.tsx#L22-L34)): every page (`AdminPage`, `DashboardPage`, `SettingsPage`, `MonitoringPage`, `ValidationPage`) is `React.lazy`-imported — each becomes its own Vite chunk. Every route wrapped in `<RouteShell>` ([router.tsx:36-42](../frontend/src/app/router.tsx#L36-L42)) which composes `<ErrorBoundary>` + `<Suspense>` so one page's crash leaves others alone.

**Background polling pause**: non-critical queries set `refetchIntervalInBackground: false` ([useSettings.ts:111](../frontend/src/features/settings/hooks/useSettings.ts#L111), [useImpact.ts:69](../frontend/src/features/settings/hooks/useImpact.ts#L69)). Operator switches tabs → polling stops. Returns → `refetchOnWindowFocus: true` pulls fresh data immediately.

**Optimistic mutations**: live-source toggles ([useStreamControl.ts:75-111](../frontend/src/features/admin/hooks/useStreamControl.ts#L75-L111)) patch the cache, roll back on error. In-flight state is `isPending` from TanStack Query, not a hand-rolled busy map.

#### Non-obvious details

1. **Each chunk is independent.** First paint to `/settings` downloads settings + shell, not admin + watchdog + validation.
2. **Suspense fallback during lazy load.** `<RouteFallback>` shows a spinner so network latency is visible, not a silent hang.
3. **Background-tab idle is ~zero cost.** Hidden settings tab stops polling; `gcTime: 5*60_000` then GCs the cache entry.
4. **No hand-rolled timeouts.** Mutations simply hold `isPending` until server responds. A 5–8s escape timer would hide real failures; operators can always reload.
5. **Optimistic rollback on failure.** If `POST /api/live/sources/{id}/start` errors, `onError` restores pre-mutation cache — UI never lies.

#### Impact

- First paint limited to one page's deps (~30 KB gzipped).
- Error on `/settings` = one-page recovery, not full reload.
- Hidden tabs cost ~zero bandwidth.
- Mutation state driven by TanStack Query's lifecycle, no custom state machine.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Next.js SSR** | Operator dashboard — nothing to SSR. Hydration mismatches + API routes for no UX gain. |
| **Service worker + stale cache** | Offline-first pattern for an always-connected device. Adds complexity for no benefit. |
| **Hand-rolled busy state + timeout** | `isPending` is the source of truth. A 5–8s fake timeout hides real failures. |

**One-liner.** *"Lazy-load each page as a separate bundle, wrap them in error boundaries, pause non-critical polling in background tabs. One feature's crash doesn't cascade; idle tabs don't drain bandwidth."*

## 9. Error handling & lifecycle

#### Problem

One component throws during render → whole SPA white-screens. No feature-level containment, no recovery path, no visibility into which feature failed.

#### Fix

**Per-route `<ErrorBoundary>` + `<Suspense>`**: every lazy route wraps in `<RouteShell>` ([app/router.tsx:36-42](../frontend/src/app/router.tsx#L36-L42)). A throw inside `/settings` is caught by its boundary, renders a "Something broke. Try again" panel, leaves `/admin` untouched. [shared/ui/ErrorBoundary.tsx](../frontend/src/shared/ui/ErrorBoundary.tsx) is a class component (React's only built-in catch mechanism) with reset() + reload() buttons.

**AbortSignal threading**: every `useQuery` / `useMutation` receives an `AbortSignal` from TanStack Query. [fetchClient.ts:131](../frontend/src/shared/lib/fetchClient.ts#L131) forwards it to `fetch()`. Unmount → in-flight requests cancel. No zombie writes, no StrictMode warnings.

**DialogProvider centralizes modals**: [shared/ui/Dialog.tsx:121-146](../frontend/src/shared/ui/Dialog.tsx#L121-L146) owns a FIFO queue of pending dialogs using the native `<dialog>` element. Every feature calls `useDialog()` for privacy confirms, rate-limit warnings, revision conflicts. One styling, one a11y behavior.

#### Non-obvious details

1. **Class component is load-bearing.** React 19 still has no hook-based error boundary. Class is forced by the API.
2. **Fallback UI includes recovery.** "Try again" calls `reset()` to clear the boundary's error state; "Reload page" does a hard refresh.
3. **AbortSignal fires on unmount OR refetch.** TanStack Query cancels before refetching; feature code never calls `abort()`.
4. **Dialog queue, not stack.** Two simultaneous mutation failures → dialogs queue FIFO, never overlap.
5. **StrictMode console stays clean.** AbortSignal + per-route boundaries eliminate unmount-phase warnings.

#### Impact

- Partial failure instead of total. One feature crashes, others keep working.
- No orphan requests → no setState-on-dead-component warnings.
- Consistent error UX across features; one dialog system.
- Clean dev console under StrictMode double-render.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Global error boundary only** | Catches everything, no per-route recovery. Operator has to reload to use any other feature. |
| **Try/catch in every component** | Doesn't catch render-phase errors. Duplicates fallback UI. |
| **ErrorBoundary on every component** | Overkill. Route-level isolation is the right granularity. |
| **Custom modal stacks** | Native `<dialog>` + FIFO queue is cleaner and has better a11y (focus management, Esc-to-cancel). |

**One-liner.** *"Every route wraps in error boundary + Suspense; requests auto-cancel on unmount; modals queue instead of stack. One page's crash doesn't touch others; unmount doesn't orphan requests; errors surface consistently."*

## 10. Documentation / in-file discoverability
- **Problem.** Tracing a SafetyEvent from SSE → React hook → rendered component meant grepping `/api/*` string literals. JSDoc coverage was uneven — some files had rich headers, most had none. Onboarding leaned on `CLAUDE.md` + tribal knowledge of *which hook hits which endpoint*.
- **Fix.** One-pass annotation of **126 FE files**. Every page / hook / component gets a file-level header naming the BE endpoint it calls and the role it plays. JSDoc above every export describes props, child elements, and the endpoint-and-effect it triggers. Inline notes on SSE reconnect/backoff, optimistic-mutation lifecycle (`onMutate`/`onError`/`onSettled`), polling cadence, severity-color mapping. Zero code changes — `tsc --noEmit` clean after the pass.
- **Impact.** `grep "/api/events/stream"` now returns a JSDoc block explaining who consumes it, with what hook, in which component. IDE hover on `useLiveStatus` → full contract. New contributors read one file instead of three.
- **Alternatives.** *Architecture doc only* (what `docs/architecture.md` does — global view, no per-file wayfinding). *Typedoc site* (more infra; current comments already surface in IDE tooltips). *No annotations* (`CLAUDE.md`'s default; overrode here to optimize FE↔BE traceability).

## FE — Critical bugfixes
1. **Zombie state writes on unmount** → `AbortSignal` threaded through `apiFetch` + every `useQuery`.
2. **Stuck "applying…" state** on settings apply → 8-second escape timer + explicit failure banner.
3. **Browser connection exhaustion** at 5+ tiles → polling-only (each poll closes promptly, stays under the 6-conn cap).
4. **Duplicated SSE subscribers** (N per open tab) → shared `<EventStreamProvider>`.
5. **Silent type drift** between BE and FE → Pydantic → TS codegen pipeline.
6. **White-screen crashes** from one feature taking out the rest → per-route error boundaries.

## FE — Best practices applied
- Feature folders > type folders (ownership beats categorization).
- One place for the fetch layer (errors, abort, headers — one file).
- Server state ≠ client state — cache (TanStack) for first; `useState`/context for second.
- Codegen the contract; never hand-mirror backend types.
- Lazy-load per route — isolate bundles *and* failures.
- Always thread `AbortSignal` on unmount.
- Document rules (`.claude/rules/frontend.md`) so they outlive memory.

## FE — Best judgments (what I chose *not* to do)
- **No Redux.** Server state is a cache problem, not a store problem.
- **No design system import.** Bespoke `shared/ui/` fits the operator-dashboard look.
- **No SSR.** Operator SPA with live streams — nothing to server-render.
- **No WebRTC.** At 2 fps, frame-period latency is invisible — infra tax not worth it.
- **Templates UI trimmed back** (BE endpoints retained) — shipped core apply+impact loop first.

---

# Backend (BE)

## 1. Project structure

#### Problem

`backend/server.py` was a **1,535-line monolith** mixing 37 HTTP routes with business logic, live state, and domain models. Every feature landed as a 300-line diff in the same file = merge-conflict magnet. Testing one route required booting the whole app — perception threads, LLM connection, watchdog loop.

#### Fix

Split into 9 packages, each one concern: [backend/{core, perception, services, api, integrations, compliance, security, rendering, domain}](../backend/). [server.py](../backend/server.py) is now 215 lines — composition root that wires lifespan, iterates 15 feature routers, serves static assets. Nothing else.

[state.py](../backend/state.py) shrank from 791 → 415 LoC; mutable domain objects (`Episode`, `StreamSlot`) moved to [backend/domain/](../backend/domain/) and can be instantiated in tests without app boot.

Tests mount one router into a fresh `FastAPI()` — [tests/test_settings_api.py](../tests/test_settings_api.py) is 14 lines for a full isolation harness.

#### Non-obvious details

1. **Composition root has no business logic.** Handlers import their own deps directly from services/domain.
2. **Test isolation falls out for free.** `backend/api/routers/live.py` depends on `state`, `config`, `models` — not YOLO, not clip rendering. Exercisable without GPU.

#### Impact

- New features land as new files, not diffs in the composition root.
- Each module independently testable. Watchdog-rules PR doesn't boot perception.
- Code review scoped per package — the reviewer asks "does this router mount its deps?" not "does the 1,535-line file still compile?"

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Hexagonal/clean architecture** | Triples file count. Ports/adapters/use-case interactors unjustified for single-process POC. |
| **Keep monolith + in-file `APIRouter` blocks** | Still merge-conflict magnet. Can't mount routers into isolated test apps. |

**One-liner.** *"Split the 1,535-line monolith into 9 packages; `server.py` is 215 lines of composition root. New features land as new files; each module is independently testable."*

## 2. Giant / massive files

#### Problem

`watchdog.py` was 1,800 LoC mixing rules (deterministic detection) + AI (Claude hypotheses) + storage (JSONL I/O) + orchestration (background loop). Fixing one concern meant reading four. A junior testing the fingerprint-collision tiebreaker (rules win over AI) had to understand the entire loop just to stub the LLM. `state.py` (791 LoC) was similarly tangled. Remaining: [services/impact.py](../backend/services/impact.py) (825 LoC), [api/settings.py](../backend/api/settings.py) (659 LoC).

#### Fix

`watchdog.py` → [backend/services/watchdog/](../backend/services/watchdog/) package:
- [model.py](../backend/services/watchdog/model.py) — `WatchdogFinding` dataclass, fingerprinting, defaults, `make_finding` factory. Shape only, no I/O.
- [rules.py](../backend/services/watchdog/rules.py) — deterministic detectors. Always available; never imports the LLM.
- [ai.py](../backend/services/watchdog/ai.py) — strictly-additive Claude hypothesis. Returns `[]` when provider is down — truly optional.
- [storage.py](../backend/services/watchdog/storage.py) — append-only JSONL.
- [api.py](../backend/services/watchdog/api.py) — background loop + `stats()` + the rule-wins-on-fingerprint-collision invariant.

[state.py](../backend/state.py) 791 → 415 LoC; [Episode](../backend/domain/episode.py) (211 LoC) and [StreamSlot](../backend/domain/stream_slot.py) (346 LoC) moved to `backend/domain/` — instantiable without singletons.

#### Non-obvious details

1. **Rules-win invariant lives in api.py, not ai.py.** The AI module calls Claude and returns findings blind; `api.py` is where the dedupe rule (rule wins on fingerprint OR title collision) is applied. Keeps `ai.py` side-effect-free and testable in isolation.
2. **Stratified testability.** `test_watchdog.py` constructs `WatchdogFinding` directly (no I/O); `test_watchdog_rules.py` exercises `rule_checks()` without the loop; `test_watchdog_api.py` mocks rules to test dedup. Previously one test entrypoint ("boot everything, send a fake frame"); now five.

#### Impact

- Each concern testable in isolation.
- Monitoring survives LLM outages — rule layer carries on alone.
- New findings: add a rule to `rules.py`, add an AI check to `ai.py`, orchestration untouched.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Keep monolith + style guide** | Next person copies existing code, not the guide. |
| **Require both rule + AI for every finding** | AI availability becomes hard requirement. One Anthropic 529 = no findings. The whole point of the split was to make rules independent. |

**One-liner.** *"Split watchdog into model/rules/ai/storage/api and moved Episode/StreamSlot to domain/. Rules-first invariant means monitoring survives provider outages; each concern is independently testable."*

## 3. Network / API organization

#### Problem

37 routes as `@app.*` decorators in one file. Testing any single route required the full perception pipeline, global singletons, watchdog loop — all running. Small settings changes cascaded across unrelated routes.

#### Fix

15 feature routers under [backend/api/routers/](../backend/api/routers/): `live.py`, `sources.py`, `admin_health.py`, `sse.py`, `watchdog.py`, `spa.py`, `agents.py`, `admin_video.py`, and 7 smaller. Each declares `router = APIRouter()` at module level, decorates handlers with `@router.get(...)`, and [server.py](../backend/server.py) loops through a `_FEATURE_ROUTERS` tuple calling `include_router`.

Two special-case **function-mounts** ([feedback.py](../backend/api/feedback.py), [settings.py](../backend/api/settings.py)) expose `mount(app, ...)` instead — they need to close over stateful callbacks (`impact_monitor`, `impact_subscribers`, `on_feedback`) at mount time that a plain `APIRouter()` can't capture.

All request/response shapes live in [backend/api/models.py](../backend/api/models.py) (721 LoC Pydantic), used via `response_model=` and driving frontend type codegen.

#### Non-obvious details

1. **Routers are singleton module objects, not factories.** No request-scoped DI — appropriate for a single-process edge where all state is in-memory.
2. **Why feedback + settings are function-mounted.** They register callbacks during the mount phase (drift recompute, impact SSE). A plain module-level `APIRouter()` can't capture these at import — must be wired at mount time.
3. **Test isolation via fresh app.** [tests/test_settings_api.py](../tests/test_settings_api.py) builds a minimal `FastAPI()` and mounts *only* the settings router. No perception, no global state. Monolithic version required the whole app stack.

#### Impact

- Tests mount single routers into fresh apps — no global state, no perception boot.
- New endpoints land as new files in the right router.
- Each router is independently auditable — `live.py` owns all status/scene/event endpoints in one file.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Group by HTTP verb** | One feature spans 3 files; `GET /api/sources` and `POST /api/sources` in different modules. |
| **Class-based controllers** | Non-idiomatic FastAPI. Dependency overrides in tests become awkward. |
| **Keep monolithic `server.py`** | Testing any route needs the full app. Merge conflicts. |

**One-liner.** *"Extracted 15 feature routers so tests mount only what they need. One `FastAPI()`, one router, zero global state — testing a settings route no longer requires booting the perception loop."*

## 4. Performance & concurrency — SettingsStore snapshot isolation

#### Problem

Hot path at `TARGET_FPS` per source touches operator-tunable params every frame. Reading a mutable dict races — one key old, another new:

```python
ttc_thresh = config_dict["TTC_HIGH_SEC"]
# <-- Apply() rebinds here
conf_floor = config_dict["CONF_THRESHOLD"]  # Torn read
```

Lock-per-read burns CPU; lock-for-eval serializes perception.

#### Fix

[backend/services/settings_db.py](../backend/services/settings_db.py) `SettingsStore` — **snapshot isolation**:

1. **Readers:** `STORE.snapshot()` returns an immutable `MappingProxyType` — lock-free. Snapshot stays coherent even if a write lands mid-eval.
2. **Writers:** Under a short `RLock` — merge, validate, build a new immutable view, atomically rebind `self._snapshot`. Subscribers notified **outside** the lock, each in `try/except`.
3. **Rollback:** `last_known_good` captured before each apply → rollback is pointer swap.
4. **Lost-update:** FE sends `expected_revision_hash`; writer raises `RevisionConflict` (409) if advanced.

```python
with self._lock:
    before = self._snapshot
    merged = dict(before); merged.update(cleaned)
    if errors := settings_spec.validate(merged):
        raise SettingsValidationError(errors)
    self._last_good = before
    self._snapshot = MappingProxyType(merged)   # atomic rebind
# Subscribers run outside lock — crashes don't revert
```

#### Non-obvious details

1. **`MappingProxyType` is not a copy.** Read-only view. Old readers keep old proxy; new readers see new one. Zero copies per gate eval.
2. **`RLock` not `Lock`.** Re-entrant — subscribers can `snapshot()` without deadlock.
3. **Subscribers fire after swap.** Raise doesn't revert. Per-subscriber try/except isolates failures.
4. **Narrow filters.** `register_subscriber_for(keys=[...])` — LLM-bucket rebuild only on `LLM_BUCKET_CAPACITY` change.
5. **Validation before swap.** Cross-field rules checked; fail → no swap.

#### Impact

- Hot path nanosecond-cheap. No lock, no queue, no allocation.
- Operators tune at runtime with zero reader contention.
- No torn reads.
- Atomic rollback via pointer swap.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Mutex per read** | Wrong order of magnitude — microseconds of contention per frame. |
| **COW dict + queue** | Still allocates; queue latency on apply. |
| **SQLite per access** | Journal sync microseconds; reintroduces partial-read windows. |
| **Pydantic validation on every read** | ~100 μs × 50 gates/frame = ms of bloat. |

**One-liner.** *"Writers take a short lock and atomically rebind an immutable view; readers never lock. Each gate eval reads a coherent snapshot — operators tune at runtime with zero hot-path contention."*

## 5. State management — typed domain objects

#### Problem

Live state (per-source streams, viewer counts, detection toggles, active incidents) lived as nested dicts mutated everywhere in `server.py`. Failure modes:
- **No type safety.** `slot["episodes"]` could be anything.
- **Mutation from N places.** Route writing `state.streams[id]["viewers"]` racing with perception writing `last_frame_ts` — no explicit sync.
- **Hard to test.** A `StreamSlot` test had to boot the whole app.
- **No state-change hooks.** Operator toggling `detection_enabled` → perception only notices via polling.

#### Fix

Domain classes in [backend/domain/](../backend/domain/):

1. **[StreamSlot](../backend/domain/stream_slot.py)** (346 LoC) — one per source:
   - Immutable post-construction: `source_id`, `name`, `original_source`, `calibration`
   - Live fields: `reader`, `episodes`, `track_history`, `quality`, `ego`, `scene`, `stage_timings_ms`
   - Typed methods: `mark_polled()`, `has_viewers()`, `record_stage_ms()`, `status_dict()`
   - Two locks: `_frame_lock` (JPEG buffer) and `_stage_lock` (latency samples) — separate so health checks never block encode

2. **[Episode](../backend/domain/episode.py)** (211 LoC) — one per active incident:
   - Immutable: `event_type`, `pair`, `started_at`
   - Peak tracking: `peak_frame`, `peak_risk`, `frame_count`, `risk_frame_counts`
   - `emitted` one-shot flag — at most one event per episode
   - `final_risk()` applies sustained-risk downgrade (≥2 high-risk frames AND ≥1s duration, else downgrade)

3. **[state.py](../backend/state.py)** (415 LoC) — owns `LiveState` singleton + `slots: dict[str, StreamSlot]`. Re-exports `Episode`/`StreamSlot` for backwards compat.

Tests construct fresh objects directly:
```python
def test_episode_sustained_risk_downgrade():
    ep = Episode(event_type="pedestrian", pair=(1, 2), started_at=time.time())
    ep.update(..., risk="high", ...)  # Single frame
    assert ep.final_risk() == "low"   # Doesn't sustain → downgrade
```

#### Non-obvious details

1. **Per-slot calibration.** Each `StreamSlot` reads camera calibration at construction. Multi-camera rigs (front 1× + rear 0.5×) report accurate distances per camera.
2. **Two containers, opposite lifetimes.** `SettingsStore` for cold operator tunables; `StreamSlot` for hot per-source state (written every frame). Different tools for different rates of change.
3. **Episode emitted flag.** `emit.py::_flush_episode` bails early if `ep.emitted`. Guards against double-emit on multi-thread boundaries.
4. **Sustained-risk thresholds live with the logic.** `MIN_HIGH_RISK_FRAMES = 2` is a constant in `episode.py`, not a config knob — it encodes a detection-artefact filter, not operator preference.
5. **Lock scopes.** `_frame_lock` vs `_stage_lock` — health check never blocks JPEG encode. `LiveState.lock` held for microseconds only.

#### Impact

- Typed, self-documenting state — `Episode.final_risk()` returns a string, no ambiguity.
- No app boot for unit tests.
- Per-slot state is local — no leaks across cameras.
- Clear ownership — routes know what to lock; perception knows what's safe lock-free.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Module-level dicts** | Doesn't scale to multi-source; state leaks across cameras; no type safety. |
| **Redis / external KV** | Deploy complexity (failover, network hops) for a single-process edge. |
| **Pydantic models for state** | Validation on every mutation — state is hot; config is cold; different tools. |
| **One global `LiveState` with no slots dict** | Ties you to single-source forever. Slots dict gives room to grow. |

**One-liner.** *"Typed domain classes with narrow locks let tests construct fixtures directly and multi-source deployments partition state per camera — no leaks, no app-boot tax."*

## 6. Type safety — two-tier mypy

#### Problem

No static type checking. Refactor errors — renaming a model field, changing a return type — only surfaced at runtime, sometimes hours into a session. Operators would report a broken endpoint; trace back to a forgotten import or field rename two commits ago.

#### Fix

Two-tier `mypy` on the boundary, configured in [pyproject.toml](../pyproject.toml):

**Tier 1 — [backend/api/models.py](../backend/api/models.py) (721 LoC), fully strict.** Every field typed. Every model annotated. This is the wire contract — a rename here fails type check before it ships.

**Tier 2 — [backend/api/](../backend/api/) routers + [backend/services/llm.py](../backend/services/llm.py) (1,055 LoC), strict minus `disallow_untyped_defs`.** Handler return types are already constrained by `response_model=` (Pydantic validates); relaxing the annotation saves boilerplate without losing safety. Everything else stays strict — `disallow_incomplete_defs`, `disallow_untyped_calls`.

**Third-party relaxed.** `cv2`, `numpy`, `ultralytics`, `anthropic` exempt via `ignore_missing_imports`. Stubbing them would take weeks for marginal value.

**Companion pyright at `basic`** for instant IDE feedback. `make typecheck` runs both.

#### Non-obvious details

1. **Why narrow scope.** Perception (`backend/core/`) hot-path touches `numpy`/`cv2` heavily. Typing those modules requires stubbing OpenCV's 500-function C API. ROI near-zero — perception logic changes rarely; type errors there don't cascade to API surface.
2. **models.py is 100% typed.** Every field, every constraint via `Field(...)`, every Optional as `| None = None`. Generated TypeScript is guaranteed to match runtime shape.
3. **llm.py is strictly typed because it owns resilience.** Provider selection, rate budgeting, circuit breaker, cost tracking, plate hashing — the system's graceful-degradation layer. Type errors here cause outages, not cosmetics.
4. **mypy overrides pattern.** Base config strict + `[[tool.mypy.overrides]]` tightens `backend.api.models` and relaxes third-party. Config lives in one place; scales to N modules.

#### Impact

- Wire boundary type-checked end-to-end: Pydantic (runtime) + mypy (build) + TS codegen + FE strict TS.
- Refactor errors caught at check time, not in prod.
- IDE feedback instant via pyright.
- Two most failure-sensitive surfaces have zero type ambiguity.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **`pyright` only** | Strict is weaker than mypy strict on overloads/forward refs; no `disallow_untyped_defs` equivalent. |
| **`mypy --strict` everywhere** | Typing `numpy` hot-path = weeks. Marginal gain. |
| **No static typing** | Errors surface in prod or hours into dev sessions. |
| **Only runtime Pydantic validation** | Catches schema mismatches, not logic errors. |

**One-liner.** *"mypy-strict on the wire contract and LLM failover — the two places where type errors cause outages — and leave perception hot-path untyped. End-to-end safety; zero weeks of numpy-typing overhead."*

## 7. Privacy & security

Three distinct trust-boundary concerns, each with its own failure mode.

### 7a. Plate hashing at ingest (not egress)

#### Problem

License plate text is GDPR-regulated PII. "Scrub at egress" is convention — every consumer must remember. One forgotten `return event` = leak. Between vision-model output and broadcast-time scrub there are a dozen places raw text can appear: in-memory buffer, traceback, SSE queue, Slack cache, cloud publisher's disk queue. You can't egress-scrub something that's already spread across the runtime.

#### Fix

[backend/compliance/privacy.py:27-80](../backend/compliance/privacy.py#L27-L80) — `hash_and_strip_plate()` is the ingest choke-point:

```python
def hash_and_strip_plate(enrichment: dict) -> dict:
    plate_text = enrichment.pop("plate_text", None)
    enrichment.pop("plate_state", None)     # state narrows identity
    digest = hash_plate(plate_text)         # salted SHA256
    if digest: enrichment["plate_hash"] = digest
    return enrichment
```

Called immediately after vision output from [backend/services/llm.py::enrich_event](../backend/services/llm.py#L878). Before the dict reaches *any* caller — buffer, SSE, cloud publisher — `plate_text` and `plate_state` are gone. [perception/emit.py:173-174](../backend/perception/emit.py#L173-L174) keeps a defense-in-depth `pop()`; primary invariant is enforced at ingest.

Encoded in [.claude/rules/python.md](../.claude/rules/python.md#L21-L22) (only `llm.py` imports SDKs) and [CLAUDE.md](../CLAUDE.md#L54-L56) ("no raw plate text in any buffer").

#### Non-obvious details

1. **Why salt the hash.** Bare `SHA256("ABC123")` is trivially rainbow-tabled — plate numbers are low-entropy. Per-deployment salt defeats precomputation.
2. **Why pop state too.** (state, color, make, timestamp, camera location) tuple re-identifies without the plate number. Treat as PII.
3. **Defense in depth isn't a substitute.** `emit.py`'s `pop()` is a safety net. `enrich_event()` is the barrier.
4. **When it runs.** Only when `ROAD_ALPR_MODE=third_party`. Default (`off`) makes no vision call; hash is a future-proof gate.

#### Impact

- Plates exist for ~ns, not seconds. Future refactors cannot accidentally leak.
- Downstream code never has to remember — field doesn't exist for them to leak.
- Misconfigured cloud publisher leaks a hash, not the raw number.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Egress scrub** | Convention-based; one missed scrub = leak. |
| **KMS encrypt at rest, decrypt at egress** | Per-event KMS latency + plate briefly cleartext in decrypt-use-log-scrub window. |
| **"Don't serialize" field marker** | Works for JSON, not for Python tracebacks, in-memory buffers, Slack mishaps. |

---

### 7b. SSRF guard + per-IP rate limits

#### Problem

Operators add video sources via the UI by pasting a URL. That URL goes to `yt-dlp` / OpenCV. An attacker (or tricked operator) could paste `http://localhost:5432` (local Postgres), `http://169.254.169.254/latest/meta-data` (AWS IMDS), or `http://10.0.0.1` (router admin). The edge would try to fetch → internal services exposed.

Clip rendering is a second surface: hammering `GET /api/road/clip?event_id=X` forces expensive re-encodes, starving detection.

#### Fix

[backend/security/ssrf.py::validate_public_url](../backend/security/ssrf.py#L43-L148) rejects URLs resolving to private / loopback / link-local / multicast / reserved. Called before slot creation on `POST /api/live/sources`. Resolves the hostname via DNS and checks *every* returned address — hostile DNS could round-robin public+private.

[backend/security/rate_limit.py::clip_rate_limit_check](../backend/security/rate_limit.py#L78-L115) — per-IP token bucket: 3 tokens burst, 1 token per 20s (3/min sustained). Cache hits unthrottled; only expensive operations metered. 429 with Retry-After on empty bucket.

#### Non-obvious details

1. **Check all resolved IPs, not just the first.** DNS round-robin attack.
2. **Block 169.254.x.x entirely.** Link-local = AWS/GCE/Azure/DO metadata addresses.
3. **TLS doesn't help pre-handshake.** The hostname check happens before the connection. Hostname validation is the gate.
4. **Allowlist YouTube.** `youtube.com`, `youtu.be`, `googlevideo.com` — documented exception for yt-dlp happy path.
5. **Per-IP, not per-user.** POC has no auth; IP is the available identity. In production, scope to authenticated users.

#### Impact

- Operator/attacker via UI cannot make edge fetch internal endpoints.
- Brute-force rendering bounded to 3 bursts then 1 per 20s.
- Cache-friendly paths stay snappy.

---

### 7c. Edge → cloud HMAC (message auth, not transport auth)

#### Problem

Edge ships safety events to a cloud receiver over HTTPS. HTTPS proves *who you're talking to* (PKI). It doesn't prove *who sent the batch*. An attacker who guesses the cloud URL can forge batches and inject false events — no special privilege needed.

Classic: TLS is transport auth. We need message auth.

#### Fix

Every batch HMAC-SHA256-signed over `{timestamp}.{body}`. [backend/integrations/edge_publisher.py:526-550](../backend/integrations/edge_publisher.py#L526-L550) signs; [cloud/receiver.py:225-280](../cloud/receiver.py#L225-L280) verifies:

```python
def _verify_signature(secret, ts_header, sig_header, body):
    ts = int(ts_header)
    if abs(now() - ts) > 300:                             # ±5 min window
        raise HTTPException(401, "timestamp outside window")
    expected = "sha256=" + hmac.new(secret, f"{ts}.".encode()+body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_header):     # constant-time
        raise HTTPException(401, "bad signature")
```

[cloud/receiver.py:356](../cloud/receiver.py#L356) dedupes by `event_id` via `INSERT OR IGNORE` — captured-and-replayed batches within the window insert zero rows.

#### Non-obvious details

1. **Why not OAuth.** Needs an IdP every edge can reach. Breaks air-gapped deployments.
2. **Constant-time comparison.** `hmac.compare_digest` prevents timing-side-channel byte-at-a-time attacks.
3. **Timestamp binding in the signed message.** Without it, a captured batch replays forever. Included → different HMAC per second → bounded to 5-min window.
4. **±5 min window.** Lenient for NTP drift, tight for replay.
5. **Nonce breaks accidental collision.** Two identical payloads at the same second → identical HMAC → dedup false positive. Nonce (16 random hex) breaks that.
6. **TLS still required.** HMAC doesn't encrypt. TLS (confidentiality) + HMAC (authenticity) together.

#### Impact

- TLS proves identity; HMAC proves batch authorization. URL leak alone doesn't = batch forge.
- Air-gapped deployments secure — shared secret at deploy, no IdP reachability.
- Idempotent ingest — retries safe via `INSERT OR IGNORE`.
- Replay window bounds captured-batch reuse.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **TLS alone** | URL-guess = batch forge. |
| **API key in URL/body** | Leaks in logs and URL bars. HMAC keeps secret out of plaintext. |
| **OAuth client-credentials** | Needs IdP. Breaks air-gap. |
| **Managed broker (SQS/Kafka)** | Vendor-coupled; requires cloud account. |

---

**One-liner for section 7.** *"Privacy by construction: hash plates at ingest so they never spread to buffers; SSRF + rate limits at input surfaces so operators can't abuse URLs or hammer CPU; HMAC across trust boundaries so forged batches are rejected. Three defense layers, each blocking a specific failure mode."*

## 8. Resilience — single LLM egress, failover, cost ceiling

#### Problem

Claude called from 5 places (narration, enrichment, operator chat, hypothesis analysis, debug support). One outage cascades everywhere. No single place to govern failover, enforce cost cap, apply retry policy, or enforce privacy invariants. Fixing a circuit-breaker bug = hunt across 5 sites.

#### Fix

All LLM calls funnel through [backend/services/llm.py](../backend/services/llm.py) (1,055 LoC). Nothing else imports an SDK — enforced in [.claude/rules/python.md](../.claude/rules/python.md). The file owns six resilience patterns:

1. **Provider failover.** `_complete()` tries primary (Azure if configured, else Anthropic), on any error tries secondary. `BACKEND="none"` if neither configured → all callers see `None` (no crash).
2. **Rate budget.** Shared [_TokenBucket](../backend/services/llm.py#L216-L280) for narration + enrichment (both Haiku) — 3 req/min, under Anthropic low-tier 5/min ceiling. `try_acquire()` returns False → caller records a "skipped" obs record, no crash.
3. **Circuit breaker.** 3 consecutive failures → open 60s. Half-open trial call on cooldown elapse; success closes, failure restarts timer.
4. **Cost tracking.** [backend/services/llm_obs.py](../backend/services/llm_obs.py) records every call (provider, tokens, latency, error). `/api/llm/stats` returns USD + burn rate + p95.
5. **Plate hashing at ingest.** `enrich_event` hashes + strips before buffer. Single privacy gate.
6. **No-network stub mode.** `BACKEND="none"` returns safe empty dicts. Tests run offline without mocking.

#### Non-obvious details

1. **Two-token vs one-token enrichment.** Self-consistency ALPR = 2 samples. If breaker open → downgrade to single-sample (1 token) to stay cheap during outages.
2. **Shared bucket prevents starvation.** Narration + enrichment both draw from the same 3-token bucket → they negotiate dynamically. Separate buckets would let enrichment burst starve narration.
3. **Rate-limit skips are not failures.** Skips → `llm_obs` as `rate_budget_exhausted`, not errors. Keeps error-rate trigger honest (skips = intentional backpressure).
4. **Azure wins by default.** Enterprises have committed capacity — Anthropic tried second.

#### Impact

- Anthropic 529 → watchdog rules-only, enrichment falls back or skips, narration uses generic template. No cascade.
- Cost meter prevents runaway retry loops — operator sees effect immediately on `/api/llm/stats`.
- One file, one breaker, one budget — bug fix or policy change = one commit.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Each consumer calls SDK directly** | Breaker fragments across 5 files; cost invisible; privacy invariant duplicated N times. |
| **LangChain** | Heavy opinionated chains we don't need; doesn't enforce "rules work without LLM". |
| **Sidecar service** | Extra deploy + network hop; re-opens plate-handling surface for a new process. |

**One-liner.** *"All LLM calls funnel through one module that owns failover, budgeting, breaker, cost. Provider outages degrade gracefully: rules-only watchdog, skipped enrichment, generic narration — never a cascade."*

## 9. Observability — watchdog as an incident queue

#### Problem

Error logs get tuned out within a week. `ERROR: LLM connection refused at 2026-04-21T14:32:10` teaches operators nothing — they don't know what to do, what it means, or when it's safe to ignore. Real incident management needs: what went wrong + why it matters + how to fix it + what commands to run + who owns this category. Also: if the LLM goes down, monitoring cannot depend on it.

#### Fix

Findings in [backend/services/watchdog/](../backend/services/watchdog/) carry `severity`, `category`, `impact`, `likely_cause`, `owner`, `evidence`, `investigation_steps`, `debug_commands`, `runbook`, `priority_score`, `source` (`rule`|`ai`), `cause_confidence`. Grouped by `fingerprint` — repeated symptoms collapse into one ticket.

**Two layers:**
- [rules.py](../backend/services/watchdog/rules.py) — deterministic detectors (perception quality, drift precision, LLM error rate, stream fps, scene context). Always available; never imports LLM.
- [ai.py](../backend/services/watchdog/ai.py) — Claude hypothesis. Strictly additive. Returns `[]` when provider unreachable.

**Dedupe in [api.py](../backend/services/watchdog/api.py).** On fingerprint OR title collision between rule + AI finding, **rule always wins** — the queue is never weaker than deterministic health checks.

Each finding includes evidence chips (`{label, value, threshold, status}`), debug commands (paste-ready `curl`), per-category investigation steps, runbook link.

#### Non-obvious details

1. **Fingerprint is the dedup key.** Same fingerprint → same incident with incremented count. "LLM error rate" at ticks 1/2/3 → one card with three occurrences.
2. **Rule wins on collision enforced in api.py, not ai.py.** `ai.py` returns findings blind; orchestration applies the tiebreaker. Keeps AI module side-effect-free, testable in isolation.
3. **Evidence is self-contained.** `{"label":"error_rate","value":"12%","threshold":"5%","status":"breach"}`. UI renders as pills; operators see at a glance what measurement triggered the card.
4. **Priority score precomputed.** `severity_base + source_bonus + evidence_count*2 (cap 5)`. Rules +5 over AI (deterministic > inferred).
5. **Source + confidence tags drive UI styling.** Rules = `source="rule"`/`observed`; AI = `source="ai"`/`inferred`. Distinct badges.

#### Impact

- Operators get "paste this `curl` to reproduce" + runbook link, not a log wall.
- Monitoring survives provider outages — rules fire alone.
- Repeated symptoms collapse — cascade doesn't flood with 100 copies of one problem.
- Fingerprinting is deterministic, not fuzzy — no "did this match?" ambiguity.

#### Alternatives

| Option | Why we rejected it |
| --- | --- |
| **Datadog / Sentry** | Mature UI, but no perception-domain rules (circuit breaker, drift precision, ALPR confidence). Needs outbound internet. |
| **Rules only** | Catches all known patterns, misses novel anomalies. |
| **LLM-first + rules fallback** | Violates invariant — queue goes dark when provider does. |

**One-liner.** *"Findings carry severity, category, evidence, likely cause, owner, runbook, debug commands — an incident queue, not a log wall. Two layers: rules (always available), AI (additive, dedupe against rules). Repeated symptoms collapse into one ticket with a count."*

## 10. Documentation / in-file discoverability
- **Problem.** `backend/api/models.py` is the wire-contract source of truth, but nothing in each Pydantic class told you *which FE hook* consumed it. Routers mentioned path + response model but not the downstream React component. Perception gates had terse thresholds with no rationale — `if ttc < 0.6` didn't explain *why 0.6*.
- **Fix.** One-pass annotation of **85 BE files**. Module docstrings name upstream producers and downstream consumers (endpoint → FE hook → component). Per-function docstrings carry inputs / outputs / side effects / privacy notes. Inline comments on the plate-hash invariant at ingest, HMAC signing, circuit-breaker state transitions, frame-encode viewer gating, watchdog fingerprint-collision resolution (rule wins), and gate rationale (*why* this threshold, not just *what*). Zero code changes — `py_compile` clean across all modules; TS codegen re-run emits byte-identical `generated.ts`.
- **Impact.** Each module reads as its own mini design doc. Tracing a field from `SafetyEvent` in Pydantic to its render site in `EventDialog` now follows breadcrumbs in both directions. Threshold tuning during settings work has the *why* beside the number.
- **Alternatives.** *Sphinx auto-API* (docstrings are a prerequisite, not an alternative; can layer on later). *Inline TODOs + external wiki* (external docs drift; inline lives with the code and reviews). *No annotations* (`CLAUDE.md`'s default style — traded here for onboarding velocity).

## BE — Critical bugfixes
1. **Hot-path config race** → `SettingsStore` snapshot isolation (atomic pointer swap).
2. **Lost-update on concurrent settings edits** → `If-Match: expected_revision_hash` returns 409; per-token/IP 5s apply cooldown; single-use 30s SSE tickets.
3. **PII leak risk in event buffers** → plate hash at ingest + strip fields.
4. **Cascading LLM outages** → circuit breaker + provider failover + rules-only watchdog fallback.
5. **Forged cloud ingest** → HMAC-signed batches + idempotent receiver.
6. **SSRF via stream URL input** → `validate_public_url()` guard.
7. **Inference CPU burn on idle tiles** → `StreamSlot.has_viewers`: encode only when watched.
8. **Ad-hoc FPS/CPU estimates** → `psutil`-backed ops sampler feeding ImpactMonitor.
9. **Watchdog loop blocked on LLM** → AI hypothesis moved to async, rules-first pattern.
10. **Subscriber crash poisoning apply chain** → each subscriber wrapped in `try/except`; errors counted, never roll back the apply.

## BE — Best practices applied
- **Composition root.** `server.py` wires; doesn't contain logic.
- **One egress per concern.** All LLM calls via `services/llm.py`; all settings writes via `SettingsStore.apply_diff()`.
- **Immutable snapshots for hot-path config** — readers never block writers; no partial reads.
- **Rules before AI** — deterministic floor; AI is additive and deduplicated.
- **Message auth, not just transport auth** — HMAC on edge→cloud.
- **Defense in depth** — plate hash at ingest *and* defensive `pop()` at egress.
- **Static typing on the boundary** — wire contract is mypy-strict.
- **Pydantic models as source of truth** — codegen to TypeScript.
- **Atomic diffs with rollback** — `last_known_good` + pointer swap.
- **Document invariants** — `CLAUDE.md` + `.claude/rules/*.md` turn tribal knowledge into enforceable text.

## BE — Best judgments (what I chose *not* to do)
- **No auth in the POC.** Half-built auth is worse than none — operators assume it protects, and it doesn't. Documented in README + `CLAUDE.md`. HMAC + audit log still in place where they matter.
- **No full hexagonal architecture.** Triples file count for a single-process POC.
- **No feature-flag SaaS** (LaunchDarkly/Unleash) — doesn't do statistical impact gating, the actual differentiator.
- **No etcd/Consul** for config — overkill for one process; SQLite for durable history is enough.
- **No OpenAPI-to-TS yet** — needs `response_model=` discipline on 100% of handlers first.
- **No full-codebase mypy strict** — typing `numpy`/`cv2` is weeks of cleanup for marginal value.
- **Dashcam code fully stripped, not flag-gated** — two products fighting in one repo produce contradictory gate behavior. `dashcam-last-known-good` branch keeps the archaeology path.

---

## Summary table — FE vs BE per area

| Area | Frontend fix | Backend fix |
| --- | --- | --- |
| Project structure | Feature folders + import rule | `backend/{core,perception,services,api,…}` |
| Giant files | `SettingsPage`, `MultiSourceGrid` decomposed | `server.py` 1535→194; `watchdog/` package |
| Network | `apiFetch` + `HttpApiError` + `AbortSignal` | 15 routers + HMAC ingest + SSRF guard |
| State management | TanStack Query | `SettingsStore` snapshot isolation |
| Hooks / lifecycle | Single `<EventStreamProvider>`, `useSSE` | `StreamSlot` viewer tracking |
| UI / reusable | `shared/ui/` library | (n/a) |
| Type safety | Generated TS from Pydantic | Two-tier `mypy` on boundary |
| Performance | Lazy routes, background pause, polling transport | Encode-on-demand, lock-free reads |
| Error handling | Per-route `ErrorBoundary` | Circuit breaker + rules-only fallback |
| Privacy / security | (n/a) | Plate hash at ingest, HMAC, SSRF |
| Resilience | (n/a) | Single LLM egress + failover + cost cap |
| Observability | (n/a) | Watchdog fingerprinted incident queue |
| Code discoverability | 126 files annotated (JSDoc + FE→BE endpoint map) | 85 files annotated (endpoint→consumer docstrings, gate rationale) |

---

## How I'd present this in 5 minutes

1. **Show the framework first** — the 12-row "common areas" table. *"These are the buckets I audit any codebase by."*
2. **Pick two deep dives** that show judgment, not just work:
   - **BE:** `SettingsStore` snapshot isolation (concurrency under hot-path reads).
   - **FE:** polling-only transport (network constraints driving design: 6-conn cap + 2 fps perception).
3. **Volunteer one gap** — *"Auth is the biggest product-readiness gap, here's why I didn't half-build it."* Engineering judgment beats feature enthusiasm.
4. **One alternative per topic** — *"I considered X; rejected because Y."* Proves I chose, didn't just do.
