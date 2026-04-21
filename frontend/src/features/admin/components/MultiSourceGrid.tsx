/**
 * MultiSourceGrid — renders one tile per configured perception source.
 *
 * Each tile shows the live MJPEG feed for its source, the name + URL,
 * a running/paused dot, frames-processed counter, and a Start / Pause
 * button. The button delegates to `useLiveSources()` mutators which
 * POST to `/api/live/sources/{id}/start|pause` and refresh the list.
 *
 * When only the primary source is configured (legacy single-stream
 * deployment) the grid still renders fine — it just shows one tile.
 */
import { useEffect, useState } from "react";

import type { UseLiveSourcesResult } from "../hooks/useLiveSources";
import type { LiveSourceStatus } from "../../../shared/types/common";
import { humanize } from "../../../shared/lib/format";
import { useDialog } from "../../../shared/ui";

import styles from "./MultiSourceGrid.module.css";
import { StreamImage } from "./StreamImage";

// Short label + tone for the per-tile mode badge. Kept here (not in a
// shared util) because it's only used by the grid right now — inline a
// helper rather than invent a module for three strings.
function modeBadge(streamType: LiveSourceStatus["stream_type"]):
  | { label: string; tone: "demo" | "live" }
  | null {
  switch (streamType) {
    case "dashcam_file":
      return { label: "Dash Cam · Demo", tone: "demo" };
    case "live_hls":
      return { label: "Live · HLS", tone: "live" };
    case "webcam":
      return { label: "Webcam", tone: "live" };
    default:
      return null;
  }
}

function StreamTile({
  source,
  focused,
  minimized,
  onFocusToggle,
  onToggleDetection,
  onRemove,
}: {
  source: LiveSourceStatus;
  focused: boolean;
  minimized: boolean;
  onFocusToggle: () => void;
  onToggleDetection: (enabled: boolean) => void;
  onRemove: () => void;
}) {
  const [imgError, setImgError] = useState(false);
  const dialog = useDialog();
  const running = source.running;
  // Reset the error flag when the source restarts or swaps identity, so a
  // tile that briefly failed gets a chance to reconnect on the next start.
  useEffect(() => {
    setImgError(false);
  }, [source.id, running]);
  // Auto-recover from transient image-load failures. A single failed JPEG
  // poll (e.g. server briefly overloaded by 6 streams sharing one YOLO model)
  // would otherwise flip ``imgError`` permanently, unmount <StreamImage>,
  // and stop polling — meaning the tile never recovers until the operator
  // restarts the source. Retrying after 1.5s lets the next poll succeed and
  // restores the live feed without operator action.
  useEffect(() => {
    if (!imgError || !running) return;
    const id = window.setTimeout(() => setImgError(false), 1500);
    return () => window.clearTimeout(id);
  }, [imgError, running]);
  const detection = source.detection_enabled;

  const tileClass = [
    styles.tile,
    !detection ? styles.tileMuted : "",
    focused ? styles.tileFocused : "",
    minimized ? styles.tileMini : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={tileClass}>
      <div
        className={styles.videoWrap}
        role="button"
        tabIndex={0}
        aria-label={
          focused
            ? `Restore grid view (currently maximized: ${source.name})`
            : `Maximize ${source.name}`
        }
        aria-pressed={focused}
        onClick={onFocusToggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onFocusToggle();
          }
        }}
      >
        {source.started_at && !imgError ? (
          // Keep the <StreamImage> mounted across pause/resume so the
          // last-delivered frame stays on screen (frozen) instead of
          // snapping to a placeholder. The server keeps the reader alive
          // while paused, so the MJPEG buffer still holds the last frame.
          <>
            <StreamImage
              source={source}
              className={styles.video}
              onError={() => setImgError(true)}
            />
            {!running && !source.last_error && (
              <span className={styles.pausedBadge}>Paused</span>
            )}
          </>
        ) : (
          <div className={styles.placeholder}>
            {source.last_error
              ? `Error: ${source.last_error}`
              : running
                ? "Connecting…"
                : "Paused"}
          </div>
        )}
        <span className={`${styles.statusDot} ${running ? styles.dotRunning : styles.dotPaused}`} />
        {(() => {
          const badge = modeBadge(source.stream_type);
          if (!badge) return null;
          const toneClass =
            badge.tone === "demo" ? styles.modeBadgeDemo : styles.modeBadgeLive;
          return (
            <span className={`${styles.modeBadge} ${toneClass}`}>
              {badge.label}
            </span>
          );
        })()}
        {!detection && running && (
          <span className={styles.detectionBadge}>detection off</span>
        )}
        {focused && (
          <span className={styles.focusedBadge} aria-hidden="true">
            Tap to restore
          </span>
        )}
        <button
          type="button"
          className={styles.removeBtn}
          title={`Remove ${source.name}`}
          aria-label={`Remove ${source.name}`}
          onClick={async (e) => {
            e.stopPropagation();
            const ok = await dialog.confirm({
              title: "Remove stream",
              message: `Remove "${source.name}" from monitoring? This stops its perception loop and frees the slot.`,
              okLabel: "Remove",
              cancelLabel: "Cancel",
              variant: "danger",
            });
            if (ok) onRemove();
          }}
        >
          ×
        </button>
      </div>

      <div className={styles.meta}>
        <div className={styles.metaTop}>
          <strong className={styles.name}>{source.name}</strong>
        </div>
        <div className={styles.metaStats}>
          <span>{source.frames_processed.toLocaleString()} frames</span>
          <span>•</span>
          <span>{source.active_episodes} active</span>
          {source.perception_state && source.perception_state !== "nominal" && (
            <>
              <span>•</span>
              <span className={styles.warn}>{humanize(source.perception_state)}</span>
            </>
          )}
        </div>
      </div>

      <div className={styles.actions} onClick={(e) => e.stopPropagation()}>
        <label
          className={styles.detectionToggle}
          title="Toggle YOLO detection + event emission for this stream. Video preview keeps running either way."
        >
          <input
            type="checkbox"
            checked={detection}
            onChange={(e) => onToggleDetection(e.target.checked)}
          />
          <span>Detection</span>
        </label>
      </div>
    </div>
  );
}

