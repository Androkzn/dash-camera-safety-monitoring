/**
 * AdminPage — live detection feed orchestrator.
 *
 * Owns:
 *   - SSE connection (events + perception state) via useEventStream
 *   - Per-frame detection stats via useDetections (SSE)
 *   - Admin health snapshot via useAdminHealth (TanStack Query)
 *   - Multi-source lifecycle via useLiveSources (TanStack Query)
 *   - Local UI state for the focused source
 *
 * Composition: TopBar → SelectedStreamHeader → optional HealthStrip →
 *              MultiSourceGrid + Tabs(Detections | Events | History).
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { useEventStream } from "../../shared/hooks/useEventStream";
import { TopBar } from "../../shared/layout/TopBar";
import { Pill, Tabs } from "../../shared/ui";
import { formatUptime } from "../../shared/lib/format";
import { useDriftCount } from "../validation";
import { useWatchdogCtx } from "../watchdog";

import {
  AdminEventCard,
  DetectionsPanel,
  HealthStrip,
  HistoryPanel,
  MultiSourceGrid,
  SelectedStreamHeader,
  VehicleMap,
} from "./components";
import { useAdminHealth } from "./hooks/useAdminHealth";
import { useDetections } from "./hooks/useDetections";
import { useLiveSources } from "./hooks/useLiveSources";

import styles from "./AdminPage.module.css";

export function AdminPage() {
  const { data: health } = useAdminHealth();
  const { frames, playheads } = useDetections();
  const { events: liveEvents, connected, clearEvents } = useEventStream();
  const liveSources = useLiveSources(5000);
  const { status: wdStatus } = useWatchdogCtx();
  const driftCount = useDriftCount();

  // Restart flow: wipe the admin event list BEFORE kicking the streams so
  // the replayed MP4 produces a fresh timeline of detections instead of
  // piling new-but-identical events on top of the old ones. Without this,
  // operators perceive cleared events as "coming back" because the dashcam
  // loop replays the same scenes and emits the same event categories.
  const handleRestart = useCallback(async () => {
    clearEvents();
    await liveSources.restartAll();
  }, [clearEvents, liveSources]);

  const [focusedId, setFocusedId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return window.localStorage.getItem("road_admin_focused_id");
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (focusedId) window.localStorage.setItem("road_admin_focused_id", focusedId);
    else window.localStorage.removeItem("road_admin_focused_id");
    window.dispatchEvent(new CustomEvent("admin-focused-id-changed"));
  }, [focusedId]);

  // Drop focus if the focused source disappeared (operator removed it).
  useEffect(() => {
    if (focusedId && !liveSources.sources.some((s) => s.id === focusedId)) {
      setFocusedId(null);
    }
  }, [focusedId, liveSources.sources]);

  const selectedSource = useMemo(() => {
    const list = liveSources.sources;
    if (list.length === 0) return null;
    if (focusedId) {
      const hit = list.find((s) => s.id === focusedId);
      if (hit) return hit;
    }
    if (liveSources.primaryId) {
      const primary = list.find((s) => s.id === liveSources.primaryId);
      if (primary) return primary;
    }
    return list[0] ?? null;
  }, [focusedId, liveSources.primaryId, liveSources.sources]);

  const startedAt = health?.server.started_at ?? null;
  const [uptimeSec, setUptimeSec] = useState<number | null>(null);
  useEffect(() => {
    if (!startedAt) return;
    const tick = () => setUptimeSec(Date.now() / 1000 - startedAt);
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startedAt]);

  const [showLowEvents, setShowLowEvents] = useState(false);
  const visibleEvents = useMemo(
    () =>
      showLowEvents
        ? liveEvents
        : liveEvents.filter((ev) => ev.risk_level !== "low"),
    [liveEvents, showLowEvents],
  );
  const hiddenLowCount = liveEvents.length - visibleEvents.length;
  const evtCount = visibleEvents.length;
  const isDashcam = selectedSource?.stream_type === "dashcam_file";

  // Pick the stream the map should follow. Preference order:
  //   1. The focused source if it's a running dashcam_file.
  //   2. Any running dashcam_file.
  //   3. The primary source (even if paused, so the badge is still useful).
  // This lets the map sync to whichever camera is actually producing frames
  // when the primary is a placeholder / paused.
  const mapClockSource = useMemo(() => {
    const list = liveSources.sources;
    if (list.length === 0) return null;
    const focused = focusedId ? list.find((s) => s.id === focusedId) : null;
    if (focused?.stream_type === "dashcam_file" && focused.running) {
      return focused;
    }
    const runningDash = list.find(
      (s) => s.stream_type === "dashcam_file" && s.running,
    );
    if (runningDash) return runningDash;
    return selectedSource;
  }, [liveSources.sources, focusedId, selectedSource]);

  return (
    <>
      <TopBar
        sourceName={health?.server.source ?? "—"}
        connected={connected}
        errorCount={wdStatus?.by_severity?.error ?? 0}
        driftCount={driftCount}
      >
        <Pill style={{ marginLeft: 8 }}>
          uptime{" "}
          <strong style={{ marginLeft: 4 }}>{formatUptime(uptimeSec)}</strong>
        </Pill>
      </TopBar>

      <SelectedStreamHeader
        source={selectedSource}
        isFocused={!!focusedId && selectedSource?.id === focusedId}
        totalSources={liveSources.sources.length}
        onClear={() => setFocusedId(null)}
      />

      {focusedId && <HealthStrip health={health ?? null} />}

      <div className={styles.main}>
        <div className={styles.leftCol}>
          <MultiSourceGrid
            liveSources={liveSources}
            focusedId={focusedId}
            onFocusChange={setFocusedId}
            onRestart={handleRestart}
          />
          {isDashcam && mapClockSource && (
            <div className={styles.mapSlot}>
              <VehicleMap
                videoKey="front"
                clock={{
                  // ``uptimeSec`` is the wallclock-since-start fallback;
                  // useful only when the per-frame SSE playhead hasn't
                  // arrived yet (first ~500 ms after a stream starts).
                  uptimeSec: mapClockSource.uptime_sec,
                  running: mapClockSource.running,
                  videoDurationSec: null,
                  resetToken: liveSources.restartAllToken,
                  // Authoritative playhead pushed every frame over SSE.
                  // When the operator pauses the stream the value stops
                  // advancing → the marker freezes; when the MP4 loops,
                  // the value resets → the marker snaps back to the
                  // start of the GPS track. Tight, no 5 s polling drift.
                  videoPosSec:
                    playheads[mapClockSource.id]?.posSec ?? null,
                  videoPosReceivedAtMs:
                    playheads[mapClockSource.id]?.receivedAtMs ?? null,
                }}
              />
            </div>
          )}
        </div>

        <div className={styles.sidebar}>
          <Tabs
            defaultTab="events"
            tabs={[
              {
                id: "events",
                label: (
                  <>
                    Events{" "}
                    {evtCount > 0 && (
                      <span style={{ fontSize: "10px", color: "var(--muted)" }}>
                        ({evtCount})
                      </span>
                    )}
                  </>
                ),
                content: (
                  <>
                    <div className={styles.evtControls}>
                      <label>
                        <input
                          type="checkbox"
                          checked={showLowEvents}
                          onChange={(e) => setShowLowEvents(e.target.checked)}
                        />
                        Show low risk
                        {hiddenLowCount > 0 && !showLowEvents
                          ? ` (${hiddenLowCount} hidden)`
                          : ""}
                      </label>
                    </div>
                    <div className={styles.evtList}>
                      {visibleEvents.length === 0 ? (
                        <div className={styles.empty}>
                          {liveEvents.length === 0
                            ? "No events yet — they appear here in real time"
                            : "No events match the current filter"}
                        </div>
                      ) : (
                        visibleEvents.map((ev) => (
                          <AdminEventCard key={ev.event_id} event={ev} />
                        ))
                      )}
                    </div>
                  </>
                ),
              },
              {
                id: "detections",
                label: "Detections",
                content: <DetectionsPanel frames={frames} />,
              },
              {
                id: "history",
                label: "History",
                content: <HistoryPanel />,
              },
            ]}
          />
        </div>
      </div>
    </>
  );
}
