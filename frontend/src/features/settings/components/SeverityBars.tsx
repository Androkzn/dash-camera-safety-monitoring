/**
 * SeverityBars — stacked horizontal bars for severity counts.
 */
import { severityLabel } from "../utils/formatting";

import styles from "../SettingsPage.module.css";

interface SeverityBarsProps {
  label: string;
  counts: Record<string, number>;
}

export function SeverityBars({ label, counts }: SeverityBarsProps) {
  const total = Object.values(counts).reduce((s, n) => s + n, 0) || 1;
  const order = ["high", "medium", "low", "unknown"];
  const seen = new Set<string>();
  return (
    <div>
      <div className={styles.subtle} style={{ marginBottom: 4 }}>
        {label}
      </div>
      <div className={styles.bars}>
        {order
          .filter((k) => counts[k] != null)
          .map((k) => {
            seen.add(k);
            const v = counts[k] ?? 0;
            return (
              <div className={styles.barRow} key={k}>
                <div>
                  <div style={{ fontSize: 10, color: "var(--muted)" }}>
                    {severityLabel(k)}
                  </div>
                  <div className={styles.bar}>
                    <div
                      className={styles.barFill}
                      style={{ width: `${(v / total) * 100}%` }}
                    />
                  </div>
                </div>
                <span className={styles.subtle}>{v}</span>
              </div>
            );
          })}
        {Object.entries(counts)
          .filter(([k]) => !seen.has(k))
          .map(([k, v]) => (
            <div className={styles.barRow} key={k}>
              <div>
                <div style={{ fontSize: 10, color: "var(--muted)" }}>
                  {severityLabel(k)}
                </div>
                <div className={styles.bar}>
                  <div
                    className={styles.barFill}
                    style={{ width: `${(v / total) * 100}%` }}
                  />
                </div>
              </div>
              <span className={styles.subtle}>{v}</span>
            </div>
          ))}
      </div>
    </div>
  );
}
