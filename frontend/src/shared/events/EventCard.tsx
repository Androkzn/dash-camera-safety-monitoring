/**
 * EventCard — public-facing card for one SafetyEvent. Larger and richer
 * than AdminEventCard: shows enrichment row (vehicle color/type, plate
 * hash), and embeds FeedbackButtons so viewers can mark the detection
 * correct or wrong.
 */

import { useState, useEffect } from "react";

import {
  formatWallTime,
  humanEventType,
  formatConfidence,
  normalizeThumbnail,
} from "../lib/format";
import { RiskBadge, Tag } from "../ui";
import type { SafetyEvent } from "../types/common";

import { FeedbackButtons } from "./FeedbackButtons";
import styles from "./EventCard.module.css";

interface EventCardProps {
  event: SafetyEvent;
  isNew?: boolean;
  // When supplied, the card becomes a button that opens the parent's
  // event-detail dialog. Optional so callers that just want a static
  // card (reports, embed contexts) keep their existing behaviour.
  onSelect?: (event: SafetyEvent) => void;
}

export function EventCard({ event: e, isNew, onSelect }: EventCardProps) {
  const [flash, setFlash] = useState(isNew);

  useEffect(() => {
    if (!isNew) return;
    const t = setTimeout(() => setFlash(false), 1500);
    return () => clearTimeout(t);
  }, [isNew]);

  const thumb = normalizeThumbnail(e.thumbnail);
  const objs = e.objects?.length ? e.objects.join(" · ") : "—";
  const enr = e.enrichment;
  const interactive = typeof onSelect === "function";

  // Friendly copy for per-event skip reasons. The backend only stamps
  // per-event reasons here (policy-level skipping is a deployment
  // property, not an event signal — see _emit_event in server.py). The
  // legacy ``alpr_policy_disabled`` value is mapped to undefined so old
  // events in the rolling buffer don't surface a noisy banner.
  const skipLabel: Record<string, string> = {
    perception_degraded: "ALPR skipped — image quality too low",
    low_risk_event: "ALPR skipped — low-risk (batch review)",
  };
  const skipNote = e.enrichment_skipped
    ? skipLabel[e.enrichment_skipped]
    : undefined;

  return (
    <div
      className={`${styles.card} ${flash ? styles.flash : ""} ${interactive ? styles.interactive ?? "" : ""}`.trim()}
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={interactive ? () => onSelect?.(e) : undefined}
      onKeyDown={
        interactive
          ? (ke) => {
              if (ke.key === "Enter" || ke.key === " ") {
                ke.preventDefault();
                onSelect?.(e);
              }
            }
          : undefined
      }
      aria-label={interactive ? `Open details for ${humanEventType(e.event_type)}` : undefined}
    >
      <div className={styles.thumb}>
        {thumb ? (
          <img
            src={thumb}
            alt=""
            onError={(ev) => {
              (ev.target as HTMLImageElement).style.display = "none";
              (ev.target as HTMLImageElement).parentElement!.textContent = "no preview";
            }}
          />
        ) : (
          <span>no preview</span>
        )}
      </div>
      <div className={styles.body}>
        <div className={styles.row1}>
          <RiskBadge level={e.risk_level} />
          <span className={styles.etype}>{humanEventType(e.event_type)}</span>
          <span className={styles.meta}>
            <span>{formatWallTime(e.wall_time)}</span>
            <span className={styles.sep}>•</span>
            <span>
              {e.timestamp_sec != null ? `T+${Number(e.timestamp_sec).toFixed(1)}s` : ""}
            </span>
          </span>
        </div>

        <div className={styles.meta}>
          <span>{objs}</span>
          <span className={styles.sep}>•</span>
          <span>conf {formatConfidence(e.confidence)}</span>
        </div>

        {(e.ttc_sec != null || e.distance_m != null || e.distance_px != null) && (
          <div className={styles.meta}>
            {e.ttc_sec != null && (
              <Tag variant={e.ttc_sec <= 1.5 ? "kin-warn" : "kin"} title="time-to-collision">
                TTC {Number(e.ttc_sec).toFixed(1)}s
              </Tag>
            )}
            {e.distance_m != null && (
              <Tag variant="kin" title="distance">
                {Number(e.distance_m).toFixed(1)}m
              </Tag>
            )}
            {e.distance_px != null && <Tag>{Math.round(e.distance_px)}px</Tag>}
          </div>
        )}

        {e.narration ? (
          <div className={styles.narr}>{e.narration}</div>
        ) : e.summary ? (
          <div className={styles.summ}>{e.summary}</div>
        ) : null}

        {skipNote && <div className={styles.skipNote}>{skipNote}</div>}

        <div className={styles.row3}>
          {e.track_ids?.length ? (
            <Tag variant="track">#{e.track_ids.join(" / #")}</Tag>
          ) : null}
          {e.episode_duration_sec != null && (
            <Tag>ep {Number(e.episode_duration_sec).toFixed(1)}s</Tag>
          )}
          {/* Privacy invariant: only the SALTED HASH ever reaches the
              frontend. Raw plate text is scrubbed in enrich_event(). */}
          {enr?.plate_hash && (
            <Tag variant="hash" title="salted plate hash">
              {enr.plate_hash}
            </Tag>
          )}
          {!enr?.plate_hash && enr?.readability && (
            <Tag variant="muted">plate {enr.readability}</Tag>
          )}
          {(enr?.vehicle_color || enr?.vehicle_type) && (
            <Tag>
              {[enr.vehicle_color, enr.vehicle_type].filter(Boolean).join(" ")}
            </Tag>
          )}
        </div>

        <div className={styles.row3}>
          <Tag>{e.event_id || ""}</Tag>
          <Tag>{e.video_id || ""}</Tag>
        </div>

        {/* stopPropagation keeps thumbs-up/down from bubbling into the
            card-level click handler that opens the event-detail dialog. */}
        <div onClick={(ev) => ev.stopPropagation()} onKeyDown={(ev) => ev.stopPropagation()}>
          <FeedbackButtons eventId={e.event_id} />
        </div>
      </div>
    </div>
  );
}
