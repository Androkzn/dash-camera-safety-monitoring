# Frontend improvements — React 19 · Vite · TypeScript SPA

**Scope.** The React SPA in [frontend/](../../frontend/): three pages (Admin, Dashboard, Monitoring), 13 custom hooks, MJPEG video, two SSE streams, copilot chat, watchdog incident queue.

**Anchor files.** [frontend/src/hooks/useSSE.ts](../../frontend/src/hooks/useSSE.ts) · [hooks/useEventStream.ts](../../frontend/src/hooks/useEventStream.ts) · [hooks/useDetections.ts](../../frontend/src/hooks/useDetections.ts) · [hooks/useChat.ts](../../frontend/src/hooks/useChat.ts) · [lib/api.ts](../../frontend/src/lib/api.ts) · [types.ts](../../frontend/src/types.ts) · [App.tsx](../../frontend/src/App.tsx) · [main.tsx](../../frontend/src/main.tsx) · [vite.config.ts](../../frontend/vite.config.ts) · [tsconfig.app.json](../../frontend/tsconfig.app.json).

**Date.** 2026-04-18.

---

## TL;DR

1. **Add an `ErrorBoundary` per route.** Today any throw inside a render or SSE handler unmounts the root and blanks the dashboard. Five-minute change.
2. **Generate FE types from `/openapi.json`.** Drops [types.ts](../../frontend/src/types.ts)'s ~200 hand-rolled lines that silently drift from Pydantic.
3. **Move SSE state to `useSyncExternalStore` + wrap bursts in `startTransition`.** Stops the chat input from stuttering during high-event bursts.
4. **`Last-Event-ID` resumption + heartbeat-stall watchdog.** A laptop wake / proxy idle today produces an "alive but frozen" dashboard — the worst kind of demo failure.
5. **Sanitize copilot chat output through DOMPurify.** Pre-emptive XSS guard for LLM-generated text.
6. **Vitest + React Testing Library + MSW.** Zero FE tests today. Five strategic tests beat fifty snapshot tests.
7. **Cap concurrent MJPEG decoders + pause minimized/off-screen tiles** (R21). 6-tile Admin grid pinned the renderer at ~76 % CPU during the 2026-04-19 perf incident; `focusedId` + `tileMini` state in `MultiSourceGrid` is the priority signal (now persisted cross-page via `localStorage.road_admin_focused_id`, consumed by the `SettingsPage` Live Preview as its default source) — remaining work is to wire it to a single-shot snapshot endpoint and `IntersectionObserver` for the minimized strip itself.

The codebase is small and clean: [useSSE.ts](../../frontend/src/hooks/useSSE.ts) already does the right basics (ref-pinned callback, exponential backoff capped at 30s, bounded buffers). [tsconfig.app.json](../../frontend/tsconfig.app.json) already has `strict: true` and `noUncheckedIndexedAccess: true`. The recommendations below are ordered by ROI for a *demo / showcase* whose audience is technical reviewers.

---

## P0 — high priority (visible in a 10-minute demo or first review)

### R1 `[H]` Replace `useState`-buffer SSE with `useSyncExternalStore`

**Pattern.** Move the SSE event buffer out of React state into a tiny external store; subscribe components via `useSyncExternalStore(subscribe, getSnapshot)`.

**Why this project.** `useEventStream` and `useDetections` push every SSE message through `setState`, forcing every consumer of the hook (and every parent that closes over it) to re-render at SSE rate. With `MAX_EVENTS=100` and an MJPEG image rerendering alongside, you pay React reconciliation per detection. `useSyncExternalStore` is the React 18/19-blessed primitive for exactly this pattern — concurrent-safe, tearing-free, and lets selector-style consumers subscribe to slices.

