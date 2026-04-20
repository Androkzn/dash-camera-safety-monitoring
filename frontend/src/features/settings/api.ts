/**
 * Settings API surface — every endpoint that requires
 * `Authorization: Bearer <ROAD_ADMIN_TOKEN>`. Routed through the
 * `adminFetch` wrapper so missing-token errors propagate as a typed
 * `MissingAdminTokenError`.
 */
import { adminFetch } from "../../shared/lib/adminApi";

import type {
  ApplyResultPayload,
  EffectiveSettings,
  ImpactReport,
  SettingsSchema,
  SettingsTemplate,
} from "./types";

interface ApplyOptions {
  expected_revision_hash?: string;
  confirm_privacy_change?: boolean;
  operator_label?: string | null;
  note?: string | null;
}

interface ImpactResponse {
  report: ImpactReport | null;
}

interface TemplatesResponse {
  templates: SettingsTemplate[];
}

export const settingsApi = {
  getSchema: () => adminFetch<SettingsSchema>("/api/settings/schema"),
  getEffective: () => adminFetch<EffectiveSettings>("/api/settings/effective"),

  validate: (diff: Record<string, unknown>) =>
    adminFetch<unknown>("/api/settings/validate", {
      method: "POST",
      body: JSON.stringify({ diff }),
    }),

  apply: (diff: Record<string, unknown>, opts: ApplyOptions = {}) =>
    adminFetch<ApplyResultPayload>("/api/settings/apply", {
      method: "POST",
      body: JSON.stringify({
        diff,
        expected_revision_hash: opts.expected_revision_hash,
        confirm_privacy_change: !!opts.confirm_privacy_change,
        operator_label: opts.operator_label ?? null,
        note: opts.note ?? null,
      }),
    }),

  rollback: () =>
    adminFetch<ApplyResultPayload>("/api/settings/rollback", { method: "POST" }),

  captureBaseline: () =>
    adminFetch<{ ok: boolean; audit_id: string }>(
      "/api/settings/baseline/capture",
      { method: "POST" },
    ),

  getImpact: () => adminFetch<ImpactResponse>("/api/settings/impact"),

  // --- Templates ---
  listTemplates: () => adminFetch<TemplatesResponse>("/api/settings/templates"),

  createTemplate: (
    name: string,
    description: string,
    payload: Record<string, unknown>,
  ) =>
    adminFetch<SettingsTemplate>("/api/settings/templates", {
      method: "POST",
      body: JSON.stringify({ name, description, payload }),
    }),

  deleteTemplate: (id: string) =>
    adminFetch<unknown>(`/api/settings/templates/${id}`, { method: "DELETE" }),

  applyTemplate: (
    id: string,
    opts: { confirm_privacy_change?: boolean } = {},
  ) =>
    adminFetch<ApplyResultPayload>(`/api/settings/templates/${id}/apply`, {
      method: "POST",
      body: JSON.stringify({
        confirm_privacy_change: !!opts.confirm_privacy_change,
      }),
    }),
};

export const settingsQueryKeys = {
  schema: ["settings", "schema"] as const,
  effective: ["settings", "effective"] as const,
  impact: ["settings", "impact"] as const,
  templates: ["settings", "templates"] as const,
};
