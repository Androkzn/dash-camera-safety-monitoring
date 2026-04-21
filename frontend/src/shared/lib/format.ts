/**
 * format.ts — pure display-formatting helpers shared across features.
 * No React, no I/O — easy to unit-test in isolation.
 */

const pad2 = (n: number): string => String(n).padStart(2, "0");

/** "HH:MM:SS" or "—" if the timestamp is invalid. */
export function formatWallTime(ts?: string | number): string {
  const d = ts ? new Date(ts) : new Date();
  if (isNaN(d.getTime())) return "—";
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/** "lane_change" → "Lane change". */
export function humanEventType(t?: string): string {
  return (t || "event")
    .replace(/_/g, " ")
    .replace(/^\w/, (c) => c.toUpperCase());
}

/**
 * "image_degraded" → "Image degraded". General snake_case humanizer for
 * enum-ish backend values (perception_state, perception_reason, scene
 * context labels, etc.). Returns "—" for empty input so call sites can
 * render it directly without null-checking.
 */
export function humanize(value?: string | null): string {
  if (!value) return "—";
  return value
    .replace(/_/g, " ")
    .replace(/^\w/, (c) => c.toUpperCase());
}

/** "2h 07m" or "04:22" depending on duration. */
export function formatUptime(secs?: number | null): string {
  if (secs == null || secs < 0) return "—";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = Math.floor(secs % 60);
  return h > 0 ? `${h}h ${pad2(m)}m` : `${pad2(m)}:${pad2(s)}`;
}

/** 0.873 → "87%". */
export function formatConfidence(c?: number | null): string {
  return c != null ? `${Math.round(c * 100)}%` : "—";
}

/**
 * Make a thumbnail URL absolute against the current origin so a relative
 * "thumbs/abc.jpg" resolves the same regardless of which route we're on.
 */
export function normalizeThumbnail(thumb?: string): string {
  if (!thumb) return "";
  if (/^https?:/.test(thumb) || thumb.startsWith("/")) return thumb;
  return "/" + thumb;
}
