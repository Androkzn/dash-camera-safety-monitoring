/**
 * useScene — TanStack Query for the scene-classifier output.
 *
 * Scene context drifts slowly (urban → highway transitions take seconds),
 * so a 7s refetch is plenty. The cache is shared by every consumer.
 */
import { useQuery } from "@tanstack/react-query";

import { dashboardApi, dashboardQueryKeys } from "../api";

export function useScene(refetchIntervalMs = 7000) {
  return useQuery({
    queryKey: dashboardQueryKeys.scene,
    queryFn: dashboardApi.getScene,
    refetchInterval: refetchIntervalMs,
  });
}
