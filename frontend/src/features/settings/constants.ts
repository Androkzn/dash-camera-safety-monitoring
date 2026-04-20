/**
 * Static maps used by the formatting helpers and the Tunable popovers.
 * Pure data — no React, no I/O.
 */

/** Per-token rewrite map for humanizing SCREAMING_SNAKE keys. */
export const TOKEN_REWRITES: Record<string, string> = {
  TTC: "Time to Collision",
  ALPR: "License Plate Recognition",
  LLM: "LLM",
  CB: "Circuit Breaker",
  BBOX: "Bounding Box",
  CONF: "Confidence",
  DIST: "Distance",
  LUM: "Luminance",
  LEN: "Length",
  MED: "Medium",
  FP: "FP",
  TP: "TP",
  MIN: "Minimum",
  MAX: "Maximum",
  ID: "ID",
};

/** Unit-suffix tokens recognised by `unitFromTail`. */
export const UNIT_TOKENS: Record<string, string> = {
  M: "meters",
  SEC: "seconds",
  FPS: "frames per second",
  MS: "milliseconds",
  HZ: "Hertz",
  PCT: "percent",
};

export interface TunableHelp {
  what: string;
  affects: string;
  increase?: string;
  decrease?: string;
  options?: Record<string, string>;
}

/** Per-tunable operator-facing explainer. */
export const TUNABLE_HELP: Record<string, TunableHelp> = {
  CONF_THRESHOLD: {
    what: "Minimum YOLO confidence required for a vehicle detection to enter the pipeline.",
    affects: "Detection ingest funnel — applied per-frame before any TTC, distance or pair gate.",
    increase: "Fewer detections; reduces false positives from billboards / shadows / odd-shaped trash. May miss distant or low-contrast vehicles.",
    decrease: "More detections including marginal ones. Adds load to LLM enrichment and downstream gates; may admit noise that scene-context can't fully suppress.",
  },
  PERSON_CONF_THRESHOLD: {
    what: "Minimum YOLO confidence for the person class.",
    affects: "Pedestrian detection only — vehicles use CONF_THRESHOLD.",
    increase: "Fewer false-positive 'person' boxes from occlusion bleed. Risks missing distant or partially-occluded pedestrians (which legitimately score lower).",
    decrease: "Catches more distant / partially-visible people. Below ~0.25 with YOLOv8n it lets occasional noise through; pair with a larger model variant if you go aggressive.",
  },
  VEHICLE_PAIR_CONF_FLOOR: {
    what: "Mean of two detection confidences required before a vehicle-vehicle pair becomes an event candidate.",
    affects: "Vehicle ↔ vehicle close-interaction events (pedestrian pairs are unaffected).",
    increase: "Fewer car-on-car alerts; kills the 'two low-confidence blobs that happen to overlap' false positive — historically the largest alert-fatigue source.",
    decrease: "More vehicle-pair alerts. Use only if your camera reliably yields high-confidence vehicle bboxes.",
  },
  MIN_BBOX_AREA: {
    what: "Minimum bounding-box area in square pixels for a vehicle detection to count.",
    affects: "Vehicle ingest filter — pedestrians have their own (smaller) PERSON_MIN_BBOX_AREA constant.",
    increase: "Rejects far-field tiny vehicles. Lower compute load, less far-field reach.",
    decrease: "Catches more distant vehicles but admits jitter / noise blobs that the geometry gates have to clean up.",
  },
  TTC_HIGH_SEC: {
    what: "Time-to-collision threshold (in seconds) at or below which an interaction is classified HIGH risk.",
    affects: "Risk tier of every emitted event; high-tier events trigger Slack pings.",
    increase: "More events classified HIGH. At 1.0s anything within one second of contact is high; at 0.5s it's essentially 'already colliding'.",
    decrease: "Stricter HIGH classification. Fewer Slack high-tier alerts; only the most imminent collisions fire.",
  },
  TTC_MED_SEC: {
    what: "Time-to-collision threshold at or below which an interaction is classified MEDIUM (above HIGH).",
    affects: "Dashboard highlight tier; medium events accumulate into the hourly Slack digest, not real-time pings.",
    increase: "More events promoted to MEDIUM rather than logged silently as LOW.",
    decrease: "Tighter MEDIUM band; more events demoted to LOW.",
  },
  DIST_HIGH_M: {
    what: "Inter-object 3D distance (in metres) at or below which an interaction is classified HIGH risk.",
    affects: "Same risk-tier cascade as TTC_HIGH_SEC, but using the depth-prior distance estimate.",
    increase: "More events classified HIGH purely on proximity. At 5m nearly any two vehicles in the same frame trip the gate.",
    decrease: "Only very close interactions (within arm's reach) trigger HIGH on distance alone.",
  },
  DIST_MED_M: {
    what: "Inter-object distance threshold for MEDIUM risk (above DIST_HIGH_M).",
    affects: "Medium-tier dashboard signal; combined with TTC_MED_SEC by worst-signal-wins.",
    increase: "More events in the MEDIUM band on proximity.",
    decrease: "Tighter MEDIUM proximity threshold.",
  },
  MIN_SCALE_GROWTH: {
    what: "Minimum bounding-box scale-expansion ratio over the trailing window before TTC will publish a value.",
    affects: "TTC computation — the 'is this object actually approaching' gate.",
    increase: "Stricter approach evidence; kills 'jittery stationary box' false TTC alerts. Risks missing slow approaches.",
    decrease: "TTC fires for smaller scale changes; more sensitivity, more jitter-driven false alerts. Below 1.05 you start emitting TTC for bbox noise alone.",
  },
  TRACK_HISTORY_LEN: {
    what: "Number of trailing samples kept per tracked object for TTC math.",
    affects: "TTC stability and responsiveness; longer history smooths estimates and is required for sustained-growth detection.",
    increase: "More stable TTC estimates and longer lookback for sustained-growth checks. Slightly more memory per active track and slower adaptation to new fast-moving objects.",
    decrease: "Faster reaction to brand-new tracks. Risks falling below the multi-gate sustained-growth sample-count requirement, which silently disables TTC for short-lived tracks.",
  },
  QUALITY_BLUR_SHARP: {
    what: "Laplacian-variance value below which the camera is classified as blurred (dirty lens / motion blur / fog).",
    affects: "QualityMonitor state machine — degraded states suppress event emission and widen TTC thresholds.",
    increase: "Stricter sharpness requirement; pipeline degrades into 'blurred' more often, suppressing events. Use when the lens is reliably clean and you want extra safety on borderline frames.",
    decrease: "More permissive — runs through more frames without suppression but admits blurry false positives.",
  },
  QUALITY_LOW_LIGHT_LUM: {
    what: "Mean grayscale luminance below which the scene is classified as low-light.",
    affects: "QualityMonitor degradation; below the threshold YOLO recall drops sharply on COCO classes.",
    increase: "Stricter light requirement; degrades into low-light suppression earlier (more events suppressed at dusk / under bridges).",
    decrease: "Tolerates darker scenes; risks acting on noisy detections from a dim sensor.",
  },
  LLM_BUCKET_CAPACITY: {
    what: "Burst capacity (whole tokens) of the shared LLM rate-limit bucket. Each enrichment costs 2 tokens, each narration costs 1.",
    affects: "How many LLM calls can fire back-to-back during an event burst before throttling kicks in.",
    increase: "Larger LLM bursts during event clusters. Higher peak cost and risk of 429s if you exceed the provider's rate limit.",
    decrease: "More aggressive throttling — non-essential narration / enrichment skipped during bursts. Lower cost, more 'rate budget exhausted' skip records in /api/llm/stats.",
  },
  LLM_BUCKET_REFILL_PER_MIN: {
    what: "Sustained refill rate of the LLM bucket in tokens per minute.",
    affects: "Long-run LLM call rate and cost.",
    increase: "Higher sustained LLM call rate, higher cost. Stay below the provider's rate limit (Anthropic Haiku low-tier ≈ 5 req/min).",
    decrease: "Lower sustained call rate; more events get the deterministic narration fallback rather than an LLM-generated one.",
  },
  SLACK_HIGH_MIN_CONFIDENCE: {
    what: "Peak event confidence required before a HIGH-risk event becomes a Slack high-tier ping.",
    affects: "Slack #high channel — operator paging tier. Medium / low events go to digests instead.",
    increase: "Fewer real-time Slack pings; only the most confident HIGH events page operators.",
    decrease: "More real-time pings. If your model has been drifting noisy, lowering this risks alert fatigue on the responder team.",
  },
  ALPR_MODE: {
    what: "Posture for the external license-plate-recognition service.",
    affects: "Privacy footprint, LLM cost, and what data leaves the edge.",
    options: {
      off: "No external ALPR calls. Plate text never leaves the edge. Safest privacy posture; the default.",
      on: "Every event triggers an ALPR call. Maximum recall, maximum cost, plate text crosses to the provider for every event.",
      on_demand: "ALPR only fires when an event is explicitly flagged for review. Balanced posture; cost scales with operator review rate, not event rate.",
    },
  },
  PAIR_COOLDOWN_SEC: {
    what: "After emitting an event for a (track A, track B) pair, suppress further events from the same pair for this many seconds.",
    affects: "Event-stream noise — without this a single sustained near-miss would emit ~20 events.",
    increase: "Fewer duplicate events; cleaner stream during sustained interactions. Risks coalescing two genuinely separate close-calls between the same pair into one report.",
    decrease: "More granular reporting on evolving incidents. Risks spamming the SSE stream and Slack on long sustained near-misses.",
  },
  MAX_RECENT_EVENTS: {
    what: "Capacity of the in-memory ring buffer of recent events.",
    affects: "How much history the dashboard, agents and impact engine can see without going to disk.",
    increase: "Longer event history available to UI and impact comparisons. More RAM (linearly).",
    decrease: "Smaller footprint. Less history for the impact engine, agents and the SSE replay-on-connect.",
  },
  TARGET_FPS: {
    what: "Perception-loop tick rate. Drives how often YOLO runs per stream second.",
    affects: "Latency to detect short-lived collisions versus CPU / GPU / LLM cost.",
    increase: "Faster reaction to motorbike cut-ins and other short TTC windows. Higher CPU and LLM budget burn.",
    decrease: "Lower compute load and LLM headroom. Risks missing sub-second TTC events. Below 1 fps the multi-gate sustained-growth requirement starts struggling for sample volume.",
  },
};