interface MultiSourceGridProps {
  // Controlled state lives in AdminPage so the page header can also read
  // the focused source. Pass the full hook result here and the grid
  // delegates start/pause/etc. straight to it.
  liveSources: UseLiveSourcesResult;
  focusedId: string | null;
  onFocusChange: (id: string | null) => void;
  // Overrides the inline ``liveSources.restartAll`` call for the Restart
  // toolbar button. AdminPage uses this to pair the stream restart with
  // a client-side event-list wipe so replayed dashcam scenes don't re-fill
  // the feed with identical-looking events right after the operator cleared.
  onRestart?: () => Promise<void> | void;
}

export function MultiSourceGrid({
  liveSources,
  focusedId,
  onFocusChange,
  onRestart,
}: MultiSourceGridProps) {
  const {
    sources,
    loading,
    error,
    start,
    pause,
    setDetection,
    remove,
    busyById,
    restartAll,
    restartingAll,
  } = liveSources;

  // Esc exits focus mode — common expectation for "maximized" UI.
  useEffect(() => {
    if (!focusedId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onFocusChange(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focusedId, onFocusChange]);

  if (loading && sources.length === 0) {
    return <div className={styles.empty}>Loading sources…</div>;
  }
  if (error && sources.length === 0) {
    return <div className={styles.empty}>Failed to load sources: {error}</div>;
  }

  // Per-tile detection toggles live on each StreamTile; bulk select/clear
  // controls were removed from the toolbar. We still surface the live
  // enabled-count in the toolbar label for at-a-glance visibility.
  const enabledCount = sources.filter((s) => s.detection_enabled).length;

  // Shared "Start all" / "Pause all" controls. The per-tile start/pause
  // buttons still work for targeted control; these toolbar actions iterate
  // every source so the operator can bring the whole fleet online with one
  // click. The hook's optimistic update handles the running flag per-tile.
  const runningCount = sources.filter((s) => s.running).length;
  const allRunning = sources.length > 0 && runningCount === sources.length;
  const noneRunning = runningCount === 0;
  // Any start/pause mutation leaves a row in ``busyById`` until the server
  // confirms — while any row is in-flight we disable both bulk buttons to
  // avoid piling up requests on a slow backend.
  const anyBusy = Object.values(busyById).some((v) => v != null);
  const startAll = () => {
    sources.forEach((s) => {
      if (!s.running) start(s.id);
    });
  };
  const pauseAll = () => {
    sources.forEach((s) => {
      if (s.running) pause(s.id);
    });
  };

  const focusedSource = focusedId ? sources.find((s) => s.id === focusedId) ?? null : null;
  const minimizedSources = focusedSource ? sources.filter((s) => s.id !== focusedSource.id) : [];

  const renderTile = (s: LiveSourceStatus, opts: { focused: boolean; minimized: boolean }) => (
    <StreamTile
      key={s.id}
      source={s}
      focused={opts.focused}
      minimized={opts.minimized}
      onFocusToggle={() => onFocusChange(focusedId === s.id ? null : s.id)}
      onToggleDetection={(enabled) => setDetection(s.id, enabled)}
      onRemove={() => remove(s.id)}
    />
  );

  return (
    <div className={styles.gridWrap}>
      <div className={styles.toolbar}>
        <span className={styles.toolbarLabel}>
          Running: <strong>{runningCount}</strong> / {sources.length}
          {sources.length > 0 && (
            <>
              {" · "}
              Detection: <strong>{enabledCount}</strong> / {sources.length}
            </>
          )}
        </span>
        <div className={styles.toolbarActions}>
          <button
            type="button"
            className={`${styles.toolbarBtn} ${styles.toolbarBtnStart}`}
            onClick={startAll}
            disabled={allRunning || sources.length === 0 || anyBusy}
            title="Start every paused stream"
          >
            Start
          </button>
          <button
            type="button"
            className={styles.toolbarBtn}
            onClick={pauseAll}
            disabled={noneRunning || anyBusy}
            title="Pause every running stream"
          >
            Pause
          </button>
          <button
            type="button"
            className={styles.toolbarBtn}
            onClick={() => {
              // Prefer the parent-supplied handler (AdminPage wipes the
              // event list first so the replayed MP4 doesn't repopulate
              // it with identical-looking detections).
              if (onRestart) void onRestart();
              else void restartAll();
            }}
            disabled={sources.length === 0 || restartingAll}
            title="Restart every stream from the beginning and reset the map marker"
          >
            {restartingAll ? "Restarting…" : "Restart"}
          </button>
        </div>
      </div>
      {sources.length === 0 ? (
        <div className={styles.empty}>
          No streams yet. Set <code>ROAD_STREAM_SOURCES</code> in <code>.env</code>
          {" "}and restart the server.
        </div>
      ) : focusedSource ? (
        <div className={styles.focusedLayout}>
          <div className={styles.focusedSlot}>
            {renderTile(focusedSource, { focused: true, minimized: false })}
          </div>
          {minimizedSources.length > 0 && (
            <div className={styles.miniStrip}>
              {minimizedSources.map((s) =>
                renderTile(s, { focused: false, minimized: true }),
              )}
            </div>
          )}
        </div>
      ) : (
        <div className={styles.grid}>
          {sources.map((s) => renderTile(s, { focused: false, minimized: false }))}
        </div>
      )}
    </div>
  );
}
