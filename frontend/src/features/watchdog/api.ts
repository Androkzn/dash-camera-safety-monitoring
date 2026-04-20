/**
 * Watchdog API surface.
 *
 * Read endpoints are public-tier (consumed by the badge in the TopBar).
 * Destructive endpoints are admin-bearer (gated server-side via
 * `require_bearer_token`) and routed through `adminFetch`.
 */
import { fetchJson } from "../../shared/lib/fetchClient";
import { adminFetch } from "../../shared/lib/adminApi";
import type {
  WatchdogFinding,
  WatchdogStatus,
} from "../../shared/types/common";

export const watchdogApi = {
  getStatus: () => fetchJson<WatchdogStatus>("/api/watchdog"),
  getRecent: (n = 100) =>
    fetchJson<WatchdogFinding[]>(`/api/watchdog/recent?n=${n}`),
  deleteFindings: (keys: string[]) =>
    adminFetch<{ deleted: number }>("/api/watchdog/findings/delete", {
      method: "POST",
      body: JSON.stringify({ keys }),
    }),
  clearAll: () =>
    adminFetch<{ deleted: number }>("/api/watchdog/findings?clear_all=true", {
      method: "DELETE",
    }),
};

export const watchdogQueryKeys = {
  all: ["watchdog"] as const,
  combined: ["watchdog", "combined"] as const,
};
