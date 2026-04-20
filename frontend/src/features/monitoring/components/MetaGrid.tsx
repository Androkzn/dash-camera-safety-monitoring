/**
 * MetaGrid — three meta cards under the summary grid (Watchdog / Active
 * Queue / Cadence).
 */
import type { WatchdogStatus } from "../../../shared/types/common";

import styles from "../MonitoringPage.module.css";

interface MetaGridProps {
  status: WatchdogStatus | null;
  filteredCount: number;
  repeatingIncidents: number;
}

export function MetaGrid({ status, filteredCount, repeatingIncidents }: MetaGridProps) {
  const lastAgo = status?.last_run_ago_sec;
  return (
    <div className={styles.metaGrid}>
      <div className={styles.metaCard}>
        <span className={styles.metaLabel}>Watchdog</span>
        <strong>{status?.run_count ?? 0} runs</strong>
        <span>
          {lastAgo != null
            ? `Last check ${Math.round(lastAgo)}s ago`
            : "Waiting for first check"}
        </span>
      </div>
      <div className={styles.metaCard}>
        <span className={styles.metaLabel}>Active Queue</span>
        <strong>{filteredCount} visible incidents</strong>
        <span>{repeatingIncidents} repeating in the recent window</span>
      </div>
      <div className={styles.metaCard}>
        <span className={styles.metaLabel}>Cadence</span>
        <strong>{status?.interval_sec ?? 60}s interval</strong>
        <span>Grouped by incident fingerprint, not raw line count</span>
      </div>
    </div>
  );
}
