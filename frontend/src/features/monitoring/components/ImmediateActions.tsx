/**
 * ImmediateActions — top-3 non-info incidents as quick-jump buttons.
 * Clicking scrolls the matching IncidentCard into view.
 */
import { formatRelative } from "../utils/formatting";
import type { WatchdogIncident } from "../types";

import styles from "../MonitoringPage.module.css";

interface ImmediateActionsProps {
  incidents: WatchdogIncident[];
}

export function ImmediateActions({ incidents }: ImmediateActionsProps) {
  if (incidents.length === 0) return null;
  return (
    <section className={styles.queueSection}>
      <div className={styles.sectionHeader}>Immediate Actions</div>
      <div className={styles.queueGrid}>
        {incidents.map((incident) => (
          <button
            key={incident.id}
            className={`${styles.queueCard} ${styles[incident.severity]}`}
            onClick={() => {
              const el = document.getElementById(`incident-${incident.id}`);
              el?.scrollIntoView({ behavior: "smooth", block: "start" });
            }}
          >
            <span className={styles.queueSeverity}>
              {incident.severity.toUpperCase()}
            </span>
            <span className={styles.queueTitle}>{incident.title}</span>
            <span className={styles.queueNext}>
              {incident.latest.suggestion || incident.latest.detail}
            </span>
            <span className={styles.queueMeta}>
              {incident.owner || incident.category} • last seen{" "}
              {formatRelative(incident.lastSeen)}
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}
