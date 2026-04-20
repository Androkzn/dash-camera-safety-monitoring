/**
 * Settings-specific display helpers.
 *
 * Numeric formatting, key humanization, label lookups, source shortening,
 * tier-class picker. Pure functions — no React.
 */
import {
  METRIC_LABELS,
  REASON_LABELS,
  SEVERITY_LABELS,
  TOKEN_REWRITES,
  UNIT_TOKENS,
} from "../constants";
import type { ConfidenceTier } from "../types";

import settingsStyles from "../SettingsPage.module.css";

export function fmt(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return Number(v).toFixed(digits);
}

export function tierClass(tier: ConfidenceTier): string {
  switch (tier) {
    case "high":
      return settingsStyles.tierHigh ?? "";
    case "medium":
      return settingsStyles.tierMedium ?? "";
    case "low":
      return settingsStyles.tierLow ?? "";
    default:
      return settingsStyles.tierInsufficient ?? "";
  }
}

function unitFromTail(tokens: string[]): { unit: string | null; consume: number } {
  if (tokens.length === 0) return { unit: null, consume: 0 };
  const last = (tokens[tokens.length - 1] ?? "").toUpperCase();
  // PER + time unit composes into "per <unit>"
  if (
    tokens.length >= 2 &&
    (tokens[tokens.length - 2] ?? "").toUpperCase() === "PER"
  ) {
    if (last === "MIN") return { unit: "per minute", consume: 2 };
    if (last === "SEC") return { unit: "per second", consume: 2 };
    if (last === "HOUR") return { unit: "per hour", consume: 2 };
  }
  if (UNIT_TOKENS[last]) return { unit: UNIT_TOKENS[last]!, consume: 1 };
  return { unit: null, consume: 0 };
}

function humanizeToken(word: string): string {
  const upper = word.toUpperCase();
  if (TOKEN_REWRITES[upper] !== undefined) return TOKEN_REWRITES[upper];
  if (/^P\d+$/.test(upper)) return upper.toLowerCase();
  const lower = word.toLowerCase();
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

export function humanize(raw: string): string {
  const tokens = raw.split(/[_\-\s]+/).filter(Boolean);
  const { unit, consume } = unitFromTail(tokens);
  const head = consume > 0 ? tokens.slice(0, tokens.length - consume) : tokens;
  const headLabel = head.map(humanizeToken).join(" ");
  return unit ? `${headLabel}, ${unit}` : headLabel;
}

export function formatState(state: string): string {
  if (state === "monitoring_unattended") return "Monitoring (unattended)";
  return humanize(state);
}

export function metricLabel(key: string): string {
  return METRIC_LABELS[key] ?? humanize(key);
}

export function reasonLabel(key: string): string {
  return REASON_LABELS[key] ?? humanize(key);
}

export function severityLabel(key: string): string {
  return SEVERITY_LABELS[key] ?? humanize(key);
}

export function shortSource(src: string): string {
  if (!src) return "—";
  if (src.startsWith("http")) {
    try {
      return new URL(src).hostname.replace(/^www\./, "");
    } catch {
      return src.slice(0, 24);
    }
  }
  const seg = src.split("/").filter(Boolean).pop();
  return seg || src;
}
