/**
 * ValidatorControl — operator checkbox for the heavy shadow-mode
 * dual-model validator. Dropped into MonitoringPage beside the other
 * summary cards. Talks to /api/validator/status + /api/validator/toggle
 * through the useValidator hook.
 */
import { useValidator } from "../hooks/useValidator";

import styles from "./ValidatorControl.module.css";

export function ValidatorControl() {
  const { status, isLoading, error, setEnabled, isPending } = useValidator();

  // When the validator was disabled at startup (ROAD_VALIDATOR_ENABLED=0),
  // the backend returns ``{enabled: false}`` and the toggle endpoint 409s.
  // Surface that explicitly so the operator knows a restart is needed.
  const startupDisabled = !!status && status.enabled === false;
  const active = !!status?.enabled && !status?.paused;
  const findings = status?.findings_emitted ?? 0;
  const jobs = status?.jobs_processed ?? 0;
  const episodes = status?.episodes_enqueued ?? 0;

  return (
    <div className={styles.card}>
      <div className={styles.row}>
        <label className={styles.label}>
          <input
            type="checkbox"
            checked={active}
            disabled={startupDisabled || isLoading || isPending}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          <span className={styles.title}>
            Heavy second validator (shadow mode)
          </span>
        </label>
        <span
          className={`${styles.dot} ${active ? styles.dotOn : styles.dotOff}`}
          aria-label={active ? "running" : "paused"}
        />
      </div>
      <p className={styles.hint}>
        Runs a second, heavier detector in the background — never gates
        live alerts, but publishes disagreements to this incident queue
        under the <code>validator</code> category.
      </p>
      {startupDisabled ? (
        <p className={styles.warn}>
          Disabled at startup. Set <code>ROAD_VALIDATOR_ENABLED=1</code>{" "}
          in <code>.env</code> and restart the server to enable runtime
          toggling.
        </p>
      ) : (
        <div className={styles.stats}>
          <span>
            <strong>{jobs.toLocaleString()}</strong> jobs
          </span>
          <span>•</span>
          <span>
            <strong>{episodes.toLocaleString()}</strong> episodes
          </span>
          <span>•</span>
          <span>
            <strong>{findings.toLocaleString()}</strong> findings
          </span>
        </div>
      )}
      {error && <p className={styles.warn}>Status unavailable: {error.message}</p>}
    </div>
  );
}
