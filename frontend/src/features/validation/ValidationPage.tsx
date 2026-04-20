/**
 * ValidationPage — the heavy shadow-mode dual-model validator tab.
 *
 * Split out of MonitoringPage so the watchdog incident queue stays
 * focused on "what's wrong with the system" and this tab stays focused
 * on "where do the two detectors disagree". Consumes the same
 * event-stream + watchdog context every other tab uses; no new
 * server calls live here.
 */
import { useMemo } from "react";

import { useEventStream } from "../../shared/hooks/useEventStream";
import { useLiveStatus } from "../../shared/hooks/useLiveStatus";
import { TopBar } from "../../shared/layout/TopBar";
import { useWatchdogCtx } from "../watchdog";

import { EventsPanel, ValidatorControl } from "./components";
import { useValidator } from "./hooks/useValidator";
import { useDriftCount } from "./hooks/useDriftCount";

import styles from "./ValidationPage.module.css";

export function ValidationPage() {
  const { connected, events } = useEventStream();
  const { data: liveStatus } = useLiveStatus();
  const { status: wdStatus, findings } = useWatchdogCtx();
  const { status: validatorStatus } = useValidator();

  const validatorActive =
    !!validatorStatus?.enabled && !validatorStatus?.paused;
  const driftCount = useDriftCount();
  const sourceName = liveStatus?.source ?? "—";

  const { disputed, shadowOnly } = useMemo(() => {
    const validator = (findings ?? []).filter((f) => f.category === "validator");
    return {
      disputed: validator.filter(
        (f) => !(f.fingerprint ?? "").endsWith("false-negative"),
      ).length,
      shadowOnly: validator.filter((f) =>
        (f.fingerprint ?? "").endsWith("false-negative"),
      ).length,
    };
  }, [findings]);

  return (
    <>
      <TopBar
        sourceName={sourceName}
        connected={connected}
        errorCount={wdStatus?.by_severity?.error ?? 0}
        driftCount={driftCount}
      />

      <div className={styles.page}>
        <div className={styles.header}>
          <div className={styles.titleRow}>
            <h1>Validation</h1>
            <p className={styles.subtitle}>
              A second, heavier detector runs in the background and
              cross-checks every primary finding. Disagreements and misses
              surface here so you can spot drift without gating live alerts.
            </p>
          </div>

          <div className={styles.statGrid}>
            <div className={styles.statCard}>
              <span className={styles.statLabel}>Disputed events</span>
              <span className={`${styles.statValue} ${styles.drift}`}>
                {disputed.toLocaleString()}
              </span>
              <span className={styles.statHint}>
                Primary detector's verdict the secondary disagrees with
                (false positive or class mismatch).
              </span>
            </div>
            <div className={styles.statCard}>
              <span className={styles.statLabel}>Shadow-only detections</span>
              <span className={`${styles.statValue} ${styles.drift}`}>
                {shadowOnly.toLocaleString()}
              </span>
              <span className={styles.statHint}>
                Events the shadow model flagged but the primary missed —
                candidate false negatives.
              </span>
            </div>
            <div className={styles.statCard}>
              <span className={styles.statLabel}>Drift total</span>
              <span className={`${styles.statValue} ${styles.drift}`}>
                {driftCount.toLocaleString()}
              </span>
              <span className={styles.statHint}>
                Combined drift count shown on the nav bubble.
              </span>
            </div>
          </div>

          <ValidatorControl />
        </div>

        <div className={styles.content}>
          <EventsPanel
            events={events}
            findings={findings ?? []}
            validatorEnabled={validatorActive}
          />
        </div>
      </div>
    </>
  );
}
