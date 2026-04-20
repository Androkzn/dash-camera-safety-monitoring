/**
 * Spinner — small CSS-only loading spinner. Sizes are in pixels for
 * predictable inline alignment alongside text.
 */
import type { CSSProperties } from "react";

import styles from "./Spinner.module.css";

interface SpinnerProps {
  size?: number;
  thickness?: number;
  className?: string;
  style?: CSSProperties;
  ariaLabel?: string;
}

export function Spinner({
  size = 16,
  thickness = 2,
  className,
  style,
  ariaLabel = "Loading",
}: SpinnerProps) {
  const cls = [styles.spinner, className ?? ""].filter(Boolean).join(" ");
  return (
    <span
      role="status"
      aria-label={ariaLabel}
      className={cls}
      style={{ width: size, height: size, borderWidth: thickness, ...style }}
    />
  );
}
