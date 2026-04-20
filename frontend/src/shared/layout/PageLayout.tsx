/**
 * PageLayout — thin shell helpers used by route-level Suspense and
 * ErrorBoundary fallbacks. Pages themselves still own their own
 * <TopBar/> markup so the existing component contracts don't break.
 */
import type { ReactNode } from "react";

import { Spinner } from "../ui";

import styles from "./PageLayout.module.css";

export function PageShell({ children }: { children: ReactNode }) {
  return <div className={styles.page}>{children}</div>;
}

export function PageBody({ children }: { children: ReactNode }) {
  return <div className={styles.body}>{children}</div>;
}

export function RouteFallback({ label = "Loading…" }: { label?: string }) {
  return (
    <div className={styles.fallback} role="status" aria-live="polite">
      <Spinner />
      <span>{label}</span>
    </div>
  );
}
