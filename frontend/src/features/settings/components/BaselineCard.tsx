/**
 * BaselineCard — capture-baseline button + inline status.
 */

import { useState } from "react";

import {
  MissingAdminTokenError,
  type AdminApiError,
} from "../../../shared/lib/adminApi";

import { settingsApi } from "../api";

import styles from "../SettingsPage.module.css";

interface BaselineCardProps {
  onCaptured: () => void;
}

type Status =
  | { kind: "ok"; auditId: string }
  | { kind: "err"; message: string }
  | null;

export function BaselineCard({ onCaptured }: BaselineCardProps) {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Status>(null);

  return (
    <div className={styles.card}>
      <div className={styles.cardHeader}>
        <h3 className={styles.cardTitle}>Baseline</h3>
      </div>
      <p className={styles.subtle} style={{ margin: 0 }}>
        Snapshot the current event buffer. Future changes' impact is computed
        against this baseline.
      </p>
      <button
        className={styles.btn}
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setStatus(null);
          try {
            const res = await settingsApi.captureBaseline();
            setStatus({ kind: "ok", auditId: res.audit_id });
            onCaptured();
          } catch (exc) {
            if (exc instanceof MissingAdminTokenError) {
              setStatus({
                kind: "err",
                message:
                  "Admin token missing. Reload the page and paste your ROAD_ADMIN_TOKEN.",
              });
              return;
            }
            const status = (exc as AdminApiError).status;
            if (status === 401 || status === 403) {
              setStatus({
                kind: "err",
                message: `HTTP ${status} — token rejected. Re-paste your ROAD_ADMIN_TOKEN and try again.`,
              });
              return;
            }
            const body = (exc as AdminApiError).body as
              | { detail?: string; error?: string }
              | null;
            const detail =
              body?.detail ||
              body?.error ||
              (exc as Error).message ||
              "unknown error";
            setStatus({ kind: "err", message: `Capture failed: ${detail}` });
            // eslint-disable-next-line no-console
            console.error("baseline capture failed", exc);
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "Capturing…" : "Capture baseline now"}
      </button>
      {status?.kind === "ok" && (
        <div className={styles.warnings} style={{ marginBottom: 0 }}>
          Baseline captured — session{" "}
          <code>{status.auditId.slice(0, 18)}</code>. The Impact card will
          populate as new events arrive.
        </div>
      )}
      {status?.kind === "err" && (
        <div className={styles.errorList} style={{ marginBottom: 0 }}>
          {status.message}
        </div>
      )}
    </div>
  );
}
