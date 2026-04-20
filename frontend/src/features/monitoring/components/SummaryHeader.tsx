/**
 * SummaryHeader — title + Select / Clear All actions on the Monitoring page.
 */
import type { ReactNode } from "react";

import styles from "../MonitoringPage.module.css";

interface SummaryHeaderProps {
  selectMode: boolean;
  totalIncidents: number;
  filteredCount: number;
  deleting: boolean;
  onEnterSelect: () => void;
  onClearAll: () => void;
  children?: ReactNode;
}

export function SummaryHeader({
  selectMode,
  totalIncidents,
  filteredCount,
  deleting,
  onEnterSelect,
  onClearAll,
  children,
}: SummaryHeaderProps) {
  return (
    <div className={styles.titleRow}>
      <div>
        <h1>Error Monitoring</h1>
        <p className={styles.subtitle}>
          Grouped into actionable incidents with impact, evidence, and next debugging moves.
        </p>
      </div>
      <div className={styles.headerActions}>
        {!selectMode && filteredCount > 0 && (
          <button className={styles.actionBtn} onClick={onEnterSelect}>
            Select
          </button>
        )}
        {!selectMode && totalIncidents > 0 && (
          <button
            className={`${styles.actionBtn} ${styles.clearBtn}`}
            onClick={onClearAll}
            disabled={deleting}
          >
            {deleting ? "Clearing…" : "Clear All"}
          </button>
        )}
        {children}
      </div>
    </div>
  );
}
