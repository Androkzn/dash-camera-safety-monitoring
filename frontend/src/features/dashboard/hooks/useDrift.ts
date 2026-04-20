/**
 * useDrift — TanStack Query for the drift report. Long interval (30s)
 * because labelled-feedback drift is a slow signal — polling aggressively
 * would just waste battery on the edge.
 */
import { useQuery } from "@tanstack/react-query";

import { dashboardApi, dashboardQueryKeys } from "../api";

export function useDrift(refetchIntervalMs = 30_000) {
  return useQuery({
    queryKey: dashboardQueryKeys.drift,
    queryFn: dashboardApi.getDrift,
    refetchInterval: refetchIntervalMs,
  });
}
