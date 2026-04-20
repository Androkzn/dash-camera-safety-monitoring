/**
 * Shared TypeScript types — cross-feature backend contracts.
 *
 * Mirrors JSON shapes produced by FastAPI handlers in the `road_safety`
 * Python package. Pure types, no runtime code (TS erases them at build).
 *
 * Feature-specific types (e.g. settings) live in their own feature folder
 * (`features/settings/types.ts`).
 */

// =============================================================================
// Event enrichment (LLM/ALPR post-processing output)
// =============================================================================

/**
 * Data attached to an event by the LLM enrichment step.
 * SECURITY: `plate_text` / `plate_state` are deliberately NOT in this
 * type — backend strips them at ingest. Only the hashed plate reaches
 * the frontend.
 */
export interface Enrichment {
  plate_hash?: string;
  readability?: string;
  vehicle_color?: string;
  vehicle_type?: string;
}

// =============================================================================
// Core event — the primary payload pushed over SSE
// =============================================================================

export interface SafetyEvent {
  event_id: string;
  vehicle_id?: string;
  road_id?: string;
  driver_id?: string;
  video_id?: string;
  timestamp_sec?: number;
  wall_time?: string;
  event_type: string;
  risk_level: "high" | "medium" | "low";
  peak_risk_level?: "high" | "medium" | "low";
  risk_demoted?: boolean;
  confidence?: number;
  objects?: string[];
  track_ids?: number[];
  episode_duration_sec?: number;
  ttc_sec?: number | null;
  distance_m?: number | null;
  distance_px?: number | null;
  summary?: string;
  narration?: string | null;
  thumbnail?: string;
  enrichment?: Enrichment;
  enrichment_skipped?: string;
  perception_state?: string;
  scene_context?: SceneContext;
  ego_flow?: {
    speed_proxy_mps?: number;
    confidence?: number;
  };
  /** Camera slot orientation from CameraCalibration: "forward" | "rear" | "side". */
  camera_orientation?: string;
  /** SAE J3063 taxonomy family: FCW | BSW | RCW | RCTA | NONE. */
  event_taxonomy?: string;
  /** Source slot id (optional for backwards compat with older snapshots). */
  source_id?: string;
  /** Human-readable slot name (e.g. "Left mirror"). */
  source_name?: string;
  _meta?: string;
}

// =============================================================================
// Perception / live status
// =============================================================================

export interface PerceptionState {
  _meta: "perception_state";
  state: string;
  reason: string;
  luminance?: number;
  sharpness?: number;
  avg_confidence?: number;
  samples?: number;
}

export interface LiveStatus {
  source: string;
  running: boolean;
  event_count: number;
  frames_read: number;
  frames_processed: number;
  uptime_sec: number;
  started_at: number | null;
  perception?: PerceptionState;
  target_fps?: number;
  alpr_mode?: string;
  location?: string;
}

/**
 * Transport / origin of a perception source, used to pick a UI badge.
 *  - `dashcam_file` — local MP4 replayed on a loop (the demo dashcam).
 *  - `live_hls`     — HLS / RTSP / RTMP network URL.
 *  - `webcam`       — OS webcam device (`cv2.VideoCapture("0")`).
 *  - `unknown`      — empty / unrecognised (no badge).
 */
export type StreamType =
  | "dashcam_file"
  | "live_hls"
  | "webcam"
  | "unknown";

export interface LiveSourceStatus {
  id: string;
  name: string;
  url: string;
  /** Classification of the source transport; drives the grid tile badge. */
  stream_type: StreamType;
  running: boolean;
  detection_enabled: boolean;
  last_error: string | null;
  frames_read: number;
  frames_processed: number;
  uptime_sec: number;
  /** Current MP4 playback head in seconds (dashcam_file only; 0 for live). */
  playback_pos_sec: number;
  /** Duration of the backing MP4 in seconds (dashcam_file only; 0 for live). */
  playback_duration_sec: number;
  started_at: number | null;
  active_episodes: number;
  perception_state: string | null;
  perception_reason: string | null;
}

export interface LiveSourcesResponse {
  primary_id: string;
  sources: LiveSourceStatus[];
}

// =============================================================================
// Scene context & drift
// =============================================================================

export interface SceneContext {
  label: string;
  confidence?: number;
  speed_proxy_mps?: number;
  pedestrian_rate_per_min?: number;
  reason: string;
  thresholds?: {
    ttc_high_sec: number;
    ttc_med_sec: number;
    dist_high_m: number;
    dist_med_m: number;
  };
}

