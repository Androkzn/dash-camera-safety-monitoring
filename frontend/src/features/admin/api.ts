/**
 * Admin API surface — health snapshot, live sources lifecycle, history.
 */
import { fetchJson } from "../../shared/lib/fetchClient";
import type {
  HealthData,
  LiveSourceStatus,
  LiveSourcesResponse,
  SafetyEvent,
} from "../../shared/types/common";

export const adminApi = {
  getHealth: () => fetchJson<HealthData>("/api/admin/health"),

  getLiveSources: () => fetchJson<LiveSourcesResponse>("/api/live/sources"),
  startLiveSource: (id: string) =>
    fetchJson<LiveSourceStatus>(
      `/api/live/sources/${encodeURIComponent(id)}/start`,
      { method: "POST" },
    ),
  pauseLiveSource: (id: string) =>
    fetchJson<LiveSourceStatus>(
      `/api/live/sources/${encodeURIComponent(id)}/pause`,
      { method: "POST" },
    ),
  restartAllLiveSources: () =>
    fetchJson<{ ok: boolean; results: Array<LiveSourceStatus & { ok: boolean; error?: string }> }>(
      "/api/live/sources/restart_all",
      { method: "POST" },
    ),
  setLiveSourceDetection: (id: string, enabled: boolean) =>
    fetchJson<LiveSourceStatus>(
      `/api/live/sources/${encodeURIComponent(id)}/detection?enabled=${enabled}`,
      { method: "POST" },
    ),
  addLiveSource: (body: {
    url: string;
    name?: string;
    id?: string;
    autostart?: boolean;
  }) =>
    fetchJson<LiveSourceStatus & { ok: boolean; error?: string }>(
      "/api/live/sources",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  removeLiveSource: (id: string) =>
    fetchJson<{ ok: boolean; removed: string }>(
      `/api/live/sources/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),

  getLiveEvents: (params?: {
    risk_level?: string;
    event_type?: string;
    limit?: number;
  }) => {
    const q = new URLSearchParams();
    if (params?.risk_level) q.set("risk_level", params.risk_level);
    if (params?.event_type) q.set("event_type", params.event_type);
    q.set("limit", String(params?.limit ?? 200));
    return fetchJson<SafetyEvent[]>(`/api/live/events?${q}`);
  },
};

export const adminQueryKeys = {
  health: ["admin", "health"] as const,
  liveSources: ["admin", "liveSources"] as const,
};
