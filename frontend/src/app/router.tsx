/**
 * Router — declarative route table.
 *
 * Every page is `React.lazy`-loaded so the initial bundle only contains
 * the app shell. Each route is wrapped in:
 *   - <ErrorBoundary> — a thrown error in /settings doesn't take out
 *                       /admin or /dashboard; the operator sees a
 *                       recoverable per-route fallback.
 *   - <Suspense>      — shows the route fallback while the page chunk
 *                       loads or any async data is resolving.
 */
import { Suspense, lazy, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { RouteFallback } from "../shared/layout/PageLayout";
import { ErrorBoundary } from "../shared/ui";

const AdminPage = lazy(() =>
  import("../features/admin").then((m) => ({ default: m.AdminPage })),
);
const DashboardPage = lazy(() =>
  import("../features/dashboard").then((m) => ({ default: m.DashboardPage })),
);
const MonitoringPage = lazy(() =>
  import("../features/monitoring").then((m) => ({ default: m.MonitoringPage })),
);
const ValidationPage = lazy(() =>
  import("../features/validation").then((m) => ({ default: m.ValidationPage })),
);
const SettingsPage = lazy(() =>
  import("../features/settings").then((m) => ({ default: m.SettingsPage })),
);

function RouteShell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <ErrorBoundary>
      <Suspense fallback={<RouteFallback label={`Loading ${label}…`} />}>
        {children}
      </Suspense>
    </ErrorBoundary>
  );
}

export function AppRouter() {
  return (
    <Routes>
      <Route
        path="/"
        element={
          <RouteShell label="Admin">
            <AdminPage />
          </RouteShell>
        }
      />
      {/* Legacy URL — redirect to "/" so old bookmarks still work. */}
      <Route path="/admin" element={<Navigate to="/" replace />} />
      <Route
        path="/dashboard"
        element={
          <RouteShell label="Dashboard">
            <DashboardPage />
          </RouteShell>
        }
      />
      <Route
        path="/monitoring"
        element={
          <RouteShell label="Monitoring">
            <MonitoringPage />
          </RouteShell>
        }
      />
      <Route
        path="/validation"
        element={
          <RouteShell label="Validation">
            <ValidationPage />
          </RouteShell>
        }
      />
      <Route
        path="/settings"
        element={
          <RouteShell label="Settings">
            <SettingsPage />
          </RouteShell>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
