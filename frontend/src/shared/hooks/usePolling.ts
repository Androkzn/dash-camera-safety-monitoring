/**
 * usePolling — generic "fetch every N ms" hook.
 *
 * Most data-fetching now goes through TanStack Query (see
 * `shared/lib/queryClient.ts`); this hook is kept for cases where we
 * need finer control (e.g. SSE wrappers built on top of it) or for
 * legacy call sites that haven't been migrated yet.
 */
import { useEffect, useRef, useCallback, useState } from "react";

interface UsePollingOptions<T> {
  fetcher: () => Promise<T>;
  intervalMs: number;
  enabled?: boolean;
  immediate?: boolean;
}

export function usePolling<T>({
  fetcher,
  intervalMs,
  enabled = true,
  immediate = true,
}: UsePollingOptions<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const poll = useCallback(async () => {
    try {
      const result = await fetcherRef.current();
      setData(result);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    if (immediate) poll();
    const id = setInterval(poll, intervalMs);
    return () => clearInterval(id);
  }, [enabled, intervalMs, immediate, poll]);

  return { data, error, refetch: poll };
}
