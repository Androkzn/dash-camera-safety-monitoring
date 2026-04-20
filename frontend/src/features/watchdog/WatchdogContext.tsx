/**
 * WatchdogContext — single shared TanStack Query that fetches both
 * status + recent findings on one timer, exposed to the whole app via
 * Context. Consumers (`useWatchdogCtx`) get deduplicated reads; the
 * provider owns mutations + admin-token error handling.
 */
import { createContext, useCallback, useContext, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type AdminApiError,
  MissingAdminTokenError,
} from "../../shared/lib/adminApi";
import { dialog } from "../../shared/ui";
import type { WatchdogFinding, WatchdogStatus } from "../../shared/types/common";

import { watchdogApi, watchdogQueryKeys } from "./api";

interface WatchdogData {
  status: WatchdogStatus;
  findings: WatchdogFinding[];
}

interface WatchdogCtx {
  status: WatchdogStatus | null;
  findings: WatchdogFinding[] | null;
  refresh: () => void;
  deleteFindings: (keys: string[]) => Promise<void>;
  clearAll: () => Promise<void>;
}

const Ctx = createContext<WatchdogCtx>({
  status: null,
  findings: null,
  refresh: () => {},
  deleteFindings: async () => {},
  clearAll: async () => {},
});

async function fetchBoth(): Promise<WatchdogData> {
  const [status, findings] = await Promise.all([
    watchdogApi.getStatus(),
    watchdogApi.getRecent(100),
  ]);
  return { status, findings };
}

function handleWatchdogAdminError(exc: unknown, action: string): void {
  if (exc instanceof MissingAdminTokenError) {
    void dialog.alert({
      title: `${action} requires admin token`,
      message:
        "Open the Settings page and paste your ROAD_ADMIN_TOKEN, then try again.",
      variant: "warning",
    });
    return;
  }
  const status = (exc as AdminApiError | undefined)?.status;
  if (status === 401 || status === 403) {
    void dialog.alert({
      title: `${action} rejected (HTTP ${status})`,
      message:
        "Your ROAD_ADMIN_TOKEN is missing or invalid. Open the Settings page " +
        "and paste a valid token, then try again.",
      variant: "warning",
    });
    return;
  }
  // eslint-disable-next-line no-console
  console.error(exc);
  void dialog.alert({
    title: `${action} failed`,
    message: (exc as Error)?.message ?? "unknown error",
    variant: "danger",
  });
}

export function WatchdogProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const { data, refetch } = useQuery({
    queryKey: watchdogQueryKeys.combined,
    queryFn: fetchBoth,
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  const refresh = useCallback(() => {
    void refetch();
  }, [refetch]);

  const deleteMutation = useMutation({
    mutationFn: (keys: string[]) => watchdogApi.deleteFindings(keys),
    onSuccess: () => qc.invalidateQueries({ queryKey: watchdogQueryKeys.combined }),
  });

  const clearMutation = useMutation({
    mutationFn: () => watchdogApi.clearAll(),
    onSuccess: () => qc.invalidateQueries({ queryKey: watchdogQueryKeys.combined }),
  });

  const deleteFindings = useCallback(
    async (keys: string[]) => {
      try {
        await deleteMutation.mutateAsync(keys);
      } catch (exc) {
        handleWatchdogAdminError(exc, "Delete findings");
      }
    },
    [deleteMutation],
  );

  const clearAll = useCallback(async () => {
    try {
      await clearMutation.mutateAsync();
    } catch (exc) {
      handleWatchdogAdminError(exc, "Clear all findings");
    }
  }, [clearMutation]);

  return (
    <Ctx.Provider
      value={{
        status: data?.status ?? null,
        findings: data?.findings ?? null,
        refresh,
        deleteFindings,
        clearAll,
      }}
    >
      {children}
    </Ctx.Provider>
  );
}

export function useWatchdogCtx() {
  return useContext(Ctx);
}
