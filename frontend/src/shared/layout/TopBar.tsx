/**
 * TopBar — persistent header strip rendered on every page.
 *
 * Pure presentational component: takes a connection flag, an optional
 * unread-error count for the Monitoring link, and any children to slot
 * into the right side. Pages own the data sources and pass them in,
 * which keeps `shared/` decoupled from any feature.
 */
import { Link, useLocation } from "react-router-dom";
import type { ReactNode } from "react";

import { Pill, Dot } from "../ui";

import styles from "./TopBar.module.css";

interface TopBarProps {
  /** Kept for backwards-compat with callers; not rendered (see CLAUDE.md). */
  sourceName?: string;
  connected?: boolean;
  /** Unread monitoring errors — drives the red bubble on the Monitoring link. */
  errorCount?: number;
  /** Drift findings from the shadow validator — drives the red bubble on
   *  the Validation link. Covers false positives, class mismatches, and
   *  shadow-only detections (false negatives). */
  driftCount?: number;
  children?: ReactNode;
}

export function TopBar({
  connected,
  errorCount = 0,
  driftCount = 0,
  children,
}: TopBarProps) {
  const { pathname } = useLocation();

  const statusVariant =
    connected === true ? "ok" : connected === false ? "bad" : "wait";
  const statusLabel =
    connected === true ? "live" : connected === false ? "disconnected" : "connecting…";

  return (
    <header className={styles.topbar}>
      <Link to="/" className={styles.brand}>
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <circle cx="12" cy="12" r="10" />
          <path d="M12 8v4l2 2" />
        </svg>
        Road Safety
      </Link>
      <nav className={styles.nav}>
        <Link to="/" className={pathname === "/" ? styles.active : ""}>
          Admin
        </Link>
        <Link
          to="/dashboard"
          className={pathname === "/dashboard" ? styles.active : ""}
        >
          Dashboard
        </Link>
        <Link
          to="/monitoring"
          className={`${styles.monLink} ${pathname === "/monitoring" ? styles.active : ""}`}
        >
          Monitoring
          {errorCount > 0 && (
            <span className={styles.errorBubble}>{errorCount}</span>
          )}
        </Link>
        <Link
          to="/validation"
          className={`${styles.monLink} ${pathname === "/validation" ? styles.active : ""}`}
        >
          Validation
          {driftCount > 0 && (
            <span
              className={`${styles.errorBubble} ${styles.driftBubble}`}
              title={`${driftCount} drift finding${driftCount === 1 ? "" : "s"} — second model disagrees`}
            >
              {driftCount}
            </span>
          )}
        </Link>
        <Link
          to="/settings"
          className={pathname === "/settings" ? styles.active : ""}
        >
          Settings
        </Link>
      </nav>
      <span className={styles.spacer} />
      <Pill>
        <Dot variant={statusVariant} />
        <span>{statusLabel}</span>
      </Pill>
      {children}
    </header>
  );
}
