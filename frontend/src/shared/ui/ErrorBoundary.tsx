/**
 * ErrorBoundary — class component (React's only built-in way) that
 * catches render-phase errors anywhere in its subtree and shows a
 * recoverable fallback instead of crashing the whole app.
 *
 * Wrap each route in one of these so a thrown error in /settings
 * doesn't take out /admin or /dashboard. Pair with `<Suspense>` for
 * lazy-loaded routes.
 *
 * Usage:
 *   <ErrorBoundary fallback={<PageError />}>
 *     <SettingsPage />
 *   </ErrorBoundary>
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

import styles from "./ErrorBoundary.module.css";
import { Button } from "./Button";

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Custom fallback UI — receives the caught error + a reset() callback. */
  fallback?: (props: { error: Error; reset: () => void }) => ReactNode;
  /** Extra logging hook (Sentry, console, etc). */
  onError?: (error: Error, info: ErrorInfo) => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.props.onError?.(error, info);
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary]", error, info);
  }

  reset = (): void => this.setState({ error: null });

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) {
      return this.props.fallback({ error, reset: this.reset });
    }
    return <DefaultFallback error={error} reset={this.reset} />;
  }
}

function DefaultFallback({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className={styles.root} role="alert">
      <h1 className={styles.title}>Something broke on this page</h1>
      <p className={styles.message}>
        The error has been logged. You can try again, reload the page, or
        navigate away — the rest of the app is unaffected.
      </p>
      <pre className={styles.details}>{error.message}</pre>
      <div className={styles.actions}>
        <Button variant="primary" onClick={reset}>
          Try again
        </Button>
        <Button onClick={() => window.location.reload()}>Reload page</Button>
      </div>
    </div>
  );
}
