/**
 * Shadow-only detection API surface — endpoint wrappers for every
 * route under ``/api/shadow/*``.
 *
 * Design:
 *   - Public GETs (``record``, ``analysis``) use plain ``fetch`` so
 *     readers don't need an admin token.
 *   - Admin-tier POSTs (``rerun``, ``promote``) go through
 *     ``adminFetch`` which attaches the bearer token and surfaces
 *     structured 401/403 errors.
 *   - Types mirror the backend dataclasses in
 *     ``road_safety/core/shadow_store.py`` + ``shadow_analysis.py``.
 *     Keep these aligned when the dataclass schema changes.
 */

import { adminFetch } from "../../shared/lib/adminApi";

// =============================================================================
// Types — mirror the backend payloads
// =============================================================================

export interface ShadowDetection {
  cls: string;
  conf: number;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  track_id: number | null;
}

export interface ShadowRecord {
  shadow_id: string;
  slot_id: string;
  wall_ts: number;
  event_type: string;
  secondary_risk: string;
  distance_m: number | null;
  distance_px: number;
  frame_h: number;
  frame_w: number;
  secondary_pair: ShadowDetection[];
  secondary_detections: ShadowDetection[];
  primary_detections: ShadowDetection[];
  thumbnail: string;
}

export interface GateVerdict {
  gate: string;
  passed: boolean;
  actual: string;
  threshold: string;
  note?: string;
}

export interface MemberAnalysis {
  cls: string;
  conf: number;
  gates: GateVerdict[];
}

export interface ShadowAnalysis {
  shadow_id: string;
  event_type: string;
  miss_reason: string;
  members: MemberAnalysis[];
  pair_gates: GateVerdict[];
  calibration_used: "slot" | "default";
}

export interface ShadowRerunResponse {
  shadow_id: string;
  stored_primary: ShadowDetection[];
  rerun_primary: ShadowDetection[];
}

export interface ShadowPromoteResponse {
  promoted_event_id: string;
  // Deliberately weakly typed — the shape already matches SafetyEvent,
  // but the dialog only uses the id for the "already promoted" signal.
  event: Record<string, unknown>;
}

// =============================================================================
// Public GETs (no admin token required)
// =============================================================================

export async function fetchShadowRecord(shadowId: string): Promise<ShadowRecord> {
  const res = await fetch(`/api/shadow/${encodeURIComponent(shadowId)}`, {
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`shadow record ${shadowId}: HTTP ${res.status}`);
  }
  return (await res.json()) as ShadowRecord;
}

export async function fetchShadowAnalysis(shadowId: string): Promise<ShadowAnalysis> {
  const res = await fetch(`/api/shadow/${encodeURIComponent(shadowId)}/analysis`, {
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`shadow analysis ${shadowId}: HTTP ${res.status}`);
  }
  return (await res.json()) as ShadowAnalysis;
}

// =============================================================================
// Admin-tier POSTs (require bearer token via adminFetch)
// =============================================================================

export function rerunShadowPrimary(shadowId: string): Promise<ShadowRerunResponse> {
  return adminFetch<ShadowRerunResponse>(
    `/api/shadow/${encodeURIComponent(shadowId)}/rerun`,
    { method: "POST" },
  );
}

export function promoteShadow(shadowId: string): Promise<ShadowPromoteResponse> {
  return adminFetch<ShadowPromoteResponse>(
    `/api/shadow/${encodeURIComponent(shadowId)}/promote`,
    { method: "POST" },
  );
}
