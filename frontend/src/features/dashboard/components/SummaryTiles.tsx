/**
 * SummaryTiles — KPI tiles row (Events / High / Medium / Uptime).
 */
import { formatUptime } from "../../../shared/lib/format";

import styles from "./SummaryTiles.module.css";

interface SummaryTilesProps {
  total: number;
  high: number;
  medium: number;
  uptimeSec?: number | null;
}

export function SummaryTiles({ total, high, medium, uptimeSec }: SummaryTilesProps) {
  return (
    <div className={styles.summary}>
      <div className={styles.tile}>
        <div className={styles.label}>Events</div>
        <div className={styles.value}>{total}</div>
      </div>
      <div className={`${styles.tile} ${styles.high}`}>
        <div className={styles.label}>High risk</div>
        <div className={styles.value}>{high}</div>
      </div>
      <div className={`${styles.tile} ${styles.medium}`}>
        <div className={styles.label}>Medium risk</div>
        <div className={styles.value}>{medium}</div>
      </div>
      <div className={styles.tile}>
        <div className={styles.label}>Uptime</div>
        <div className={styles.value}>{formatUptime(uptimeSec)}</div>
      </div>
    </div>
  );
}
