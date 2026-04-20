/**
 * SummaryGrid — the four-tile severity row. Click to filter; click the
 * active tile to reset to "all".
 */
import type { SevFilter } from "../types";

import styles from "../MonitoringPage.module.css";
import { FilterTile } from "./FilterTile";

interface SummaryGridProps {
  errors: number;
  warnings: number;
  infos: number;
  totalIncidents: number;
  filter: SevFilter;
  onToggle: (sev: SevFilter) => void;
  onShowAll: () => void;
}

export function SummaryGrid({
  errors,
  warnings,
  infos,
  totalIncidents,
  filter,
  onToggle,
  onShowAll,
}: SummaryGridProps) {
  return (
    <div className={styles.summaryGrid}>
      <FilterTile
        label="Errors"
        value={errors}
        variant="error"
        active={filter === "error"}
        onClick={() => onToggle("error")}
      />
      <FilterTile
        label="Warnings"
        value={warnings}
        variant="warning"
        active={filter === "warning"}
        onClick={() => onToggle("warning")}
      />
      <FilterTile
        label="Info"
        value={infos}
        variant="info"
        active={filter === "info"}
        onClick={() => onToggle("info")}
      />
      <FilterTile
        label="Incidents"
        value={totalIncidents}
        variant="total"
        active={filter === "all"}
        onClick={onShowAll}
      />
    </div>
  );
}
