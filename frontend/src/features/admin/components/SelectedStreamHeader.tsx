/**
 * SelectedStreamHeader — top-of-page summary for the currently focused
 * (or, when nothing is focused, the primary) perception stream.
 *
 * Sits between TopBar and HealthStrip on the Admin page. Lets operators
 * see the chosen camera's identity, running state, throughput, and
 * perception health without having to scan the tile grid.
 */
import { useEffect, useState } from "react";

import type { LiveSourceStatus } from "../../../shared/types/common";
import { humanize } from "../../../shared/lib/format";

import styles from "./SelectedStreamHeader.module.css";

function shortHost(url: string): string {
  if (!url) return "—";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.slice(0, 48);
  }
}

function formatUptime(secs: number): string {
  if (!Number.isFinite(secs) || secs <= 0) return "0s";
  const s = Math.floor(secs % 60);
  const m = Math.floor((secs / 60) % 60);
  const h = Math.floor(secs / 3600);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

interface SelectedStreamHeaderProps {
  source: LiveSourceStatus | null;
  // True when the source was chosen by an explicit tap; false when we
  // fell back to the primary. Drives the "focused" badge.
  isFocused: boolean;
  // Total configured sources — shown next to the name as context.
  totalSources: number;
  onClear?: () => void;
}

export function SelectedStreamHeader({
  source,
  isFocused,
  totalSources,
  onClear,
}: SelectedStreamHeaderProps) {
  // Live-ticking uptime so the operator gets a heartbeat even when the
  // backend hasn't pushed a fresh snapshot in the last few seconds.
  const startedAt = source?.started_at ?? null;
  const [now, setNow] = useState<number>(() => Date.now() / 1000);
  useEffect(() => {
    if (!startedAt || !source?.running) return;
    const tick = () => setNow(Date.now() / 1000);
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startedAt, source?.running]);

  if (!source) {
    return (
      <div className={styles.bar}>
        <div className={styles.empty}>No streams configured</div>
      </div>
    );
  }

  const uptime = source.running && startedAt ? now - startedAt : source.uptime_sec;
  const perceptionRaw = source.perception_state ?? "—";
  const perceptionWarn = perceptionRaw !== "nominal" && perceptionRaw !== "—";
  const perception = perceptionRaw === "—" ? "—" : humanize(perceptionRaw);

  return (
    <div className={styles.bar}>
      <div className={styles.identity}>
        <span
          className={`${styles.dot} ${source.running ? styles.dotRunning : styles.dotPaused}`}
          aria-hidden
        />
        <div className={styles.nameWrap}>
          <div className={styles.nameRow}>
            <strong className={styles.name} title={source.name}>
              {source.name}
            </strong>
            {isFocused ? (
              <span className={styles.focusBadge}>focused</span>
            ) : (
              <span className={styles.primaryBadge}>primary</span>
            )}
            {!source.detection_enabled && (
              <span className={styles.detectionOff}>detection off</span>
            )}
          </div>
          <div className={styles.host} title={source.url}>
            {shortHost(source.url)} · {totalSources} stream{totalSources === 1 ? "" : "s"} total
          </div>
        </div>
      </div>

      <div className={styles.stats}>
        <Stat label="status" value={source.running ? "running" : "paused"} />
        <Stat label="uptime" value={formatUptime(uptime)} />
        <Stat
          label="frames"
          value={`${source.frames_processed.toLocaleString()} / ${source.frames_read.toLocaleString()}`}
          hint="processed / read"
        />
        <Stat label="active" value={String(source.active_episodes)} hint="open episodes" />
        <Stat
          label="perception"
          value={perception}
          warn={perceptionWarn}
          hint={source.perception_reason ?? undefined}
        />
      </div>

      {isFocused && onClear && (
        <button
          type="button"
          className={styles.clearBtn}
          onClick={onClear}
          title="Restore the multi-stream grid (Esc)"
        >
          Restore grid
        </button>
      )}

      {source.last_error && (
        <div className={styles.error} title={source.last_error}>
          {source.last_error}
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  warn,
}: {
  label: string;
  value: string;
  hint?: string;
  warn?: boolean;
}) {
  return (
    <div className={styles.stat} title={hint}>
      <span className={styles.statLabel}>{label}</span>
      <span className={`${styles.statValue} ${warn ? styles.warn : ""}`}>{value}</span>
    </div>
  );
}
