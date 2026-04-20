/**
 * ImpactCard — before/after deltas + ops + severity bars + recommendation.
 */
import { useEffect, useState } from "react";

import {
  fmt,
  humanize,
  metricLabel,
  reasonLabel,
  tierClass,
} from "../utils/formatting";
import type { ImpactReport } from "../types";

import { OpsDeltas } from "./OpsDeltas";
import { SeverityBars } from "./SeverityBars";

import styles from "../SettingsPage.module.css";

interface ImpactCardProps {
  report: ImpactReport | null;
  refreshing: boolean;
  lastUpdatedTs: number | null;
  onRefresh: () => void;
}

export function ImpactCard({
  report: r,
  refreshing,
  lastUpdatedTs,
  onRefresh,
}: ImpactCardProps) {
  // Tick once a second so the "Xs ago" label stays live between polls.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, []);
  const ago =
    lastUpdatedTs === null
      ? null
      : Math.max(0, Math.round((Date.now() - lastUpdatedTs) / 1000));

  if (!r) {
    return (
      <div className={styles.card}>
        <div className={styles.cardHeader}>
          <h3 className={styles.cardTitle}>Impact</h3>
        </div>
        <p className={styles.subtle} style={{ margin: 0 }}>
          No active session yet. Apply a change or capture a baseline.
        </p>
      </div>
    );
  }

  return (
    <div className={styles.card}>
      <div className={styles.cardHeader}>
        <h3 className={styles.cardTitle}>Impact ({humanize(r.state)})</h3>
        <span
          className={`${styles.confidenceTier} ${tierClass(r.confidence_tier)}`}
        >
          {humanize(r.confidence_tier)}
        </span>
      </div>

      {r.changed_keys.length > 0 && (
        <div className={styles.subtle} style={{ fontSize: 11 }}>
          {r.changed_keys.length} key{r.changed_keys.length === 1 ? "" : "s"}{" "}
          changed: {r.changed_keys.slice(0, 2).map(humanize).join(", ")}
          {r.changed_keys.length > 2 ? "…" : ""}
        </div>
      )}

      {r.confidence_reasons.length > 0 && (
        <div className={styles.reasonList}>
          {r.confidence_reasons.map((reason) => (
            <span key={reason} className={styles.reasonChip} title={reason}>
              {reasonLabel(reason)}
            </span>
          ))}
        </div>
      )}

      {r.baseline && r.after_window && (
        <>
          <div className={styles.deltaList}>
            <span>{metricLabel("event_rate_per_min")}</span>
            <span>
              {fmt(r.baseline.event_rate_per_min)} →{" "}
              {fmt(r.after_window.event_rate_per_min)}
            </span>
            <span
              className={
                (r.deltas.event_rate_per_min ?? 0) > 0
                  ? styles.deltaNeg
                  : styles.deltaPos
              }
            >
              {fmt(r.deltas.event_rate_per_min, 1)}%
            </span>

            <span>{metricLabel("confidence_p50")}</span>
            <span>
              {fmt(r.baseline.confidence_p50)} →{" "}
              {fmt(r.after_window.confidence_p50)}
            </span>
            <span
              className={
                (r.deltas.confidence_p50 ?? 0) > 0
                  ? styles.deltaPos
                  : styles.deltaNeg
              }
            >
              {fmt(r.deltas.confidence_p50, 1)}%
            </span>

            <span>{metricLabel("ttc_p95")}</span>
            <span>
              {fmt(r.baseline.ttc_p95)} → {fmt(r.after_window.ttc_p95)}
            </span>
            <span
              className={
                (r.deltas.ttc_p95 ?? 0) > 0 ? styles.deltaPos : styles.deltaNeg
              }
            >
              {fmt(r.deltas.ttc_p95, 1)}%
            </span>

            <span>{metricLabel("sample_size")}</span>
            <span>
              {r.baseline.sample_size} → {r.after_window.sample_size}
            </span>
            <span></span>
          </div>

          <OpsDeltas
            baseline={r.baseline}
            after={r.after_window}
            deltas={r.deltas}
          />

          <SeverityBars
            label="Severity (after-change)"
            counts={r.after_window.severity_counts}
          />
        </>
      )}

      {r.narrative && (
        <div className={styles.narrative}>
          <strong>{(r.recommendation ?? "monitor").toUpperCase()}</strong>:{" "}
          {r.narrative}
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <button
          className={styles.btn}
          onClick={onRefresh}
          disabled={refreshing}
        >
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
        <span className={styles.subtle} style={{ fontSize: 11 }}>
          auto · every 5s{ago !== null ? ` · updated ${ago}s ago` : ""}
        </span>
      </div>

      {r.lagging_metrics.length > 0 && (
        <div className={styles.subtle} style={{ fontSize: 10 }}>
          Lagging metrics ({r.lagging_metrics.map(metricLabel).join(", ")}) need
          operator feedback before they populate.
        </div>
      )}
    </div>
  );
}
