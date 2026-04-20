/**
 * Tiny display-formatting helpers used only by the monitoring feature.
 */

export function formatTimestamp(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function formatRelative(ts: string): string {
  const target = new Date(ts).getTime();
  if (Number.isNaN(target)) return ts;
  const diffSec = Math.max(0, Math.round((Date.now() - target) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.round(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.round(diffSec / 3600)}h ago`;
  return `${Math.round(diffSec / 86400)}d ago`;
}

export const SEV_ICON: Record<string, string> = {
  error: "!!",
  warning: "!",
  info: "i",
};
