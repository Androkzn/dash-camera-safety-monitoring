/**
 * Pill — rounded "capsule" container, typically holding a Dot + label
 * for status strips (e.g. <Pill><Dot variant="ok" /> LIVE</Pill>).
 */
import type { CSSProperties, ReactNode } from "react";

const base: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  padding: "4px 9px",
  borderRadius: 999,
  background: "#0b0f14",
  border: "1px solid var(--border)",
  fontSize: "11px",
  color: "var(--muted)",
};

interface PillProps {
  children: ReactNode;
  style?: CSSProperties;
}

export function Pill({ children, style }: PillProps) {
  return <span style={{ ...base, ...style }}>{children}</span>;
}
