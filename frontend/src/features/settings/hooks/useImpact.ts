/**
 * useImpact — polls `/api/settings/impact` for the active session.
 */
import { useEffect, useState } from "react";

import {
  clearAdminToken,
  isAdminAuthFailure,
} from "../../../shared/lib/adminApi";

import { settingsApi } from "../api";
import type { ImpactReport } from "../types";

const POLL_MS = 5_000;

export interface ImpactState {
  report: ImpactReport | null;
  refreshing: boolean;
  error: string | null;
  lastUpdatedTs: number | null;
  refresh: () => Promise<void>;
}

export function useImpact(token: string | null): ImpactState {
  const [report, setReport] = useState<ImpactReport | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdatedTs, setLastUpdatedTs] = useState<number | null>(null);

  const refresh = async () => {
    if (!token) return;
    setRefreshing(true);
    try {
      const data = await settingsApi.getImpact();
      setReport(data.report);
      setLastUpdatedTs(Date.now());
      setError(null);
    } catch (exc) {
      if (isAdminAuthFailure(exc)) clearAdminToken();
      if (exc instanceof Error) setError(exc.message);
    } finally {
      setRefreshing(false);
    }
  };

  useEffect(() => {
    if (!token) return;
    void refresh();
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  return { report, refreshing, error, lastUpdatedTs, refresh };
}
