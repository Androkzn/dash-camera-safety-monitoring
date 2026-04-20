/**
 * useLiveSources — list + lifecycle for perception sources.
 *
 * Polls `/api/live/sources` via TanStack Query and exposes optimistic
 * `start`/`pause`/`setDetection`/`add`/`remove` mutators that write
 * through the cache.
 */
import { useCallback, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { dialog } from "../../../shared/ui";
import type {
  LiveSourceStatus,
  LiveSourcesResponse,
} from "../../../shared/types/common";

import { adminApi, adminQueryKeys } from "../api";

/** Action a slot is currently performing — drives in-flight button labels. */
export type BusyAction = "starting" | "pausing" | "removing";

export interface UseLiveSourcesResult {
  sources: LiveSourceStatus[];
  primaryId: string | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  start: (id: string) => Promise<void>;
  pause: (id: string) => Promise<void>;
  setDetection: (id: string, enabled: boolean) => Promise<void>;
  add: (input: {
    url: string;
    name?: string;
  }) => Promise<{ ok: boolean; error?: string }>;
  remove: (id: string) => Promise<void>;
  restartAll: () => Promise<void>;
  /** Monotonically-increasing counter bumped every time restartAll() is
   *  invoked. The map hook watches this to reset its local playhead back
   *  to the start of the GPS track (since the server's uptime reset alone
   *  is intentionally ignored by the map — see useDemoTrack). */
  restartAllToken: number;
  restartingAll: boolean;
  busyById: Record<string, BusyAction | null>;
}

export function useLiveSources(refetchIntervalMs = 5000): UseLiveSourcesResult {
  const qc = useQueryClient();
  const { data, error, refetch, isLoading } = useQuery<LiveSourcesResponse>({
    queryKey: adminQueryKeys.liveSources,
    queryFn: adminApi.getLiveSources,
    refetchInterval: refetchIntervalMs,
    staleTime: 2_000,
  });

  const [busyById, setBusyById] = useState<Record<string, BusyAction | null>>({});

  const refresh = useCallback(async () => {
    await refetch();
  }, [refetch]);

  const mark = useCallback((id: string, v: BusyAction | null) => {
    setBusyById((prev) => ({ ...prev, [id]: v }));
  }, []);

  // Safety belt: any slot stuck in busy for > 8s gets force-cleared.
  useEffect(() => {
    const stuck = Object.entries(busyById).filter(([, v]) => !!v);
    if (stuck.length === 0) return;
    const t = window.setTimeout(() => {
      setBusyById((prev) => {
        const next: Record<string, BusyAction | null> = { ...prev };
        let changed = false;
        for (const [id] of stuck) {
          if (next[id]) {
            next[id] = null;
            changed = true;
          }
        }
        return changed ? next : prev;
      });
    }, 8000);
    return () => window.clearTimeout(t);
  }, [busyById]);

  const setRunningOptimistic = useCallback(
    (id: string, running: boolean) => {
      qc.setQueryData<LiveSourcesResponse>(adminQueryKeys.liveSources, (prev) =>
        prev
          ? {
              ...prev,
              sources: prev.sources.map((s) =>
                s.id === id ? { ...s, running } : s,
              ),
            }
          : prev,
      );
    },
    [qc],
  );

  const start = useCallback(
    async (id: string) => {
      mark(id, "starting");
      setRunningOptimistic(id, true);
      try {
        await adminApi.startLiveSource(id);
      } catch (exc) {
        setRunningOptimistic(id, false);
        void dialog.alert({
          title: "Start stream failed",
          message: (exc as Error)?.message ?? "unknown error",
          variant: "danger",
        });
      } finally {
        mark(id, null);
        await refresh();
      }
    },
    [mark, refresh, setRunningOptimistic],
  );

  const pause = useCallback(
    async (id: string) => {
      mark(id, "pausing");
      setRunningOptimistic(id, false);
      try {
        await adminApi.pauseLiveSource(id);
      } catch (exc) {
        setRunningOptimistic(id, true);
        void dialog.alert({
          title: "Pause stream failed",
          message: (exc as Error)?.message ?? "unknown error",
          variant: "danger",
        });
      } finally {
        mark(id, null);
        await refresh();
      }
    },
    [mark, refresh, setRunningOptimistic],
  );

  const setDetection = useCallback(
    async (id: string, enabled: boolean) => {
      qc.setQueryData<LiveSourcesResponse>(
        adminQueryKeys.liveSources,
        (prev) =>
          prev
            ? {
                ...prev,
                sources: prev.sources.map((s) =>
                  s.id === id ? { ...s, detection_enabled: enabled } : s,
                ),
              }
            : prev,
      );
      try {
        await adminApi.setLiveSourceDetection(id, enabled);
      } finally {
        await refresh();
      }
    },
    [qc, refresh],
  );

  const add = useCallback(
    async (input: { url: string; name?: string }) => {
      try {
        const res = await adminApi.addLiveSource({ ...input, autostart: true });
        await refresh();
        return { ok: !!res.ok, error: res.error };
      } catch (exc) {
        await refresh();
        return { ok: false, error: (exc as Error).message };
      }
    },
    [refresh],
  );

  const remove = useCallback(
    async (id: string) => {
      mark(id, "removing");
      qc.setQueryData<LiveSourcesResponse>(
        adminQueryKeys.liveSources,
        (prev) =>
          prev
            ? { ...prev, sources: prev.sources.filter((s) => s.id !== id) }
            : prev,
      );
      try {
        await adminApi.removeLiveSource(id);
      } finally {
        mark(id, null);
        await refresh();
      }
    },
    [qc, mark, refresh],
  );

  const [restartingAll, setRestartingAll] = useState(false);
  const [restartAllToken, setRestartAllToken] = useState(0);

  const restartAll = useCallback(async () => {
    setRestartingAll(true);
    try {
      const res = await adminApi.restartAllLiveSources();
      // Bump the token only on success so the map doesn't snap back on a
      // failed restart (the backend left the slots as they were).
      if (res.ok) {
        setRestartAllToken((n) => n + 1);
      }
      const failed = (res.results ?? []).filter((r) => !r.ok);
      if (failed.length) {
        void dialog.alert({
          title: "Some streams failed to restart",
          message: failed
            .map((f) => `${f.name || f.id}: ${f.error ?? "unknown error"}`)
            .join("\n"),
          variant: "danger",
        });
      }
    } catch (exc) {
      void dialog.alert({
        title: "Restart all failed",
        message: (exc as Error)?.message ?? "unknown error",
        variant: "danger",
      });
    } finally {
      setRestartingAll(false);
      await refresh();
    }
  }, [refresh]);

  return {
    sources: data?.sources ?? [],
    primaryId: data?.primary_id ?? null,
    loading: isLoading,
    error: error ? (error as Error).message : null,
    refresh,
    start,
    pause,
    setDetection,
    add,
    remove,
    restartAll,
    restartAllToken,
    restartingAll,
    busyById,
  };
}
