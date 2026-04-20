/**
 * useDetections — subscribes to `/admin/detections` SSE and exposes a
 * rolling window of recent detection frames plus aggregate stats + fps.
 *
 * Also tracks the latest authoritative MP4 playback position per source
 * (``playback_pos_sec``, ``playback_duration_sec``) and the wallclock
 * instant we received it. The map overlay reads this to drive its marker
 * directly off the video's actual playhead — so when the operator pauses
 * a stream, ``playback_pos_sec`` stops advancing and the marker freezes
 * with it. No 5 s polling drift.
 */
import { useState, useCallback, useRef } from "react";

import { useSSE } from "../../../shared/hooks/useSSE";
import type { DetectionSnapshot } from "../../../shared/types/common";

const MAX_FRAMES = 8;

/** Per-source playhead snapshot. */
export interface PlayheadSample {
  /** Authoritative MP4 position in seconds (``CAP_PROP_POS_MSEC / 1000``). */
  posSec: number;
  /** Total MP4 duration in seconds. */
  durationSec: number;
  /** Local wallclock (``performance.now()``) when this sample arrived.
   *  Used by the map hook to predict forward between SSE updates without
   *  drifting away from the server's authoritative clock. */
  receivedAtMs: number;
}

export function useDetections() {
  const [frames, setFrames] = useState<DetectionSnapshot[]>([]);
  const [stats, setStats] = useState({
    detections: 0,
    persons: 0,
    vehicles: 0,
    interactions: 0,
    fps: "—",
  });
  // Per-source latest playhead. We hold this in state (not a ref) so
  // consumers re-render when a new snapshot arrives. Each SSE message
  // overwrites only its own source key — slots stay independent.
  const [playheads, setPlayheads] = useState<Record<string, PlayheadSample>>({});

  const fpsCounterRef = useRef({ count: 0, start: Date.now() });

  const onMessage = useCallback((msg: DetectionSnapshot) => {
    const counter = fpsCounterRef.current;
    counter.count++;
    const now = Date.now();
    let fps = stats.fps;
    if (now - counter.start >= 3000) {
      fps = (counter.count / ((now - counter.start) / 1000)).toFixed(1);
      counter.count = 0;
      counter.start = now;
    }

    setStats({
      detections: msg.detections || 0,
      persons: msg.persons || 0,
      vehicles: msg.vehicles || 0,
      interactions: msg.interactions || 0,
      fps,
    });

    if (msg.objects?.length) {
      setFrames((prev) => {
        const next = [msg, ...prev];
        return next.length > MAX_FRAMES ? next.slice(0, MAX_FRAMES) : next;
      });
    }

    // Authoritative video playhead from the server. Recorded *every*
    // frame (even when ``objects`` is empty) so the map clock stays
    // fresh on quiet stretches of road too.
    if (msg.source_id && typeof msg.playback_pos_sec === "number") {
      const sample: PlayheadSample = {
        posSec: msg.playback_pos_sec,
        durationSec: msg.playback_duration_sec ?? 0,
        receivedAtMs: performance.now(),
      };
      setPlayheads((prev) => {
        const previous = prev[msg.source_id!];
        // Avoid useless re-renders when the server reports the exact
        // same value (paused stream → playhead frozen). We still
        // refresh ``receivedAtMs`` so consumers know the source is
        // alive, but only when the position actually moved or the
        // source was previously unknown.
        if (
          previous
          && previous.posSec === sample.posSec
          && previous.durationSec === sample.durationSec
        ) {
          return prev;
        }
        return { ...prev, [msg.source_id!]: sample };
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useSSE<DetectionSnapshot>({ url: "/admin/detections", onMessage });

  return { frames, stats, playheads };
}
