/**
 * AppProviders — single composition root for cross-cutting providers.
 *
 * Order matters:
 *   1. QueryClientProvider — every feature hook reads its cache.
 *   2. BrowserRouter       — react-router context for <Link>/<Route>.
 *   3. WatchdogProvider    — depends on QueryClient (it uses TanStack
 *                            Query internally).
 *   4. DialogProvider      — exposes themed alert/confirm to every
 *                            descendant (and to non-component callers
 *                            via the `dialog` singleton).
 */
import type { ReactNode } from "react";
import { BrowserRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";

import { queryClient } from "../shared/lib/queryClient";
import { DialogProvider } from "../shared/ui";
import { WatchdogProvider } from "../features/watchdog";

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <WatchdogProvider>
          <DialogProvider>{children}</DialogProvider>
        </WatchdogProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
