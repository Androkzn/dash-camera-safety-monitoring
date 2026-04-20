---
name: frontend
description: React 19 + Vite + TypeScript conventions for frontend/
type: rules
paths:
  - "frontend/**/*.{ts,tsx}"
  - "frontend/**/*.css"
---

# Frontend conventions

- Stack: React 19, Vite 6, TypeScript ~5.8, react-router 7, TanStack Query 5. No Redux, no Tailwind.
- Functional components + hooks only. No class components.
- Pages: `AdminPage` (live detections), `DashboardPage` (fleet overview), `MonitoringPage` (incident-queue watchdog), `SettingsPage` (operator tuning console).

## Folder layout — feature-based

```
frontend/src/
  app/                ← app shell only
    router.tsx        ← lazy routes + <RouteShell> (ErrorBoundary + Suspense)
    providers.tsx     ← QueryClientProvider, BrowserRouter, WatchdogProvider, DialogProvider
  features/           ← one self-contained folder per domain
    admin/
      AdminPage.tsx   ← thin orchestrator; page file lives with its feature
      components/     ← feature-local components
      hooks/          ← feature-local hooks
      api.ts          ← feature-local endpoint wrappers
      types.ts
      index.ts        ← public barrel
    dashboard/
    monitoring/
    settings/         ← also has utils/ for formatting, validation, steps
    tests/
    watchdog/
  shared/             ← cross-feature building blocks
    ui/               ← primitives: Button, Card, Dialog, EmptyState, ErrorBoundary,
                        Input, Pill, Section, Skeleton, Spinner, Tabs, …
    hooks/            ← usePolling, useSSE, useEventStream, useLiveStatus, useAdminToken
    lib/              ← queryClient, fetchClient, adminApi, format
    layout/           ← TopBar, PageLayout
    events/           ← EventCard, FeedbackButtons (shared across admin/dashboard)
    types/            ← truly global types only
```

**Import rule:** a feature may import from `shared/` or its own folder. A feature must **not** import from another feature. If two features need the same thing, promote it to `shared/`. This is the load-bearing rule — it keeps the folder names honest as the app grows.

## Data flow

- Backend talks to the frontend via SSE (`/api/live/stream`) and JSON endpoints (`/api/...`). No websockets.
- Data fetching goes through **TanStack Query** (`useQuery` / `useMutation`). New polling hooks should wrap `useQuery` with `refetchInterval`, not hand-rolled `setInterval`. `shared/hooks/usePolling.ts` is legacy and only kept for hooks not yet migrated.
- SSE still uses the `shared/hooks/useSSE.ts` primitive; don't wrap SSE in TanStack Query.
- Public thumbnails only — never call endpoints requiring `X-DSAR-Token` from the frontend without an explicit privileged path.
- Admin endpoints require `Authorization: Bearer <ROAD_ADMIN_TOKEN>`; UI must surface auth failures, not retry silently.

## Routing

- Every route is `React.lazy`-loaded in [app/router.tsx](frontend/src/app/router.tsx) so each page is its own chunk.
- Every route is wrapped in `<RouteShell>` which provides `ErrorBoundary` + `Suspense`. A thrown error in one page must not take out the others.

## Build

- `cd frontend && npm run build` → `tsc -b && vite build` into `frontend/dist/`.
- `start.py` rebuilds the frontend before launching the server. The server serves from `frontend/dist/` if present, else `static/`.
- For frontend-only iteration: `cd frontend && npm run dev` (Vite dev server on a separate port).

## Type checking

- `cd frontend && npx tsc -b --noEmit` to type-check without writing.
- Don't disable `strict` or `noUncheckedIndexedAccess` in [frontend/tsconfig.app.json](frontend/tsconfig.app.json) without asking.
