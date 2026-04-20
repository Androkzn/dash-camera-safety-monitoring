/**
 * useDriftCount — number of validator (shadow-model) findings where the
 * second detector disagreed with the first: false-positive, classification
 * mismatch, or false-negative (shadow-only detection).
 *
 * Drives the red bubble on the Validation nav link. Reads from the
 * watchdog context so every tab shares one query / one cache.
 */
import { useMemo } from "react";

import { useWatchdogCtx } from "../../watchdog";

export function useDriftCount(): number {
  const { findings } = useWatchdogCtx();
  return useMemo(
    () => (findings ?? []).filter((f) => f.category === "validator").length,
    [findings],
  );
}