**Adoption.** `frontend/src/lib/eventStore.ts`:
```ts
type Listener = () => void;
const listeners = new Set<Listener>();
let snapshot: { events: SafetyEvent[]; perception: PerceptionState | null } =
  { events: [], perception: null };

export const eventStore = {
  subscribe(l: Listener) { listeners.add(l); return () => listeners.delete(l); },
  getSnapshot: () => snapshot,
  push(ev: SafetyEvent) {
    snapshot = { ...snapshot, events: [ev, ...snapshot.events].slice(0, 100) };
    listeners.forEach(l => l());
  },
};
```
Then `const { events } = useSyncExternalStore(eventStore.subscribe, eventStore.getSnapshot)`. The SSE connector becomes a singleton outside React.

**Trade-off.** ~30 LOC of plumbing; loses the "everything is a hook" purity. Worth it the moment two components want to read the same stream.

**Citation.** https://react.dev/reference/react/useSyncExternalStore.

### R2 `[H]` Wrap SSE bursts in `startTransition`

**Pattern.** When an SSE message arrives, mark the buffer-update as a transition so React can interrupt it for higher-priority work (input typing in copilot chat, route changes).

**Why this project.** During a burst (crowded scene → 20 detections/s) the current code blocks the renderer with N synchronous setStates. Detection FPS is essentially "background" data — perfect transition candidate. Keeps the chat input from stuttering while events stream.

**Adoption.** In `useEventStream`:
```ts
const onMessage = useCallback((msg) => {
  startTransition(() => setEvents(prev => [msg, ...prev].slice(0, 100)));
}, []);
```
If you adopt R1, call `startTransition` in the React subscriber, not the producer.

**Trade-off.** "Connected" pip and event count may lag ~16–32ms behind reality. For a safety dashboard that's an *acceptable* trade for input responsiveness; comment the choice in code.

**Citation.** https://react.dev/reference/react/useTransition.

### R3 `[H]` `Last-Event-ID` + heartbeat-stall detection

**Pattern.** Two gaps in [useSSE.ts](../../frontend/src/hooks/useSSE.ts):

1. EventSource auto-reconnects but the **server** must honour the `Last-Event-ID` request header to replay missed events. Today the backend doesn't emit `id:` lines — see [integration.md R1.1](./integration.md#r11-h-implement-last-event-id-resumption).
2. `onerror` only fires on transport failure; a **silent stalled connection** (proxy idle-killed, laptop sleep) won't trigger reconnect. You need a heartbeat-timeout watchdog.

**Why this project.** A demo where the laptop sleeps and the dashboard "looks alive but stops updating" is a credibility killer. Both fixes are ~20 LOC and demonstrate you've shipped real-time systems before.

**Adoption.**
- BE side covered in integration doc.
- FE side, in [useSSE.ts](../../frontend/src/hooks/useSSE.ts):
  ```ts
  const lastBeatRef = useRef(Date.now());
  useEffect(() => {
    const id = setInterval(() => {
      if (Date.now() - lastBeatRef.current > 30_000) {
        esRef.current?.close();
        connect();
      }
    }, 5_000);
    return () => clearInterval(id);
  }, []);
  // in onmessage:
  lastBeatRef.current = Date.now();
  ```

**Trade-off.** If you ever need custom auth headers or POST bodies on the SSE request, native `EventSource` can't — switch to `fetch` + `ReadableStream` (Microsoft's `@microsoft/fetch-event-source` is the de-facto port). For this demo, EventSource is fine.

**Citations.** https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events · https://html.spec.whatwg.org/multipage/server-sent-events.html#last-event-id.

### R4 `[H]` Error boundaries with reporting

**Pattern.** A single top-level `<ErrorBoundary>` in [main.tsx](../../frontend/src/main.tsx), plus per-route boundaries in [App.tsx](../../frontend/src/App.tsx) so a render crash in `MonitoringPage` doesn't blank the live admin view.

**Why this project.** Today, any throw inside a render or SSE handler unmounts the root. For a demo that runs unattended on a screen, this is fatal. Reviewers also expect to see one — its absence reads as "early prototype".

