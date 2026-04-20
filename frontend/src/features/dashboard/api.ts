/**
 * Dashboard API surface — scene context, drift report, copilot chat.
 */
import { fetchJson, postJson } from "../../shared/lib/fetchClient";
import type {
  DriftReport,
  SceneContext,
} from "../../shared/types/common";

export const dashboardApi = {
  getScene: () => fetchJson<SceneContext>("/api/live/scene"),
  getDrift: () => fetchJson<DriftReport>("/api/drift"),
  chat: (query: string) =>
    postJson<{ answer: string }>("/chat", { query }),
};

export const dashboardQueryKeys = {
  scene: ["dashboard", "scene"] as const,
  drift: ["dashboard", "drift"] as const,
};
