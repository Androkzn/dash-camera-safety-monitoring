/**
 * EventDialog — modal showing a ±3s looping clip + detailed analytics for
 * a single SafetyEvent.
 *
 * The clip is served by `GET /api/events/{id}/clip`; if the event's source
 * is a live stream (no seekable backing file), the backend 404s and we
 * fall back to showing the event thumbnail.
 */
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { humanize } from "../lib/format";
import type { SafetyEvent } from "../types/common";

import styles from "./EventDialog.module.css";

interface EventDialogProps {
  event: SafetyEvent | null;
  disputeLabel?: string;
  disputeBody?: string;
  onClose: () => void;
  /**
   * Skip the ``/api/events/{id}/clip`` fetch and render the thumbnail
   * fallback immediately. Shadow-only events (no seekable source) pass
   * this so the dialog doesn't fire a guaranteed-404 network request.
   */
  disableClip?: boolean;
  /**
   * Optional extra content rendered into the info-side body after the
   * built-in sections. Features compose shadow-specific panels here
   * (miss-reason diagnostic, re-run / promote actions) without
   * polluting the shared dialog with feature-level concerns.
   */
  children?: ReactNode;
}

function fmtNum(v: number | undefined | null, unit = "", digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v.toFixed(digits)}${unit}`;
}

function fmtPct(v: number | undefined | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${Math.round(v * 100)}%`;
}

function fmtTime(ts?: string): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

export function EventDialog({
  event,
  disputeLabel,
  disputeBody,
  onClose,
  disableClip = false,
  children,
}: EventDialogProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [clipFailed, setClipFailed] = useState(false);

  useEffect(() => {
    setClipFailed(false);
  }, [event?.event_id]);

  // Esc to close.
  useEffect(() => {
    if (!event) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [event, onClose]);

  const clipUrl = useMemo(
    () =>
      event && !disableClip
        ? `/api/events/${encodeURIComponent(event.event_id)}/clip?before=3&after=3`
        : null,
    [event, disableClip],
  );

  if (!event) return null;

  const risk = event.risk_level;
  const objects = event.objects?.map(humanize).join(", ") || "—";

  return (
    <div
      className={styles.backdrop}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className={styles.panel} role="dialog" aria-modal="true">
        <div className={styles.videoSide}>
          <div className={styles.videoWrap}>
            {!clipFailed && clipUrl ? (
              <video
                ref={videoRef}
                src={clipUrl}
                autoPlay
                loop
                muted
                controls
                playsInline
                onError={() => setClipFailed(true)}
              />
            ) : (
              <div className={styles.videoFallback}>
                {event.thumbnail ? (
                  <img src={`/${event.thumbnail}`} alt="event thumbnail" />
                ) : null}
                <span>
                  No seekable clip available — the source is a live stream
                  or the file is not accessible server-side.
                </span>
              </div>
            )}
          </div>
          <div className={styles.videoMeta}>
            <span>
              {humanize(event.source_name) || humanize(event.source_id) || "—"}
              {typeof event.timestamp_sec === "number"
                ? ` · @ ${event.timestamp_sec.toFixed(1)}s`
                : ""}
            </span>
            <span className={styles.loopHint}>↻ Loop · ±3s around event</span>
          </div>
        </div>

        <div className={styles.infoSide}>
          <header className={styles.header}>
            <div className={styles.titleBlock}>
              <h2>{humanize(event.event_type)}</h2>
              <div className={styles.subtitle}>
                {fmtTime(event.wall_time)}
                {event.vehicle_id ? ` · ${humanize(event.vehicle_id)}` : ""}
              </div>
            </div>
            <button
              type="button"
              className={styles.closeBtn}
              onClick={onClose}
              aria-label="Close"
              title="Close (Esc)"
            >
              ×
            </button>
          </header>

          <div className={styles.body}>
            <div className={styles.pillRow}>
              <span className={`${styles.pill} ${styles[risk] ?? ""}`}>{risk} risk</span>
              {event.risk_demoted && <span className={styles.pill}>demoted</span>}
              {event.peak_risk_level && event.peak_risk_level !== risk && (
                <span className={styles.pill}>peak {event.peak_risk_level}</span>
              )}
              {event.scene_context?.label && (
                <span className={styles.pill}>{humanize(event.scene_context.label)}</span>
              )}
              {event.perception_state && event.perception_state !== "nominal" && (
                <span className={styles.pill}>{humanize(event.perception_state)}</span>
              )}
            </div>

            {(disputeLabel || disputeBody) && (
              <div className={styles.dispute}>
                {disputeLabel && <span className={styles.disputeLabel}>{disputeLabel}</span>}
                {disputeBody && <span className={styles.disputeBody}>{disputeBody}</span>}
              </div>
            )}

            {event.narration && (
              <div>
                <div className={styles.sectionLabel}>Narration</div>
                <div className={styles.narrate}>{event.narration}</div>
              </div>
            )}

            {event.summary && (
              <div>
                <div className={styles.sectionLabel}>Summary</div>
                <div className={styles.summary}>{event.summary}</div>
              </div>
            )}

            <div>
              <div className={styles.sectionLabel}>Detected params</div>
              <div className={styles.grid}>
                <Cell label="Objects" value={objects} />
                <Cell label="Confidence" value={fmtPct(event.confidence)} />
                <Cell label="TTC" value={fmtNum(event.ttc_sec, " s", 1)} />
                <Cell label="Distance" value={fmtNum(event.distance_m, " m", 1)} />
                <Cell
                  label="Distance (px)"
                  value={typeof event.distance_px === "number" ? `${event.distance_px.toFixed(0)} px` : "—"}
                />
                <Cell
                  label="Episode"
                  value={fmtNum(event.episode_duration_sec, " s", 2)}
                />
                <Cell
                  label="Track IDs"
                  value={event.track_ids?.length ? event.track_ids.join(" · ") : "—"}
                />
                <Cell label="Event ID" value={event.event_id} />
              </div>
            </div>

            {(event.scene_context || event.ego_flow) && (
              <div>
                <div className={styles.sectionLabel}>Scene + ego motion</div>
                <div className={styles.grid}>
                  {event.scene_context && (
                    <>
                      <Cell
                        label="Scene"
                        value={`${humanize(event.scene_context.label)} (${fmtPct(event.scene_context.confidence)})`}
                      />
                      <Cell
                        label="Scene reason"
                        value={event.scene_context.reason || "—"}
                      />
                    </>
                  )}
                  {event.ego_flow && (
                    <>
                      <Cell
                        label="Ego speed proxy"
                        value={fmtNum(event.ego_flow.speed_proxy_mps, " m/s", 2)}
                      />
                      <Cell
                        label="Ego flow conf"
                        value={fmtPct(event.ego_flow.confidence)}
                      />
                    </>
                  )}
                </div>
              </div>
            )}

            {event.enrichment_skipped && (
              <div className={styles.summary}>
                Enrichment skipped: {humanize(event.enrichment_skipped)}
              </div>
            )}

            {children}
          </div>
        </div>
      </div>
    </div>
  );
}

function Cell({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.cell}>
      <span className={styles.cellLabel}>{label}</span>
      <span className={styles.cellValue} title={value}>
        {value}
      </span>
    </div>
  );
}
