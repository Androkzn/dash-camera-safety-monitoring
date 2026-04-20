/**
 * Dot — small coloured status indicator (a 7x7 round span).
 * Pure presentational primitive; uses inline styles for atomicity.
 */
import type { CSSProperties } from "react";

type DotVariant = "ok" | "bad" | "wait" | "default";

const styles: Record<DotVariant, CSSProperties> = {
  ok: { background: "var(--green)", boxShadow: "0 0 6px rgba(34,197,94,.5)" },
  bad: { background: "var(--high)", animation: "blink 1.5s infinite" },
  wait: { background: "var(--medium)" },
  default: { background: "var(--low)" },
};

interface DotProps {
  variant?: DotVariant;
  style?: CSSProperties;
}

export function Dot({ variant = "default", style }: DotProps) {
  return (
    <span
      style={{
        width: 7,
        height: 7,
        borderRadius: "50%",
        display: "inline-block",
        ...styles[variant],
        ...style,
      }}
    />
  );
}
