/**
 * useSettingsTemplates — list/create/delete/apply settings templates.
 */
import { useCallback, useEffect, useState } from "react";

import {
  clearAdminToken,
  isAdminAuthFailure,
} from "../../../shared/lib/adminApi";

import { settingsApi } from "../api";
import type { ApplyResultPayload, SettingsTemplate } from "../types";

export interface SettingsTemplatesState {
  templates: SettingsTemplate[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  create: (
    name: string,
    description: string,
    payload: Record<string, unknown>,
  ) => Promise<SettingsTemplate>;
  remove: (id: string) => Promise<void>;
  applyTemplate: (
    id: string,
    opts?: { confirm_privacy_change?: boolean },
  ) => Promise<ApplyResultPayload>;
}

export function useSettingsTemplates(
  token: string | null,
): SettingsTemplatesState {
  const [templates, setTemplates] = useState<SettingsTemplate[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const data = await settingsApi.listTemplates();
      setTemplates(data.templates);
    } catch (exc) {
      if (isAdminAuthFailure(exc)) clearAdminToken();
      if (exc instanceof Error) setError(exc.message);
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const create = useCallback(
    async (
      name: string,
      description: string,
      payload: Record<string, unknown>,
    ) => {
      const tmpl = await settingsApi.createTemplate(name, description, payload);
      await refresh();
      return tmpl;
    },
    [refresh],
  );

  const remove = useCallback(
    async (id: string) => {
      await settingsApi.deleteTemplate(id);
      await refresh();
    },
    [refresh],
  );

  const applyTemplate = useCallback(
    async (id: string, opts: { confirm_privacy_change?: boolean } = {}) => {
      const result = await settingsApi.applyTemplate(id, opts);
      await refresh();
      return result;
    },
    [refresh],
  );

  return { templates, loading, error, refresh, create, remove, applyTemplate };
}
