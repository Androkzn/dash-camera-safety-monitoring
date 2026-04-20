/**
 * IncidentFeed — full incident list section, including the empty state.
 */
import type { WatchdogStatus } from "../../../shared/types/common";
import type { SevFilter, WatchdogIncident } from "../types";

import { IncidentCard } from "./IncidentCard";
import styles from "../MonitoringPage.module.css";

interface IncidentFeedProps {
  filter: SevFilter;
  status: WatchdogStatus | null;
  incidents: WatchdogIncident[];
  selectMode: boolean;
  selected: Set<string>;
  onToggleSelect: (id: string) => void;
  onDelete: (rawKeys: string[]) => void;
}

export function IncidentFeed({
  filter,
  status,
  incidents,
  selectMode,
  selected,
  onToggleSelect,
  onDelete,
}: IncidentFeedProps) {
  return (
    <section className={styles.feedSection}>
      <div className={styles.sectionHeader}>
        {filter === "all"
          ? `Showing ${incidents.length} incident groups`
          : `Showing ${incidents.length} ${filter} incident${incidents.length !== 1 ? "s" : ""}`}
      </div>

      {incidents.length === 0 && (
        <div className={styles.emptyList}>
          {filter !== "all"
            ? `No ${filter} incidents in the recent window`
            : status?.run_count
              ? "No active issues found in the recent window"
              : "Waiting for the first watchdog check…"}
        </div>
      )}

      <div className={styles.incidentList}>
        {incidents.map((incident) => (
          <IncidentCard
            key={incident.id}
            incident={incident}
            selectMode={selectMode}
            isSelected={selected.has(incident.id)}
            onToggleSelect={onToggleSelect}
            onDelete={onDelete}
          />
        ))}
      </div>
    </section>
  );
}
