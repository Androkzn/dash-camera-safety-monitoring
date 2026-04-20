/**
 * OpsDeltas — operational-metric before/after rows for the Impact card.
 *
 * "Lower is better" and "higher is better" are coloured separately:
 * CPU, latency, cost and skip-rate going UP is bad (red); fps going
 * DOWN is bad (red).
 */
import { Fragment } from "react";

import { fmt } from "../utils/formatting";
import type { WindowStats } from "../types";

import styles from "../SettingsPage.module.css";

interface OpsDeltasProps {
  baseline: WindowStats;
  after: WindowStats;
  deltas: Record<string, number>;
}

type Row = {
  key: keyof WindowStats;
  label: string;
  goodWhen: "up" | "down";
  digits?: number;
};

const ROWS: Row[] = [
  { key: "actual_fps_p50", label: "Actual fps p50", goodWhen: "up", digits: 2 },
  { key: "cpu_p95", label: "CPU %, p95", goodWhen: "down", digits: 1 },
  { key: "memory_p95", label: "Memory %, p95", goodWhen: "down", digits: 1 },
  { key: "llm_cost_usd_per_min", label: "LLM $ / min", goodWhen: "down", digits: 4 },
  { key: "llm_tokens_per_min", label: "LLM tokens / min", goodWhen: "down", digits: 0 },
  { key: "llm_latency_p95_ms", label: "LLM latency p95 ms", goodWhen: "down", digits: 0 },
  { key: "llm_skip_rate", label: "LLM skip rate", goodWhen: "down", digits: 3 },
  { key: "frames_dropped_ratio_p95", label: "Frames dropped p95", goodWhen: "down", digits: 3 },
];

export function OpsDeltas({ baseline, after, deltas }: OpsDeltasProps) {
  const visible = ROWS.filter(
    (r) => baseline[r.key] != null || after[r.key] != null,
  );
  if (!visible.length) return null;
  return (
    <>
      <div className={styles.subtle} style={{ fontSize: 11, marginTop: 6 }}>
        Operational
      </div>
      <div className={styles.deltaList}>
        {visible.map((r) => {
          const b = baseline[r.key] as number | null | undefined;
          const a = after[r.key] as number | null | undefined;
          const d = deltas[r.key];
          const isUp = (d ?? 0) > 0;
          const good =
            d == null || d === 0
              ? null
              : r.goodWhen === "up"
                ? isUp
                : !isUp;
          const deltaClass =
            good == null ? "" : good ? styles.deltaPos : styles.deltaNeg;
          return (
            <Fragment key={String(r.key)}>
              <span>{r.label}</span>
              <span>
                {fmt(b ?? null, r.digits ?? 2)} → {fmt(a ?? null, r.digits ?? 2)}
              </span>
              <span className={deltaClass}>
                {d != null ? `${fmt(d, 1)}%` : ""}
              </span>
            </Fragment>
          );
        })}
      </div>
      {after.ops_samples === 0 && (
        <div className={styles.subtle} style={{ fontSize: 10 }}>
          Waiting for operational samples (first CPU / fps window after apply).
        </div>
      )}
    </>
  );
}
