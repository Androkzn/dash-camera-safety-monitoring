/**
 * Shadow-finding → SafetyEvent adapter.
 *
 * Turns a ``WatchdogFinding`` with ``fingerprint === "validator/false-
 * negative"`` into an object shaped like ``SafetyEvent`` so the existing
 * :component:`EventDialog` can render it without a second code path.
 *
 * The dialog recognises shadow events by the ``event_id`` prefix
 * (``"shadow_<snapshot_id>"``) and by the presence of a matching
 * ``shadow_id`` prop on the dialog; the adapter does not leak any
 * shadow-specific ceremony into the base SafetyEvent shape.
 */

import type { SafetyEvent, WatchdogFinding } from "../../../shared/types/common";

function evidence(f: WatchdogFinding, label: string): string | undefined {
  return f.evidence?.find((e) => e.label === label)?.value;
}

function parseNumber(v: string | undefined): number | undefined {
  if (!v) return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}

function meanConfidence(f: WatchdogFinding): number | undefined {
  const raw = evidence(f, "secondary_pair_confs"); // e.g. "0.80,0.75"
  if (!raw) return undefined;
  const values = raw
    .split(",")
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isFinite(n));
  if (values.length === 0) return undefined;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

function pairClasses(f: WatchdogFinding): string[] | undefined {
  const raw = evidence(f, "secondary_pair_classes");
  if (!raw) return undefined;
  const parts = raw.split(",").map((s) => s.trim()).filter(Boolean);
  return parts.length > 0 ? parts : undefined;
}

export function getShadowId(f: WatchdogFinding): string | undefined {
  return evidence(f, "shadow_id");
}

export function isShadowFinding(f: WatchdogFinding): boolean {
  return (f.fingerprint ?? "").endsWith("false-negative");
}

/** Risk string → typed risk_level with a safe fallback. */
function riskLevel(v: string | undefined): SafetyEvent["risk_level"] {
  if (v === "high" || v === "medium" || v === "low") return v;
  return "medium";
}

/**
 * Build a SafetyEvent-shaped object from a shadow-only finding.
 *
 * The ``event_id`` is deterministic (``shadow_<snapshot_id>_<ts>``) so
 * re-renders of the same row produce the same dialog key — important
 * for React reconciliation when the finding list re-sorts.
 */
export function shadowFindingToEvent(f: WatchdogFinding): SafetyEvent {
  const shadowId = getShadowId(f) ?? f.snapshot_id;
  const classes = pairClasses(f);
  return {
    event_id: `shadow_${f.snapshot_id}_${f.ts}`,
    wall_time: f.ts,
    event_type: evidence(f, "event_type") ?? "shadow_only",
    risk_level: riskLevel(evidence(f, "secondary_risk")),
    confidence: meanConfidence(f),
    objects: classes,
    track_ids: undefined,
    ttc_sec: null,
    distance_m: parseNumber(evidence(f, "distance_m")) ?? null,
    distance_px: parseNumber(evidence(f, "distance_px")) ?? null,
    // Redacted thumbnail URL — the backend always saves one when a
    // shadow record is emitted, so this is safe to assume present.
    thumbnail: `thumbnails/shadow_${shadowId}.jpg`,
    source_id: evidence(f, "slot_id"),
    source_name: evidence(f, "slot_id"),
    summary: f.detail,
    narration: null,
    perception_state: "nominal",
    enrichment_skipped: "shadow_only",
  };
}
