/**
 * EventsPanel — primary detection feed + shadow-only validator findings.
 *
 * Correlates each primary event with validator findings by scanning
 * the finding evidence arrays for `primary_event_id`:
 *   - finding with matching id      → "Disputed" (validator disagreed)
 *   - validator enabled + no finding for events older than grace window
 *                                   → "Verified"
 *   - otherwise                     → "Pending" (validator hasn't checked yet or is off)
 *
 * The "Clear all events" action lives on the Dashboard, not here — this
 * panel is purely informational on the Validation tab.
 */
import { useMemo, useState } from "react";

import { EventDialog } from "../../../shared/events";
import type {
  SafetyEvent,
  WatchdogFinding,
} from "../../../shared/types/common";
import {
  getShadowId,
  shadowFindingToEvent,
} from "../lib/shadowEvent";

import { ShadowDetailPanel } from "./ShadowDetailPanel";

import styles from "./EventsPanel.module.css";

const VERIFY_GRACE_MS = 5_000;

type Verdict = "verified" | "disputed" | "pending";

interface DisputeInfo {
  kind: string;
  primary?: string;
  secondary?: string;
}

interface PanelEvent {
  ev: SafetyEvent;
  verdict: Verdict;
  dispute?: DisputeInfo;
}

function humanize(value: string | undefined): string {
  if (!value) return "—";
  return value.replace(/_/g, " ");
}

function evidenceGet(f: WatchdogFinding, label: string): string | undefined {
  return f.evidence?.find((e) => e.label === label)?.value;
}

function parseDispute(f: WatchdogFinding): DisputeInfo {
  const fp = f.fingerprint ?? "";
  let kind = "disagreement";
  if (fp.endsWith("false-positive")) kind = "False positive";
  else if (fp.endsWith("classification-mismatch")) kind = "Class mismatch";
  else if (fp.endsWith("false-negative")) kind = "Missed detection";
  return {
    kind,
    primary: evidenceGet(f, "primary_label") ?? evidenceGet(f, "primary_risk"),
    secondary:
      evidenceGet(f, "secondary_label") ?? evidenceGet(f, "secondary_risk"),
  };
}

function formatTime(ts?: string): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

interface EventsPanelProps {
  events: SafetyEvent[];
  findings: WatchdogFinding[];
  validatorEnabled: boolean;
}

