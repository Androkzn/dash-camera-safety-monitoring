/**
 * StreamImage — renders a live per-source video tile, choosing between
 * persistent MJPEG (`multipart/x-mixed-replace`) and short-poll JPEG
 * snapshots based on the deployment's likely HTTP version.
 *
 * Why two transports?
 *   The server exposes two endpoints for the same underlying frame buffer:
 *     - GET /admin/video_feed/{id} → MJPEG. One long-lived HTTP connection
 *       per tile; the server pushes each freshly-encoded JPEG. No polling
 *       latency floor, no cache-bust query params, no redundant GETs.
 *     - GET /admin/frame/{id}      → single JPEG. Short-lived requests at
 *       ~400 ms intervals from the client.
 *
 *   MJPEG is strictly better — *if* the connection budget allows it.
 *   Browsers cap HTTP/1.1 at 6 concurrent connections per host. With 6+
 *   tiles + the SSE channel + ad-hoc API calls, MJPEG over HTTP/1.1
 *   stalls. HTTP/2 multiplexes all streams over a single TCP connection,
 *   making the cap irrelevant.
 *
 * Transport selection
 *   We assume HTTP/2 is available whenever the page was loaded over
 *   HTTPS — in practice, any TLS-fronted deployment (nginx, Caddy,
 *   Cloudflare, ALB) negotiates h2 by default. Plain HTTP almost always
 *   means we're talking directly to uvicorn (HTTP/1.1 only), where the
 *   poll fallback is the only thing that doesn't deadlock.
 *
 *   Operators can override via Vite env var `VITE_ROAD_VIDEO_TRANSPORT`
 *   (`mjpeg` | `poll`) at build time — useful for HTTP-fronted h2c
 *   deployments or for forcing polling during transport debugging.
 *
 * Cleanup invariant (the subtle bit)
 *   When an MJPEG `<img>` is removed from the DOM, some browsers
 *   (notably Firefox — see Mozilla bug 662195) leave the underlying
 *   multipart connection open until garbage collection. That pins one of
 *   the precious 6 HTTP/1.1 slots *and* keeps the server-side encode
 *   path warm (the slot's viewer counter never drops to zero). To force
 *   a synchronous teardown we re-point `src` at a 1×1 transparent GIF
 *   data URL on unmount and on every (id, started_at) change.
 */
import { useEffect, useRef, useState } from "react";

import type { LiveSourceStatus } from "../../../shared/types/common";

type Transport = "mjpeg" | "poll";

// 1×1 transparent GIF. Assigning this to `<img>.src` is the canonical way
// to force the browser to abort an in-flight `multipart/x-mixed-replace`
// fetch *now* (rather than at GC time). Cheap, valid, no network round-trip.
const BLANK_GIF =
  "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";

function resolveTransport(): Transport {
  // Build-time override beats runtime detection. Useful when the ops team
  // runs HTTP/2 cleartext (h2c) behind a reverse proxy that strips TLS
  // before reaching the browser, or when forcing polling for diagnosis.
  const override = (import.meta.env.VITE_ROAD_VIDEO_TRANSPORT ?? "")
    .toString()
    .trim()
    .toLowerCase();
  if (override === "mjpeg" || override === "poll") return override;
  // SSR / non-browser contexts (Vitest's jsdom included) fall back to poll —
  // it has no long-lived sockets to clean up so it's the safer default.
  if (typeof window === "undefined") return "poll";
  return window.location.protocol === "https:" ? "mjpeg" : "poll";
}

export const VIDEO_TRANSPORT: Transport = resolveTransport();

interface StreamImageProps {
  source: LiveSourceStatus;
  className?: string;
  onError: () => void;
}

/**
 * Renders the live frame for one source. Picks transport once at module
 * load (see `resolveTransport`). Callers should still gate this behind
 * `running && !errorState` so we don't open a connection for paused tiles.
 */
export function StreamImage({ source, className, onError }: StreamImageProps) {
  if (VIDEO_TRANSPORT === "mjpeg") {
    return <MjpegStreamImage source={source} className={className} onError={onError} />;
  }
  return <PollingStreamImage source={source} className={className} onError={onError} />;
}

function MjpegStreamImage({ source, className, onError }: StreamImageProps) {
  const ref = useRef<HTMLImageElement>(null);

  // The ?v=started_at param isn't a cache-buster — MJPEG is never cached —
  // it's a *re-mount key* baked into the URL so the server sees a brand
  // new connection after a restart, instead of reattaching to the prior
  // (now-stale) viewer slot. Pair this with the React `key` below.
  const startedAt = source.started_at ?? 0;
  const src = `/admin/video_feed/${source.id}?v=${startedAt}`;

  useEffect(() => {
    const img = ref.current;
    return () => {
      // Abort the multipart connection synchronously on unmount or on
      // (id, started_at) change. Without this, the prior stream lingers
      // long enough to: (a) hold one of the 6 HTTP/1.1 connection slots,
      // and (b) keep the server-side viewer counter > 0 for that slot,
      // which delays the encode-skip optimisation in `_on_frame`.
      if (img) img.src = BLANK_GIF;
    };
  }, [source.id, startedAt]);

  return (
    <img
      ref={ref}
      key={`${source.id}-${startedAt}`}
      src={src}
      alt={`Live feed: ${source.name}`}
      className={className}
      onError={onError}
    />
  );
}

function PollingStreamImage({ source, className, onError }: StreamImageProps) {
  // Polling tick. Each fresh `Date.now()` doubles as a cache-buster query
  // param so the <img> actually refetches; without it the browser would
  // happily reuse the cached JPEG forever. The 400 ms cadence matches the
  // server-side 2 fps perception loop — faster would just resend identical
  // bytes; slower would visibly lag the live feed.
  const [tick, setTick] = useState(() => Date.now());
  // Track whether the tile has ever successfully loaded a real frame. Before
  // the first success, the server returns 503 while the stream warms up; we
  // want to keep polling silently instead of bubbling every 503 up to the
  // parent (which would unmount the tile and freeze it on "Connecting…").
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    const id = window.setInterval(() => setTick(Date.now()), 400);
    return () => window.clearInterval(id);
  }, [source.id]);

  const handleError = () => {
    // Before the first successful frame: swallow the error and let the next
    // tick retry. After a prior success: escalate — a sustained error once
    // the stream was healthy is a real problem (stream paused, server
    // restarted, etc.) and the parent's fallback placeholder should show.
    if (loaded) onError();
  };

  return (
    <>
      <img
        key={`${source.id}-${source.started_at ?? "x"}`}
        src={`/admin/frame/${source.id}?t=${tick}`}
        alt={`Live feed: ${source.name}`}
        className={className}
        // Hide the element until the first real frame lands. Otherwise the
        // browser flashes its broken-image icon for each 503 returned during
        // the warmup window, which looks worse than our CSS overlay.
        style={loaded ? undefined : { visibility: "hidden" }}
        onLoad={() => {
          if (!loaded) setLoaded(true);
        }}
        onError={handleError}
      />
      {!loaded && (
        <div
          aria-live="polite"
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#94a3b8",
            fontSize: 12,
            pointerEvents: "none",
          }}
        >
          Connecting…
        </div>
      )}
    </>
  );
}
