/**
 * SettingsHeader — page title + Discard / Rollback / Apply action bar.
 */
import styles from "../SettingsPage.module.css";

interface SettingsHeaderProps {
  dirtyCount: number;
  submitting: boolean;
  onDiscard: () => void;
  onRollback: () => void;
  onApply: () => void;
}

export function SettingsHeader({
  dirtyCount,
  submitting,
  onDiscard,
  onRollback,
  onApply,
}: SettingsHeaderProps) {
  return (
    <div className={styles.pageHeader}>
      <div className={styles.pageTitleGroup}>
        <h1 className={styles.pageTitle}>Settings</h1>
      </div>
      <div className={styles.headerActions}>
        <span className={styles.dirtyCount}>
          {dirtyCount} pending change{dirtyCount === 1 ? "" : "s"}
        </span>
        <button
          className={styles.btn}
          disabled={!dirtyCount}
          onClick={onDiscard}
        >
          Discard
        </button>
        <button
          className={`${styles.btn} ${styles.btnDanger}`}
          disabled={submitting}
          onClick={onRollback}
        >
          Rollback to last-good
        </button>
        <button
          className={`${styles.btn} ${styles.btnPrimary}`}
          disabled={!dirtyCount || submitting}
          onClick={onApply}
        >
          {submitting
            ? "Applying…"
            : `Apply${dirtyCount ? ` (${dirtyCount})` : ""}`}
        </button>
      </div>
    </div>
  );
}
