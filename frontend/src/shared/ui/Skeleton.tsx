/**
 * Skeleton — content-loading shimmer placeholder. Pass width/height as
 * numbers (px) or strings (any CSS length).
 *
 * Usage:
 *   <Skeleton width="100%" height={20} />
 *   <Skeleton circle width={32} height={32} />
 */
import type { CSSProperties } from "react";

import styles from "./Skeleton.module.css";

interface SkeletonProps {
  width?: number | string;
  height?: number | string;
  circle?: boolean;
  className?: string;
  style?: CSSProperties;
}

export function Skeleton({ width = "100%", height = 14, circle, className, style }: SkeletonProps) {
  const cls = [styles.skeleton, className ?? ""].filter(Boolean).join(" ");
  return (
    <span
      className={cls}
      aria-hidden="true"
      style={{
        width,
        height,
        borderRadius: circle ? "50%" : undefined,
        ...style,
      }}
    />
  );
}
