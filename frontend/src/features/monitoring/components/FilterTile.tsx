/**
 * FilterTile — single clickable severity counter inside the summary grid.
 */
import styles from "../MonitoringPage.module.css";

interface FilterTileProps {
  label: string;
  value: number | string;
  variant: string;
  active: boolean;
  onClick: () => void;
}

export function FilterTile({ label, value, variant, active, onClick }: FilterTileProps) {
  return (
    <button
      className={`${styles.tile} ${styles[`t${variant}`]} ${active ? styles.tileActive : ""}`}
      onClick={onClick}
    >
      <div className={styles.tLabel}>{label}</div>
      <div className={styles.tValue}>{value}</div>
    </button>
  );
}
