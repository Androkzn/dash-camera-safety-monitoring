/**
 * VideoFeed — the live annotated MJPEG feed from the edge server, with
 * an overlay of big-number counters (detections, persons, vehicles,
 * interactions, fps).
 *
 * Where it renders:
 *   The hero block of pages/AdminPage.tsx. The stats come from the
 *   page's SSE-driven state (see hooks/useLiveStream.ts) and are
 *   piped in as a single `stats` prop.
 *
 * Props:
 *   - stats: VideoOverlayStats  (required)
 *       Small bundle of the latest counters to display on top of the
 *       video. Re-rendered whenever AdminPage receives a new frame
 *       snapshot from SSE.
 *
 * Visual region:
 *   A rectangular video container with:
 *     - the <img> serving MJPEG at /admin/video_feed (the browser
 *       keeps the HTTP connection open and refreshes the frame as
 *       multipart parts arrive — no <video> tag needed)
 *     - a loading placeholder while the first frame hasn't loaded
 *     - an error placeholder if the feed fails
 *     - an overlay row of five stat tiles
 *
 * React concepts demonstrated in this file:
 *   - `useState` for two independent booleans (`loaded`, `error`)
 *   - DOM-native image load events: `onLoad` / `onError`
 *   - Conditional rendering with `&&` chains
 *   - Mixing multiple CSS-module classes on one element
 *
 * NOTE: We're using <img>, not a React ref, because the server emits
 * MJPEG (multipart/x-mixed-replace). An <img> element decodes it for
 * free. If we ever switch to HLS/WebRTC we'll need a <video ref={...}>
 * and a `useRef`/`useEffect` pair to wire up the player.
 */

// TEACH: `useState` — see full explainer in TabBar.tsx. Each call
//        creates an independent slot of component state.
import { useState } from "react";
import styles from "./VideoFeed.module.css";

// --- Types ---

// TEACH: Grouping overlay stats into a single object prop (rather than
//        5 separate props) makes the call site terser and keeps the
//        "one concept per prop" rule intact.
interface VideoOverlayStats {
  detections: number;
  persons: number;
  vehicles: number;
  interactions: number;
  // TEACH: `fps` is a pre-formatted string (e.g. "2.0") because the
  //        parent already rounds it; keeps formatting logic in one place.
  fps: string;
}

interface VideoFeedProps {
  stats: VideoOverlayStats;
}

// --- Render ---

export function VideoFeed({ stats }: VideoFeedProps) {
  // --- State ---

  // TEACH: Two independent pieces of local UI state. Using `useState`
  //        twice is idiomatic — don't try to merge unrelated booleans
  //        into one object unless they change together.
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);

  return (
    <div className={styles.videoCol}>
      <div className={styles.videoWrap}>
        {/* TEACH: An MJPEG feed embedded as a plain <img>. The browser
             keeps the connection open; each multipart part replaces
             the displayed frame. React treats this like any other img. */}
        <img
          className={styles.feedImg}
          src="/admin/video_feed"
          alt="Live detection feed"
          // TEACH: Inline handlers for the DOM events the <img> fires.
          //        `onLoad` is called once per successful frame — it
          //        fires many times with MJPEG, but setting the same
          //        `true` doesn't trigger a re-render (React bails out
          //        when the new state equals the old).
          onLoad={() => setLoaded(true)}
          onError={() => setError(true)}
        />
        {/* TEACH: Conditional placeholders. `!loaded && !error` — the
             "neither loaded nor failed" state, i.e. first connection
             in flight. */}
        {!loaded && !error && (
          <div className={styles.placeholder}>Waiting for video stream&hellip;</div>
        )}
        {error && (
          <div className={styles.placeholder}>Video feed unavailable</div>
        )}
        {/* --- Overlay stats row --- */}
        <div className={styles.overlay}>
          <div className={styles.stat}>
            {/* TEACH: Two classes applied at once via template string —
                 `styles.num` is the shared size/weight, `styles.accent`
                 is the colour variant for this specific stat. */}
            <div className={`${styles.num} ${styles.accent}`}>{stats.detections}</div>
            <div className={styles.statLabel}>detections</div>
          </div>
          <div className={styles.stat}>
            <div className={`${styles.num} ${styles.green}`}>{stats.persons}</div>
            <div className={styles.statLabel}>persons</div>
          </div>
          <div className={styles.stat}>
            <div className={`${styles.num} ${styles.warn}`}>{stats.vehicles}</div>
            <div className={styles.statLabel}>vehicles</div>
          </div>
          <div className={styles.stat}>
            <div className={`${styles.num} ${styles.danger}`}>{stats.interactions}</div>
            <div className={styles.statLabel}>interactions</div>
          </div>
          <div className={styles.stat}>
            {/* TEACH: No colour variant here — just the base `num`. */}
            <div className={styles.num}>{stats.fps}</div>
            <div className={styles.statLabel}>fps</div>
          </div>
        </div>
      </div>
    </div>
  );
}