**Adoption.** Use [`react-error-boundary`](https://github.com/bvaughn/react-error-boundary) (Brian Vaughn, React core team alumnus):
```tsx
<ErrorBoundary FallbackComponent={Crash} onError={reportToTelemetry}>
  <Routes>...</Routes>
</ErrorBoundary>
```
Wire `onError` to `console.error` for now; mark `// TODO: Sentry / OTel` for later.

**Trade-off.** None. Five minutes.

**Citation.** https://react.dev/reference/react/Component#catching-rendering-errors-with-an-error-boundary.

### R5 `[H]` Sanitize copilot chat output (DOMPurify)

**Pattern.** [useChat](../../frontend/src/hooks/useChat.ts) renders `answer` directly. If anywhere it's rendered with `dangerouslySetInnerHTML` (or markdown→HTML is added), you have an XSS sink in the demo. Pre-emptively pipe through DOMPurify.

**Why this project.** Reviewers grep for `dangerouslySetInnerHTML`. Adding `DOMPurify.sanitize()` before any markdown render shows you treat LLM output as untrusted. Even today's plain-text rendering should go through a `renderSafeMarkdown(text)` helper so future markdown support is safe-by-default. Pairs with [backend.md R6 (LLM02 Insecure output handling)](./backend.md#r6-h-owasp-llm-top-10-2025-mapping--missing-controls).

**Adoption.**
```bash
npm i dompurify @types/dompurify marked
```
`frontend/src/lib/safeMarkdown.ts`:
```ts
import DOMPurify from "dompurify";
import { marked } from "marked";
export const safeMarkdown = (s: string) => DOMPurify.sanitize(marked.parse(s) as string);
```

**Trade-off.** ~20 KB gz for marked + DOMPurify. Acceptable for the value.

**Citations.** https://github.com/cure53/DOMPurify · https://owasp.org/www-community/attacks/xss/.

### R6 `[H]` Generate types from FastAPI's `/openapi.json`

**Pattern.** Drop hand-maintained [types.ts](../../frontend/src/types.ts) (~200 lines, drifts) for `openapi-typescript` + `openapi-fetch`. See [integration.md R2.1](./integration.md#r21-h-generate-fe-types-from-fastapis-openapijson) for the full plan including the BE-side `response_model=` work.

**FE side adoption.** `npm i -D openapi-typescript`; `npm i openapi-fetch`. Add `npm run gen:api`. Refactor [lib/api.ts](../../frontend/src/lib/api.ts):
```ts
import createClient from "openapi-fetch";
import type { paths } from "../types/api.gen";
export const api = createClient<paths>({ baseUrl: "" });
```
Use: `const { data, error } = await api.GET("/api/live/events", { params: { query: { limit: 50 } } })` — typed per status code.

**Trade-off.** Couples FE codegen to a running backend; mitigate by committing the generated `.d.ts` and regenerating in CI.

**Citation.** https://openapi-ts.dev/.

---

## P1 — medium priority (clear quality wins)

### R7 `[M]` Runtime-validate SSE payloads (Valibot)

**Pattern.** SSE messages are stringly-typed JSON. [useSSE.ts](../../frontend/src/hooks/useSSE.ts) does `JSON.parse(ev.data) as T` — pure faith. Add a Valibot schema per stream and parse with `safeParse`.

**Why this project.** Backend pipeline emits a discriminated union (`SafetyEvent | PerceptionState` keyed by `_meta`). The current code leans on `"_meta" in msg` which is fine but not exhaustive. Valibot gives discriminated-union safety, defaulting on missing fields, and a single source of truth that doubles as TS types via `v.InferOutput`. ~3 KB gz; preferable to Zod (~12 KB) for this FE.

**Adoption.** `frontend/src/types/schemas.ts` defining `SafetyEventSchema = v.object({...})`; in `useSSE`, accept an optional `schema?: v.GenericSchema<T>` and gate `onMessage` on `v.safeParse(...).success`.

**Citations.** https://valibot.dev/ · https://zod.dev/.

### R8 `[M]` Virtualize the event list (TanStack Virtual)

**Pattern.** `MAX_EVENTS=100` is small enough today that a full DOM render is fine. But the watchdog incident queue and the historical events views will grow. Adopt `@tanstack/react-virtual` now and set the precedent.

**Why this project.** TanStack Virtual is headless (no DOM imposed), <5 KB, React-19 ready. Lighthouse LCP penalizes long DOM trees with images (each event has a thumbnail).

**Adoption.**
```ts
const rowVirtualizer = useVirtualizer({
  count: events.length,
  getScrollElement: () => parentRef.current,
  estimateSize: () => 84,
});
```

**Trade-off.** Slightly more complex CSS (absolutely-positioned rows). Worth it once any list exceeds 50 visible items.

**Citation.** https://tanstack.com/virtual/latest.

### R9 `[M]` Adopt the React Compiler

**Pattern.** Add `babel-plugin-react-compiler` to the Vite React plugin. Auto-memoizes components, callbacks, and derived values — eliminates most need for `useMemo` / `useCallback` / `React.memo`.

**Why this project.** This codebase is full of inline callbacks and `useCallback` wrappers. The compiler removes them as a class of bug ("did I add the right deps?"). For a 2026 demo it's table-stakes signal that you track the React roadmap.

**Adoption.**
```ts
// vite.config.ts
react({ babel: { plugins: [["babel-plugin-react-compiler", { target: "19" }]] } })
```
Add `eslint-plugin-react-compiler` so the linter flags code the compiler can't optimize.

**Trade-off.** ~5–10% slower dev build. Some patterns (mutating refs during render) get flagged.

**Citation.** https://react.dev/learn/react-compiler.

### R10 `[M]` Code-split routes with `React.lazy` + `Suspense`

**Pattern.** Three top-level pages, all eagerly imported in [App.tsx](../../frontend/src/App.tsx). Lazy-load the two non-default routes.

**Why this project.** Default route is `/` → AdminPage. A first-time visitor pays for `MonitoringPage` (watchdog tables) and `DashboardPage` (chat, charts) on initial load. Lazy split shaves ~30–50% off the initial bundle.

**Adoption.**
```tsx
const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const MonitoringPage = lazy(() => import("./pages/MonitoringPage"));
// wrap <Routes> in <Suspense fallback={<RouteSkeleton />}>
```
Add prefetch on nav-link hover so the first navigation doesn't show a Suspense fallback.

**Citation.** https://react.dev/reference/react/lazy.

### R11 `[M]` Bundle analysis + Vite `build.target` tuning

**Pattern.** Add `rollup-plugin-visualizer`; inspect `dist/stats.html`. Set `build.target: "es2022"` (matches your tsconfig) and `build.modulePreload: { polyfill: false }`.

**Why this project.** Vite's default `target: "baseline-widely-available"` is conservative. Aligning with the project's ES2022 tsconfig saves ~5–10 KB of polyfills. The visualizer plugin produces an HTML treemap useful for screenshots in the README.

**Adoption.**
```ts
// vite.config.ts
import { visualizer } from "rollup-plugin-visualizer";
build: {
  target: "es2022",
  sourcemap: true,
  rollupOptions: { plugins: [visualizer({ filename: "dist/stats.html" })] },
}
```

**Citations.** https://vite.dev/guide/build.html · https://github.com/btd/rollup-plugin-visualizer.

### R12 `[M]` Live region for SSE alerts; pause on tab hidden

**Pattern.** Two a11y/UX wins:

1. `<div role="status" aria-live="polite" aria-atomic="true">` reading the latest *high-risk* event so screen-reader users get audible alerts.
2. In [useSSE](../../frontend/src/hooks/useSSE.ts), listen to `document.visibilitychange` and close the EventSource when the tab is hidden for >60s; reopen on visible.

**Why this project.** Live region is a 5-line a11y win that interviewers notice. Tab-hidden pause cuts CPU/network on background tabs by ~100% for the SSE stream and mirrors what real production dashboards do (Grafana, Datadog).

**Adoption.**
```tsx
// AdminPage.tsx
<div role="status" aria-live="polite" className="sr-only">
  {latestHighRisk?.summary ?? ""}
</div>
```
```ts
// useSSE.ts
useEffect(() => {
  const onVis = () => document.hidden ? esRef.current?.close() : connect();
  document.addEventListener("visibilitychange", onVis);
  return () => document.removeEventListener("visibilitychange", onVis);
}, []);
```

**Trade-off.** Need a "stale data" indicator while reconnecting after wake.

**Citations.** https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Roles/status_role · https://web.dev/articles/page-lifecycle-api.

### R13 `[M]` Vitest + React Testing Library + MSW

**Pattern.** No tests exist. Stand up the standard React 19 stack:

- `vitest` (zero-config with Vite), `@testing-library/react`, `@testing-library/user-event`.
- `msw` v2 for REST mocks; for SSE either `event-source-polyfill` + MSW or `vitest-sse-mock`.
- Strategic test set:
  - `useSSE` reconnect logic (assert exponential backoff, jitter optional).
  - `useEventStream` 100-event cap and dedup-by-event_id.
  - One smoke test per page (renders, no crash).
  - One E2E happy path with Playwright (load `/`, see SSE event arrive, see thumbnail).

**Why this project.** A single `useSSE.test.ts` proving exponential backoff is more impressive than ten React component snapshot tests. For a demo, ship 5–10 strategic tests, not coverage targets.

**Adoption.**
```bash
npm i -D vitest @vitest/ui @testing-library/react @testing-library/jest-dom \
  @testing-library/user-event msw jsdom @playwright/test
```
Add `vitest.config.ts` with `test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"] }`.

**Trade-off.** ~1 day of setup. High signal for code reviewers.

**Citations.** https://vitest.dev/guide/ · https://testing-library.com/docs/react-testing-library/intro/ · https://mswjs.io/ · https://playwright.dev/.

---

## P2 — lower priority (polish + signaling)

### R14 `[L]` Tighten `tsconfig.app.json`

[tsconfig.app.json](../../frontend/tsconfig.app.json) already has `strict: true` and `noUncheckedIndexedAccess: true` — strong. Add:
```jsonc
"exactOptionalPropertyTypes": true,
"noImplicitOverride": true,
"noPropertyAccessFromIndexSignature": true,
"verbatimModuleSyntax": true
```
Install [`@total-typescript/ts-reset`](https://github.com/total-typescript/ts-reset) to fix `JSON.parse` returning `any`, `Array.filter(Boolean)` typing, `fetch().json()` returning `unknown`.

**Why this project.** `exactOptionalPropertyTypes` catches the `enrichment_skipped?: string` vs `enrichment_skipped: string | undefined` mismatch that bites at backend boundaries. `ts-reset` makes `JSON.parse` return `unknown` — pairs perfectly with the Valibot recommendation (R7).

**Trade-off.** ~50 type errors will surface initially. All real bugs.

**Citations.** https://www.typescriptlang.org/tsconfig/ · https://github.com/total-typescript/ts-reset.

### R15 `[L]` `web-vitals` + structured client logs

**Pattern.** Drop in Google's `web-vitals` package; POST CLS/INP/LCP to `/api/metrics/web-vitals`.

**Why this project.** Live dashboards live and die by INP (Interaction-to-Next-Paint, Core Web Vital since March 2024). Sending metrics to your own watchdog endpoint closes the observability loop and is a strong demo talking point.

**Adoption.**
```ts
import { onINP, onLCP, onCLS } from "web-vitals";
[onINP, onLCP, onCLS].forEach(fn =>
  fn(m => navigator.sendBeacon("/api/metrics/web-vitals", JSON.stringify(m))));
```

**Citations.** https://web.dev/articles/vitals · https://github.com/GoogleChrome/web-vitals.

### R16 `[L]` `useOptimistic` for chat + feedback

**Pattern.** [useChat](../../frontend/src/hooks/useChat.ts).send waits for the round trip before showing the user message. `api.sendFeedback` (TP/FP buttons) likely also blocks. Wrap both in `useOptimistic`.

**Why this project.** React 19 ships `useOptimistic` specifically for this. The chat already does manual optimistic insertion (`setMessages([..., user])`); converting to `useOptimistic` is pure idiom — and signals you read the React 19 announcement carefully.

**Adoption.**
```ts
const [optimisticMsgs, addOptimistic] = useOptimistic(
  messages,
  (state, msg: ChatMessage) => [...state, msg],
);
```

**Citation.** https://react.dev/reference/react/useOptimistic.

### R17 `[L]` Focus management on route change + `prefers-reduced-motion`

**Pattern.** On `useLocation()` change, focus a `<h1 tabIndex={-1}>` so screen readers announce the new page. In `global.css`, gate all transitions on `@media (prefers-reduced-motion: no-preference)`.

**Why this project.** SPA route changes are silent for AT users by default. ~10 lines, clear a11y signal.

**Citations.** https://www.gatsbyjs.com/blog/2019-07-11-user-testing-accessible-client-routing/ · https://web.dev/articles/prefers-reduced-motion.

### R18 `[L]` CSP, Trusted Types, SRI (FastAPI middleware)

**Pattern.** Backend change but listed here because it's FE-security. Middleware on FastAPI to set:
```
Content-Security-Policy: default-src 'self'; img-src 'self' data: blob:; connect-src 'self'; style-src 'self' 'unsafe-inline'; require-trusted-types-for 'script'
Strict-Transport-Security: max-age=63072000
Permissions-Policy: camera=(), microphone=(), geolocation=()
```
Drop `'unsafe-inline'` for styles by extracting CSS-modules' generated runtime to a hashed script.

**Why this project.** Demo will likely be exposed publicly at some point; CSP is a 5-minute middleware add. Trusted Types pairs with the DOMPurify recommendation (R5) — the browser refuses to assign untrusted strings to `innerHTML`.

**Citations.** https://developer.mozilla.org/en-US/docs/Web/HTTP/CSP · https://web.dev/articles/trusted-types.

### R19 `[L]` Husky + lint-staged + Biome

**Pattern.** Replace ESLint+Prettier with Biome (single binary, ~10× faster, batteries included). Add `husky` + `lint-staged` to run `biome check --apply` on staged files.

**Why this project.** No linter is currently configured. Biome avoids the ESLint flat-config migration headache. ~3 KB of config.

**Adoption.**
```bash
npm i -D --save-exact @biomejs/biome husky lint-staged
npx biome init
npx husky init
echo 'npx lint-staged' > .husky/pre-commit
```

**Trade-off.** Biome doesn't yet support `eslint-plugin-react-compiler` — keep ESLint just for that one rule, or wait for Biome's compiler support.

**Citation.** https://biomejs.dev/.

### R20 `[L]` MJPEG → consider `<canvas>` + WebCodecs later

**Pattern.** MJPEG `<img src="/admin/video_feed">` is fine for a demo (zero JS, browser handles it). For production-style polish, ingest the same multipart stream into a `<canvas>` with `ImageDecoder` (WebCodecs) — gives frame-rate control, overlay drawing without DOM thrash, snapshot capture.

**Why this project.** Reviewers may ask "why MJPEG and not WebRTC/HLS?" Have an answer: MJPEG = no transcoding, no media-server, fine for ≤5 fps preview; WebCodecs is the modern path if you need detection-overlay-on-frame. See also [integration.md R1.5](./integration.md#r15-l-mjpeg--fragmented-mp4--webcodecs-at-fleet-scale).

**Trade-off.** WebCodecs is Chromium-first (Safari 17+). Don't migrate until you need overlays.

**Citation.** https://developer.mozilla.org/en-US/docs/Web/API/WebCodecs_API.

### R21 `[M]` Cap simultaneous MJPEG decoders; pause minimized + off-screen tiles

**Context — 2026-04-19 perf incident.** With 6 stream tiles mounted on the Admin grid, six `<img>` elements held a `multipart/x-mixed-replace` connection open simultaneously. Each one decodes a JPEG every ~500 ms in the renderer process; we measured Cursor Helper (Renderer) at ~76 % CPU during the saturation event. The tap-to-focus layout (`MultiSourceGrid` + `SelectedStreamHeader`) gives us the priority signal, and since 2026-04-19 the focused id is durable (`localStorage.road_admin_focused_id`, with a `admin-focused-id-changed` custom event for same-tab listeners and the native `storage` event for cross-tab sync) — the `SettingsPage` Live Preview already consumes it as the default source via `<StreamImage>`. What's still missing is the in-grid throttling of the minimized tiles themselves.

**Pattern.** Treat MJPEG `<img>` mounts as a scarce resource: only the focused tile (or the visible window of a virtualized strip) decodes at full rate; the rest fall back to a paused snapshot.

**Adoption.**
- In `MultiSourceGrid`, when `focusedId !== null` and a tile is in `tileMini`, swap `<img src="/admin/video_feed/{id}">` for `<img src="/admin/video_feed/{id}/snapshot?t=…">` (single-shot JPEG endpoint, refresh on a 2-5 s timer). Backend snapshot route is a one-line read of the slot's last `_annotated_jpeg` buffer.
- Use `IntersectionObserver` on the mini strip so tiles scrolled out of view don't decode at all.
- Cap any concurrent live MJPEG mounts at `navigator.hardwareConcurrency / 2` — anything beyond gets snapshots until something is paused.
- `document.visibilityState === "hidden"` should pause every live decoder (re-uses the R12 visibility hook).

**Why this project.** Pairs directly with [backend.md R-PERF](./backend.md#r-perf-h-multi-source-load-governance-per-slot-detection-toggle--runtime-cpu-guard) — server-side shared encoder + client-side decoder budget together turn N-stream cost into ~constant-cost. The focus state lifted into `AdminPage` (alongside the `useLiveSources`-once pattern) is the natural priority input, and is now exported to any page via `localStorage` + `admin-focused-id-changed` event — the `SettingsPage` Live Preview is the first cross-page consumer.

**Trade-off.** Snapshots lose the live "moving" affordance for minimized tiles; mitigate with a low (1 fps) refresh and a "paused" badge so the operator isn't surprised.

**Citations.** MJPEG `multipart/x-mixed-replace` — https://datatracker.ietf.org/doc/html/rfc2046#section-5.1 · IntersectionObserver — https://developer.mozilla.org/en-US/docs/Web/API/Intersection_Observer_API · Page Visibility API — https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API.

---

## What I would NOT recommend

- **Server Components / Next.js.** SSR has zero value for an authenticated single-user admin dashboard with real-time data. The architectural fit is wrong; mention this explicitly so reviewers know you considered it.
- **Redux / Zustand / Jotai.** `useSyncExternalStore` (R1) covers the need. Adding Zustand is fine but premature; tkdodo's own advice is "don't reach for state managers until you feel the pain" — https://tkdodo.eu/blog/working-with-zustand.
- **Tailwind migration.** CSS Modules are working; switching mid-project is pure churn.
- **React Query / SWR.** Most data here is SSE-pushed, not pull-fetched. The few REST calls (`/api/live/status`, `/api/admin/health`, `/chat`) don't need a cache layer; `useSyncExternalStore` (R1) covers the SSE story better than React Query would.
- **Activity API (React 19 experimental).** Useful for hiding/preserving state on tab switches, but it's experimental and the tab-visibility pattern (R12) covers the same use case in stable APIs.
- **Switch to a UI kit (MUI / Chakra / shadcn).** The bespoke CSS Modules look intentional and on-brand for a safety dashboard. Mid-project UI-kit migration is a common time sink with little reviewer-perceptible upside.

---

## 90-day phased rollout

### Week 1 — risk reduction, small diffs
- **R4** ErrorBoundary per route · 0.5 d
- **R5** DOMPurify on copilot output · 0.5 d
- **R3** heartbeat-stall watchdog (FE side) · 0.5 d
- **R12** aria-live region · 0.5 d
- **R14** tsconfig tighten · 0.5 d (then ~1 d to fix surfaced errors)

### Week 2 — architectural foundation
- **R6** openapi-typescript codegen + `lib/api.ts` migration · 2 d (paired with [backend.md R13](./backend.md#r13-m-pydantic-v2-models-for-events-payloads-sse-frames))
- **R1** `useSyncExternalStore` for events + detections · 1 d
- **R2** `startTransition` wrapping · 0.5 d

### Week 3 — performance
- **R9** React Compiler + eslint plugin · 0.5 d
- **R10** route-level code splitting · 0.5 d
- **R11** Vite build.target + visualizer · 0.5 d
- **R8** TanStack Virtual on event list · 0.5 d

### Week 4 — tests + polish
- **R13** Vitest + RTL + MSW skeleton + 5 strategic tests · 2 d
- **R7** Valibot validation in `useSSE` · 0.5 d
- **R12** visibility-pause SSE · 0.5 d

### Defer past 30 days
- R15 (web-vitals — only after R3 OTel ships in BE), R16 (useOptimistic — pure polish), R17 (a11y polish), R18 (CSP — coordinate with infra), R19 (Biome — wait for compiler-plugin support), R20 (WebCodecs — only if overlays needed).

---

## File → recommendation map

| File | Recommendations |
|------|-----------------|
| [hooks/useSSE.ts](../../frontend/src/hooks/useSSE.ts) | R3 · R7 · R12 |
| [hooks/useEventStream.ts](../../frontend/src/hooks/useEventStream.ts) | R1 · R2 |
| [hooks/useDetections.ts](../../frontend/src/hooks/useDetections.ts) | R1 · R2 |
| [hooks/useChat.ts](../../frontend/src/hooks/useChat.ts) | R5 · R16 |
| [lib/api.ts](../../frontend/src/lib/api.ts) | R6 |
| [types.ts](../../frontend/src/types.ts) | R6 (replaced by codegen) |
| [App.tsx](../../frontend/src/App.tsx) | R4 · R10 |
| [main.tsx](../../frontend/src/main.tsx) | R4 |
| [vite.config.ts](../../frontend/vite.config.ts) | R9 · R11 |
| [tsconfig.app.json](../../frontend/tsconfig.app.json) | R14 |
| [package.json](../../frontend/package.json) | R6 · R7 · R8 · R13 · R19 (deps) |
| (new) `src/lib/eventStore.ts` | R1 |
| (new) `src/lib/safeMarkdown.ts` | R5 |
| (new) `src/types/api.gen.ts` | R6 (generated) |
| (new) `src/types/schemas.ts` | R7 |
| (new) `src/test/setup.ts` + `vitest.config.ts` | R13 |

---

**Closing.** The FE is small, clean, and intentionally minimal — most recommendations here are about adopting React-19-and-2026 patterns (`useSyncExternalStore`, `startTransition`, the React Compiler), closing the type-safety loop with the BE (R6, R7), and adding the missing safety nets (ErrorBoundary R4, tests R13, sanitization R5). None of these change the architecture; all of them raise the floor.
