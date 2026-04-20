/**
 * useValidator — polls /api/validator/status and exposes an enable/disable
 * mutator backed by /api/validator/toggle.
 *
 * Optimistic updates: the checkbox flips immediately on click, then the
 * mutator rewrites the cache with the server's response. If the request
 * fails we revert via query invalidation.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchJson, postJson } from "../../../shared/lib/fetchClient";

export interface ValidatorStatusPayload {
  enabled: boolean;
  paused?: boolean;
  running?: boolean;
  backend?: string;
  model_path?: string;
  device?: string;
  queue_depth?: number;
  queue_max?: number;
  sample_sec?: number;
  iou_threshold?: number;
  jobs_processed?: number;
  jobs_dropped?: number;
  findings_emitted?: number;
  episodes_enqueued?: number;
  overflow_depth?: number;
}

const VALIDATOR_QUERY_KEY = ["monitoring", "validator"] as const;

export function useValidator(refetchMs = 5000) {
  const qc = useQueryClient();

  const status = useQuery<ValidatorStatusPayload>({
    queryKey: VALIDATOR_QUERY_KEY,
    queryFn: () => fetchJson<ValidatorStatusPayload>("/api/validator/status"),
    refetchInterval: refetchMs,
    staleTime: 2000,
  });

  const toggle = useMutation({
    mutationFn: (enabled: boolean) =>
      postJson<ValidatorStatusPayload, { enabled: boolean }>(
        "/api/validator/toggle",
        { enabled },
      ),
    onMutate: async (enabled) => {
      await qc.cancelQueries({ queryKey: VALIDATOR_QUERY_KEY });
      const previous = qc.getQueryData<ValidatorStatusPayload>(
        VALIDATOR_QUERY_KEY,
      );
      if (previous) {
        qc.setQueryData<ValidatorStatusPayload>(VALIDATOR_QUERY_KEY, {
          ...previous,
          paused: !enabled,
        });
      }
      return { previous };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.previous) {
        qc.setQueryData(VALIDATOR_QUERY_KEY, ctx.previous);
      }
    },
    onSuccess: (payload) => {
      qc.setQueryData(VALIDATOR_QUERY_KEY, payload);
    },
  });

  return {
    status: status.data ?? null,
    isLoading: status.isLoading,
    error: status.error as Error | null,
    setEnabled: (enabled: boolean) => toggle.mutate(enabled),
    isPending: toggle.isPending,
  };
}
