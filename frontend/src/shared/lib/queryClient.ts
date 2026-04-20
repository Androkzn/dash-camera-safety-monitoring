/**
 * queryClient.ts — shared TanStack Query client.
 *
 * Defaults are tuned for a real-time ops console:
 *   - `refetchOnWindowFocus: true`     — operator switches tabs and gets
 *                                        fresh data on return.
 *   - `staleTime: 5_000`               — within 5s, treat data as fresh
 *                                        and skip background refetches.
 *   - `gcTime: 5 * 60_000`             — keep cache for 5 min after last
 *                                        observer unmounts.
 *   - `retry: 1`                       — single retry on transient errors.
 *
 * Per-query options (refetchInterval, etc) live in each feature's hook.
 */

import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: true,
      staleTime: 5_000,
      gcTime: 5 * 60_000,
      retry: 1,
    },
    mutations: {
      retry: 0,
    },
  },
});
