/**
 * useSettings — fetches schema + effective values, exposes apply / rollback.
 *
 * Both endpoints are admin-bearer; we route them via TanStack Query so
 * the cache + dedup works as for any other feature, but we surface the
 * "needs token" empty-state (and drop the cached token on auth failure)
 * via local state plus the `MissingAdminTokenError` discriminator.
 */
import { useCallback, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  clearAdminToken,
  isAdminAuthFailure,
  MissingAdminTokenError,
  type AdminApiError,
} from "../../../shared/lib/adminApi";

import { settingsApi, settingsQueryKeys } from "../api";
import type {
  ApplyResultPayload,
  EffectiveSettings,
  SettingsSchema,
} from "../types";

interface ApplyOptions {
  expected_revision_hash?: string;
  confirm_privacy_change?: boolean;
  operator_label?: string;
  note?: string;
}

export interface SettingsState {
  schema: SettingsSchema | null;
  effective: EffectiveSettings | null;
  loading: boolean;
  error: string | null;
  needsToken: boolean;
  refresh: () => Promise<void>;
  apply: (
    diff: Record<string, unknown>,
    opts?: ApplyOptions,
  ) => Promise<ApplyResultPayload>;
  rollback: () => Promise<ApplyResultPayload>;
  validate: (diff: Record<string, unknown>) => Promise<unknown>;
}

const POLL_MS = 15_000;

export function useSettings(token: string | null): SettingsState {
  const qc = useQueryClient();
  const [schema, setSchema] = useState<SettingsSchema | null>(null);
  const [effective, setEffective] = useState<EffectiveSettings | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [needsToken, setNeedsToken] = useState<boolean>(!token);

  const refresh = useCallback(async () => {
    if (!token) {
      setNeedsToken(true);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const [s, e] = await Promise.all([
        schema
          ? Promise.resolve(schema)
          : settingsApi.getSchema(),
        settingsApi.getEffective(),
      ]);
      setSchema(s);
      setEffective(e);
      setNeedsToken(false);
    } catch (exc) {
      if (exc instanceof MissingAdminTokenError) {
        setNeedsToken(true);
      } else if (exc instanceof Error) {
        const status = (exc as AdminApiError).status;
        if (isAdminAuthFailure(exc)) {
          clearAdminToken();
          setNeedsToken(true);
          setError(
            status === 403
              ? "Admin token rejected (HTTP 403). Paste the correct ROAD_ADMIN_TOKEN."
              : status === 503
                ? "Server has no admin token configured (HTTP 503)."
                : "Authentication required (HTTP 401).",
          );
        } else {
          setError(exc.message);
        }
      }
    } finally {
      setLoading(false);
    }
  }, [schema, token]);

  useEffect(() => {
    if (!token) return;
    void refresh();
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh, token]);

  const validate = useCallback(
    async (diff: Record<string, unknown>) => settingsApi.validate(diff),
    [],
  );

  const apply = useCallback(
    async (diff: Record<string, unknown>, opts: ApplyOptions = {}) => {
      const result = await settingsApi.apply(diff, {
        expected_revision_hash:
          opts.expected_revision_hash ?? effective?.revision_hash,
        confirm_privacy_change: !!opts.confirm_privacy_change,
        operator_label: opts.operator_label ?? null,
        note: opts.note ?? null,
      });
      await refresh();
      // Invalidate the impact session so its card refreshes promptly.
      void qc.invalidateQueries({ queryKey: settingsQueryKeys.impact });
      return result;
    },
    [effective, qc, refresh],
  );

  const rollback = useCallback(async () => {
    const result = await settingsApi.rollback();
    await refresh();
    void qc.invalidateQueries({ queryKey: settingsQueryKeys.impact });
    return result;
  }, [qc, refresh]);

  return {
    schema,
    effective,
    loading,
    error,
    needsToken,
    refresh,
    apply,
    rollback,
    validate,
  };
}
