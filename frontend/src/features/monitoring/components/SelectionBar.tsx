/**
 * SelectionBar — bulk-edit toolbar visible only in select mode.
 */
import styles from "../MonitoringPage.module.css";

interface SelectionBarProps {
  selectedCount: number;
  filteredCount: number;
  deleting: boolean;
  onSelectAll: () => void;
  onDeselectAll: () => void;
  onDeleteSelected: () => void;
  onCancel: () => void;
}

export function SelectionBar({
  selectedCount,
  filteredCount,
  deleting,
  onSelectAll,
  onDeselectAll,
  onDeleteSelected,
  onCancel,
}: SelectionBarProps) {
  return (
    <div className={styles.selectionBar}>
      <div className={styles.selectionInfo}>
        <span>{selectedCount} incident groups selected</span>
        <button className={styles.selBarBtn} onClick={onSelectAll}>
          Select all ({filteredCount})
        </button>
        <button className={styles.selBarBtn} onClick={onDeselectAll}>
          Deselect all
        </button>
      </div>
      <div className={styles.selectionActions}>
        <button
          className={`${styles.selBarBtn} ${styles.deleteBtn}`}
          onClick={onDeleteSelected}
          disabled={selectedCount === 0 || deleting}
        >
          {deleting ? "Deleting…" : `Delete (${selectedCount})`}
        </button>
        <button className={styles.selBarBtn} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
