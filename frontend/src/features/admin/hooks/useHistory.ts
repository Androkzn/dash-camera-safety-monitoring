/**
 * useHistory — loads a filtered slice of `/api/live/events` on demand
 * for the Admin "history" panel. No auto-refresh — caller drives loads
 * via `updateFilters(patch)` or `refresh()`.
 */
import { useState, useCallback } from "react";

import { adminApi } from "../api";
import type { SafetyEvent } from "../../../shared/types/common";

interface HistoryFilters {
  risk_level: string;
  event_type: string;
}

export function useHistory() {
  const [events, setEvents] = useState<SafetyEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState<HistoryFilters>({
    risk_level: "",
    event_type: "",
  });

  const load = useCallback(
    async (f?: HistoryFilters) => {
      const activeFilters = f ?? filters;
      setLoading(true);
      setError(null);
      try {
        const items = await adminApi.getLiveEvents({
          risk_level: activeFilters.risk_level || undefined,
          event_type: activeFilters.event_type || undefined,
          limit: 200,
        });
        setEvents(items.reverse());
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    },
    [filters],
  );

  const updateFilters = useCallback(
    (patch: Partial<HistoryFilters>) => {
      setFilters((prev) => {
        const next = { ...prev, ...patch };
        load(next);
        return next;
      });
    },
    [load],
  );

  return {
    events,
    loading,
    error,
    filters,
    updateFilters,
    refresh: () => load(),
  };
}
