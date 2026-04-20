/**
 * useTests — TanStack Query wrapper around the pytest runner status.
 *
 * The poll interval flips between fast (1.5s) while a run is in
 * progress and slow (10s) while idle. TanStack supports per-call
 * function-form `refetchInterval` so we don't need a second effect.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { testsApi, testsQueryKeys } from "../api";
import type { TestStatus } from "../../../shared/types/common";

export function useTests() {
  const qc = useQueryClient();
  const { data: status, refetch } = useQuery<TestStatus>({
    queryKey: testsQueryKeys.status,
    queryFn: testsApi.getStatus,
    refetchInterval: (query) =>
      query.state.data?.status === "running" ? 1_500 : 10_000,
    staleTime: 1_000,
    refetchOnWindowFocus: true,
  });

  const rerun = useMutation({
    mutationFn: testsApi.run,
    onSuccess: () => qc.invalidateQueries({ queryKey: testsQueryKeys.status }),
  });

  return {
    status: status ?? null,
    rerun: () => rerun.mutateAsync(),
    refetch,
  };
}