export function EventsPanel({
  events,
  findings,
  validatorEnabled,
}: EventsPanelProps) {
  const [openEvent, setOpenEvent] = useState<{
    ev: SafetyEvent;
    dispute?: DisputeInfo;
    shadowId?: string;
  } | null>(null);
  const [showLow, setShowLow] = useState(false);

  const visibleEvents = useMemo(
    () =>
      showLow ? events : events.filter((ev) => ev.risk_level !== "low"),
    [events, showLow],
  );
  const hiddenLowCount = events.length - visibleEvents.length;

  const validatorFindings = useMemo(
    () => findings.filter((f) => f.category === "validator"),
    [findings],
  );

  const disputesByEventId = useMemo(() => {
    const map = new Map<string, WatchdogFinding>();
    for (const f of validatorFindings) {
      const id = evidenceGet(f, "primary_event_id");
      if (id) map.set(id, f);
    }
    return map;
  }, [validatorFindings]);

  const panelEvents: PanelEvent[] = useMemo(() => {
    const now = Date.now();
    return visibleEvents.map((ev) => {
      const dispute = disputesByEventId.get(ev.event_id);
      if (dispute) {
        return { ev, verdict: "disputed" as const, dispute: parseDispute(dispute) };
      }
      const age = ev.wall_time ? now - new Date(ev.wall_time).getTime() : 0;
      const settled = age >= VERIFY_GRACE_MS;
      return {
        ev,
        verdict: validatorEnabled && settled ? ("verified" as const) : ("pending" as const),
      };
    });
  }, [visibleEvents, disputesByEventId, validatorEnabled]);

  const shadowOnly = useMemo(
    () =>
      validatorFindings.filter((f) =>
        (f.fingerprint ?? "").endsWith("false-negative"),
      ),
    [validatorFindings],
  );

  return (
    <div className={styles.wrap}>
      <section className={styles.section}>
        <header className={styles.sectionHead}>
          <div className={styles.titleBlock}>
            <h2>Detection events</h2>
            <p>
              Live events from the primary detector. Each row shows the
              validator's verdict — verified, disputed, or pending. Disputed
              rows expand with the secondary detector's reading.
            </p>
          </div>
          <label className={styles.showLow}>
            <input
              type="checkbox"
              checked={showLow}
              onChange={(e) => setShowLow(e.target.checked)}
            />
            Show low risk
            {hiddenLowCount > 0 && !showLow
              ? ` (${hiddenLowCount} hidden)`
              : ""}
          </label>
        </header>

        {panelEvents.length === 0 ? (
          <div className={styles.empty}>
            No events yet — they appear here as the primary detector emits them.
          </div>
        ) : (
          <div className={styles.list}>
            {panelEvents.map(({ ev, verdict, dispute }) => (
              <EventRow
                key={ev.event_id}
                ev={ev}
                verdict={verdict}
                dispute={dispute}
                onClick={() => setOpenEvent({ ev, dispute })}
              />
            ))}
          </div>
        )}
      </section>

      <section className={styles.section}>
        <header className={styles.sectionHead}>
          <div className={styles.titleBlock}>
            <h2>Shadow-only detections</h2>
            <p>
              Events the shadow validator flagged but the primary detector
              missed. Surface them to catch false negatives.
            </p>
          </div>
        </header>

        {shadowOnly.length === 0 ? (
          <div className={styles.empty}>
            {validatorEnabled
              ? "No shadow-only detections in the current window."
              : "Shadow validator is disabled — enable it above to surface misses."}
          </div>
        ) : (
          <div className={styles.list}>
            {shadowOnly.map((f) => {
              // ``shadow_id`` comes from the finding's evidence — the
              // validator stamps it on every false-negative so the
              // dialog can fetch the full ShadowRecord + analysis.
              // Findings emitted before the shadow-store wiring shipped
              // won't carry one — we fall back to a non-clickable row
              // so older rows still render without a broken dialog.
              const shadowId = getShadowId(f);
              const clickable = Boolean(shadowId);
              const onOpen = () =>
                setOpenEvent({
                  ev: shadowFindingToEvent(f),
                  shadowId,
                });
              return (
                <div
                  key={`${f.snapshot_id}_${f.ts}`}
                  className={`${styles.shadowRow} ${clickable ? styles.shadowRowClickable ?? "" : ""}`}
                  onClick={clickable ? onOpen : undefined}
                  role={clickable ? "button" : undefined}
                  tabIndex={clickable ? 0 : undefined}
                  onKeyDown={(e) => {
                    if (!clickable) return;
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onOpen();
                    }
                  }}
                >
                  <div className={styles.shadowTop}>
                    <span className={styles.shadowTitle}>{f.title}</span>
                    <span className={`${styles.badge} ${styles.badgeDisputed}`}>
                      Shadow flag
                    </span>
                  </div>
                  <div className={styles.shadowMeta}>
                    {formatTime(f.ts)} · {f.detail}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <EventDialog
        event={openEvent?.ev ?? null}
        disputeLabel={openEvent?.dispute?.kind}
        disputeBody={
          openEvent?.dispute
            ? openEvent.dispute.primary || openEvent.dispute.secondary
              ? `Primary said ${humanize(openEvent.dispute.primary)} · Secondary said ${humanize(openEvent.dispute.secondary)}`
              : "Secondary detector disagreed with the primary on this frame."
            : undefined
        }
        disableClip={Boolean(openEvent?.shadowId)}
        onClose={() => setOpenEvent(null)}
      >
        {openEvent?.shadowId ? (
          <ShadowDetailPanel
            shadowId={openEvent.shadowId}
            onPromoted={() => setOpenEvent(null)}
          />
        ) : null}
      </EventDialog>
    </div>
  );
}

interface EventRowProps {
  ev: SafetyEvent;
  verdict: Verdict;
  dispute?: DisputeInfo;
  onClick?: () => void;
}

function EventRow({ ev, verdict, dispute, onClick }: EventRowProps) {
  const risk = ev.risk_level;
  const objects =
    ev.objects?.slice(0, 3).map((o) => humanize(o)).join(" · ") ?? "";
  const conf =
    typeof ev.confidence === "number" ? `${Math.round(ev.confidence * 100)}%` : "—";

  const badgeClass =
    verdict === "verified"
      ? styles.badgeVerified
      : verdict === "disputed"
        ? styles.badgeDisputed
        : styles.badgePending;

  const badgeLabel =
    verdict === "verified" ? "✓ Verified" : verdict === "disputed" ? "⚠ Disputed" : "Pending";

  return (
    <div
      className={`${styles.row} ${styles[risk] ?? ""} ${styles.rowClickable ?? ""}`}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={(e) => {
        if (onClick && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          onClick();
        }
      }}
    >
      <span className={styles.time}>{formatTime(ev.wall_time)}</span>
      <div className={styles.mid}>
        <div className={styles.midTop}>
          <span className={styles.eventType}>{humanize(ev.event_type)}</span>
          <span className={`${styles.riskPill} ${styles[risk] ?? ""}`}>{risk}</span>
        </div>
        <div className={styles.meta}>
          {objects || "—"}
          {ev.vehicle_id ? ` · ${humanize(ev.vehicle_id)}` : ""}
          {typeof ev.ttc_sec === "number" ? ` · TTC ${ev.ttc_sec.toFixed(1)}s` : ""}
        </div>
      </div>
      <div className={styles.conf}>
        <span className={styles.confLabel}>Conf</span>
        <span className={styles.confValue}>{conf}</span>
      </div>
      <span className={`${styles.badge} ${badgeClass}`}>{badgeLabel}</span>

      {verdict === "disputed" && dispute && (
        <div className={styles.dispute}>
          <span className={styles.disputeLabel}>{dispute.kind}</span>
          <span className={styles.disputeDetail}>
            {dispute.primary || dispute.secondary
              ? `Primary said ${humanize(dispute.primary)} · Secondary said ${humanize(dispute.secondary)}`
              : "Secondary detector disagreed with the primary on this frame."}
          </span>
        </div>
      )}
    </div>
  );
}
