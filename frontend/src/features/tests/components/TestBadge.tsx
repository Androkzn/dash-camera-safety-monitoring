/**
 * TestBadge — pill summarising the latest pytest run, slotted into TopBar.
 */
import type { ReactNode } from "react";

import type { TestStatus } from "../../../shared/types/common";

import styles from "./TestBadge.module.css";

interface TestBadgeProps {
  status: TestStatus | null;
  onClick: () => void;
}

export function TestBadge({ status, onClick }: TestBadgeProps) {
  const state = status?.status ?? "idle";

  let label = "Tests";
  let countsContent: ReactNode = null;

  if (state === "running") {
    label = "Running";
    const pct =
      status && status.total > 0
        ? Math.round((status.progress / status.total) * 100)
        : 0;
    countsContent = <span>{pct}%</span>;
  } else if (state === "passed" || state === "failed") {
    label = state === "passed" ? "Passed" : "Failed";
    countsContent = (
      <>
        <span className={styles.cntPass}>{status?.passed ?? 0}</span>
        {(status?.failed ?? 0) > 0 && (
          <span className={styles.cntFail}>{status?.failed}</span>
        )}
      </>
    );
  }

  return (
    <div
      className={styles.badge}
      title="Click to view test results"
      onClick={onClick}
      style={{ marginLeft: 8 }}
    >
      <span className={`${styles.dot} ${styles[state]}`} />
      <span>{label}</span>
      {countsContent && <span className={styles.counts}>{countsContent}</span>}
    </div>
  );
}
