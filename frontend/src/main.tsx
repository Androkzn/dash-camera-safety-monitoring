/**
 * main.tsx — browser entry point.
 *
 * 1. Creates a React root bound to <div id="root"> in `index.html`.
 * 2. Wraps the App in cross-cutting providers (TanStack Query, Router,
 *    Watchdog, Dialog) — see `app/providers.tsx` for ordering rationale.
 * 3. <StrictMode> stays on so React's dev-time double-invocation
 *    surfaces missing effect cleanups.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { AppProviders } from "./app/providers";
import "./styles/global.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppProviders>
      <App />
    </AppProviders>
  </StrictMode>,
);