/** Pretty labels for impact-engine metric keys. */
export const METRIC_LABELS: Record<string, string> = {
  event_rate_per_min: "Events / min",
  confidence_p50: "Confidence p50",
  confidence_p95: "Confidence p95",
  ttc_p50: "TTC p50",
  ttc_p95: "TTC p95",
  distance_p50_m: "Distance p50, m",
  distance_p95_m: "Distance p95, m",
  sample_size: "Sample size",
  fp_rate: "False-positive rate",
  drift_precision: "Drift precision",
  feedback_coverage: "Feedback coverage",
  llm_cost_usd_per_min: "LLM cost, $ / min",
  llm_tokens_per_min: "LLM tokens / min",
  llm_latency_p95_ms: "LLM latency p95, ms",
  llm_skip_rate: "LLM skip rate",
  enrichment_skipped_rate: "Enrichment skipped rate",
  episode_duration_mean: "Episode duration, mean",
  episode_duration_p95: "Episode duration p95",
  frames_processed_ratio: "Frames processed ratio",
  actual_fps_p50: "Actual fps, p50",
  actual_fps_p95: "Actual fps, p95",
  frames_dropped_ratio_p95: "Frames dropped p95",
  cpu_p50: "CPU %, p50",
  cpu_p95: "CPU %, p95",
  memory_p95: "Memory %, p95",
};

/** Reason codes returned by the comparability gate. */
export const REASON_LABELS: Record<string, string> = {
  insufficient_events: "Insufficient events",
  scene_mix_drift: "Scene mix drifted",
  quality_drift: "Quality drifted",
  no_baseline_or_after: "No baseline or after-window yet",
};

export const SEVERITY_LABELS: Record<string, string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
  unknown: "Unknown",
};
