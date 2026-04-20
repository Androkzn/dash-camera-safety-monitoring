/**
 * Tag — small rectangular label for structured event metadata
 * (plate hash, track id, kin grouping, …). Variant colour-codes the
 * kind of metadata so a reader can scan rows quickly.
 *
 * Privacy note: per CLAUDE.md, raw plate text MUST NEVER reach the
 * frontend — when you see a "plate" Tag, the content is a hash.
 */
import type { CSSProperties, ReactNode } from "react";

type TagVariant = "default" | "plate" | "hash" | "muted" | "kin" | "kin-warn" | "track";

const base: CSSProperties = {
  display: "inline-block",
  padding: "2px 7px",
  borderRadius: 4,
  background: "#0b0f14",
  border: "1px solid var(--border)",
  color: "var(--muted)",
  fontSize: "10.5px",
  letterSpacing: "0.3px",
};

const variants: Record<TagVariant, CSSProperties> = {
  default: {},
  plate: {
    background: "#1a1300",
    borderColor: "#3f2b00",
    color: "#fbbf24",
    fontFamily: "var(--font-mono)",
    fontWeight: 600,
    letterSpacing: "1px",
  },
  hash: {
    background: "#0a1a2e",
    borderColor: "#1e3a5f",
    color: "#7dd3fc",
    fontFamily: "var(--font-mono)",
    fontSize: "10px",
  },
  muted: { opacity: 0.6, fontStyle: "italic" },
  kin: {
    background: "#0a1a0e",
    borderColor: "#14532d",
    color: "#86efac",
    fontFamily: "var(--font-mono)",
    fontWeight: 600,
  },
  "kin-warn": {
    background: "#2a0f0f",
    borderColor: "#7f1d1d",
    color: "#fca5a5",
    fontFamily: "var(--font-mono)",
    fontWeight: 600,
  },
  track: {
    background: "#1a1022",
    borderColor: "#3f1d5f",
    color: "#c4b5fd",
    fontFamily: "var(--font-mono)",
  },
};

interface TagProps {
  variant?: TagVariant;
  title?: string;
  children: ReactNode;
  style?: CSSProperties;
}

export function Tag({ variant = "default", title, children, style }: TagProps) {
  return (
    <span style={{ ...base, ...variants[variant], ...style }} title={title}>
      {children}
    </span>
  );
}