export interface DriftReport {
  window_size: number;
  true_positives: number;
  false_positives: number;
  precision?: number;
  trend?: string;
  alert_triggered?: boolean;
}

// =============================================================================
// Raw detections — per-frame debug overlay data
// =============================================================================

export interface DetectionObject {
  cls: string;
  conf: number;
  track_id?: number | null;
  bbox: [number, number, number, number];
  /** Ego→object distance in metres from the monocular depth estimator.
   *  Already offset by the camera→bumper distance so the number is the
   *  gap to the ego car's nearest edge, not the camera glass. Null when
   *  estimation is disabled or the object is above the horizon. */
  distance_m?: number | null;
  /** Semantic axis of ``distance_m``.
   *  - ``"range"``:   forward / rear cams — longitudinal distance down
   *    the direction of travel; TTC is meaningful.
   *  - ``"lateral"``: side-window cams — sideways distance to adjacent-
   *    lane traffic; TTC is largely meaningless for this axis.
   *  Optional for backwards compat with snapshots from older edge nodes
   *  that didn't tag the axis (treat undefined as ``"range"``). */
  distance_axis?: "range" | "lateral";
}

export interface DetectionSnapshot {
  ts: number;
  /** Source id this snapshot came from. Optional for backwards compat
   *  with single-source servers. */
  source_id?: string;
  source_name?: string;
  detections: number;
  persons: number;
  vehicles: number;
  interactions: number;
  objects: DetectionObject[];
  /** Authoritative MP4 playhead in seconds, sourced from
   *  ``cv2.CAP_PROP_POS_MSEC`` per frame. ``0`` for live feeds; for
   *  looped local files this advances while the video plays, freezes on
   *  pause, and wraps back to ``0`` when the MP4 loops. The map overlay
   *  uses this as the authoritative clock so the marker stays locked to
   *  what the camera is actually showing. */
  playback_pos_sec?: number;
  /** Total MP4 duration in seconds (``0`` for live feeds). */
  playback_duration_sec?: number;
}

// =============================================================================
// Health endpoint
// =============================================================================

export interface HealthData {
  server: {
    running: boolean;
    uptime_sec: number;
    started_at: number | null;
    source: string;
    target_fps: number;
  };
  pipeline: {
    frames_read: number;
    frames_processed: number;
    event_count: number;
    active_episodes: number;
    tracker: string;
    risk_model: string;
    model: string;
  };
  integrations: {
    llm_configured: boolean;
    slack_configured: boolean;
    edge_publisher: boolean;
    pii_redaction: string;
    dsar_endpoint: boolean;
  };
  perception: {
    state: string;
    reason: string;
    samples: number;
    avg_confidence?: number;
    luminance?: number;
    sharpness?: number;
  };
  scene: {
    label: string;
    confidence?: number;
    speed_proxy_mps?: number;
    reason: string;
  };
}

// =============================================================================
// Watchdog
// =============================================================================

export interface WatchdogFinding {
  severity: "error" | "warning" | "info";
  category: string;
  title: string;
  detail: string;
  suggestion: string;
  impact?: string;
  likely_cause?: string;
  owner?: string;
  runbook?: string;
  fingerprint?: string;
  source?: "rule" | "ai";
  cause_confidence?: "observed" | "inferred";
  priority_score?: number;
  evidence?: Array<{
    label: string;
    value: string;
    threshold?: string;
    status?: string;
  }>;
  investigation_steps?: string[];
  debug_commands?: string[];
  ts: string;
  snapshot_id: string;
}

export interface WatchdogStatus {
  enabled: boolean;
  interval_sec: number;
  last_run: number;
  last_run_ago_sec: number | null;
  run_count: number;
  total_findings_emitted: number;
  total_findings: number;
  unique_incidents?: number;
  repeating_incidents?: number;
  by_severity: Record<string, number>;
  by_category: Record<string, number>;
  top_incidents?: Array<{
    fingerprint: string;
    severity: string;
    category: string;
    title: string;
    owner: string;
    count: number;
    first_seen_ts: string;
    last_seen_ts: string;
    latest: WatchdogFinding;
  }>;
}

// =============================================================================
// Test runner
// =============================================================================

export interface TestResult {
  name: string;
  node_id: string;
  file: string;
  outcome: "passed" | "failed" | "error" | "skipped";
  duration_ms: number;
  message?: string;
}

export interface TestStatus {
  status: "idle" | "running" | "passed" | "failed";
  total: number;
  passed: number;
  failed: number;
  skipped: number;
  progress: number;
  elapsed_sec: number;
  results: TestResult[];
}
