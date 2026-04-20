/**
 * IncidentCard — full per-incident article block.
 *
 * Includes severity icon, title + meta-pills, "Next move" CTA, three
 * Observed/Impact/Likely-cause cards, and optional Evidence / Steps /
 * Debug-commands / Runbook sections.
 */
import { SEV_ICON, formatRelative, formatTimestamp } from "../utils/formatting";
import type { WatchdogIncident } from "../types";

import styles from "../MonitoringPage.module.css";

function getEvidenceClass(status?: string): string {
  if (status === "breach") return styles.evidenceBreach ?? "";
  if (status === "trend") return styles.evidenceTrend ?? "";
  return styles.evidenceContext ?? "";
}

interface IncidentCardProps {
  incident: WatchdogIncident;
  selectMode: boolean;
  isSelected: boolean;
  onToggleSelect: (id: string) => void;
  onDelete: (rawKeys: string[]) => void;
}

export function IncidentCard({
  incident,
  selectMode,
  isSelected,
  onToggleSelect,
  onDelete,
}: IncidentCardProps) {
  const latest = incident.latest;
  const cls = [
    styles.incidentCard,
    styles[incident.severity],
    selectMode ? styles.selectable : "",
    isSelected ? styles.selected : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <article
      id={`incident-${incident.id}`}
      className={cls}
      onClick={selectMode ? () => onToggleSelect(incident.id) : undefined}
    >
      <div className={styles.incidentHeader}>
        <div className={styles.incidentHeaderLeft}>
          {selectMode && (
            <span className={`${styles.checkbox} ${isSelected ? styles.checked : ""}`}>
              {isSelected ? "✓" : ""}
            </span>
          )}
          <span className={`${styles.sevIcon} ${styles[incident.severity]}`}>
            {SEV_ICON[incident.severity] ?? "?"}
          </span>
          <div className={styles.incidentTitleBlock}>
            <div className={styles.incidentTitleRow}>
              <h2 className={styles.incidentTitle}>{incident.title}</h2>
              <div className={styles.metaPills}>
                <span className={styles.pill}>{incident.category}</span>
                {incident.owner && <span className={styles.pill}>{incident.owner}</span>}
                {incident.count > 1 && (
                  <span className={`${styles.pill} ${styles.repeatPill}`}>
                    Seen {incident.count}x
                  </span>
                )}
                {latest.source === "ai" && (
                  <span className={`${styles.pill} ${styles.aiPill}`}>AI hypothesis</span>
                )}
              </div>
            </div>
            <div className={styles.incidentTimeline}>
              <span>First seen {formatTimestamp(incident.firstSeen)}</span>
              <span>
                Last seen {formatTimestamp(incident.lastSeen)} (
                {formatRelative(incident.lastSeen)})
              </span>
            </div>
          </div>
        </div>
        {!selectMode && (
          <button
            className={styles.deleteSingle}
            onClick={(e) => {
              e.stopPropagation();
              onDelete(incident.rawKeys);
            }}
            title="Delete this incident group"
          >
            &times;
          </button>
        )}
      </div>

      <div className={styles.nextStepBox}>
        <span className={styles.nextStepLabel}>Next move</span>
        <strong>
          {latest.suggestion || "Inspect the evidence attached to this incident."}
        </strong>
      </div>

      <div className={styles.summaryPanel}>
        <div className={styles.summaryCard}>
          <span className={styles.summaryLabel}>Observed</span>
          <p>{latest.detail}</p>
        </div>
        <div className={styles.summaryCard}>
          <span className={styles.summaryLabel}>Impact</span>
          <p>{latest.impact || "Impact not provided for this incident yet."}</p>
        </div>
        <div className={styles.summaryCard}>
          <span className={styles.summaryLabel}>
            Likely Cause
            {latest.cause_confidence === "inferred" ? " (inferred)" : ""}
          </span>
          <p>{latest.likely_cause || "No likely cause attached yet."}</p>
        </div>
      </div>

      {latest.evidence && latest.evidence.length > 0 && (
        <div className={styles.sectionBlock}>
          <div className={styles.blockLabel}>Evidence</div>
          <div className={styles.evidenceGrid}>
            {latest.evidence.map((item, index) => (
              <div
                className={`${styles.evidenceChip} ${getEvidenceClass(item.status)}`}
                key={`${incident.id}-evidence-${index}`}
              >
                <span className={styles.evidenceLabel}>{item.label}</span>
                <strong className={styles.evidenceValue}>{item.value}</strong>
                {item.threshold && (
                  <span className={styles.evidenceThreshold}>Target {item.threshold}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {latest.investigation_steps && latest.investigation_steps.length > 0 && (
        <div className={styles.sectionBlock}>
          <div className={styles.blockLabel}>What To Check</div>
          <ol className={styles.stepsList}>
            {latest.investigation_steps.map((step, index) => (
              <li key={`${incident.id}-step-${index}`}>{step}</li>
            ))}
          </ol>
        </div>
      )}

      {latest.debug_commands && latest.debug_commands.length > 0 && (
        <div className={styles.sectionBlock}>
          <div className={styles.blockLabel}>Fast Debug Paths</div>
          <div className={styles.commandsList}>
            {latest.debug_commands.map((command, index) => (
              <code className={styles.commandChip} key={`${incident.id}-cmd-${index}`}>
                {command}
              </code>
            ))}
          </div>
        </div>
      )}

      {latest.runbook && (
        <div className={styles.runbook}>Playbook: {latest.runbook}</div>
      )}
    </article>
  );
}
