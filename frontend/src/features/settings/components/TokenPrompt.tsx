/**
 * TokenPrompt — empty state shown when the operator hasn't pasted an
 * admin bearer token yet.
 */
import { useState } from "react";

import { TopBar } from "../../../shared/layout/TopBar";

import styles from "../SettingsPage.module.css";

interface TokenPromptProps {
  sourceName: string;
  connected?: boolean;
  errorCount?: number;
  driftCount?: number;
  error: string | null;
  onSave: (token: string) => void;
}

export function TokenPrompt({
  sourceName,
  connected,
  errorCount,
  driftCount,
  error,
  onSave,
}: TokenPromptProps) {
  const [tokenInput, setTokenInput] = useState("");

  return (
    <>
      <TopBar
        sourceName={sourceName}
        connected={connected}
        errorCount={errorCount}
        driftCount={driftCount}
      />
      <main className={styles.main}>
        <section className={styles.center}>
          <div className={styles.tokenWrap}>
            <h2 className={styles.pageTitle}>Settings Console</h2>
            {error && <div className={styles.errorList}>{error}</div>}
            <p className={styles.subtle}>
              Settings is admin-tier. Paste your <code>ROAD_ADMIN_TOKEN</code>{" "}
              (kept in <code>sessionStorage</code>, cleared on tab close).
            </p>
            <div className={styles.tokenPrompt}>
              <input
                type="password"
                className={styles.tokenInput}
                placeholder="ROAD_ADMIN_TOKEN…"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && tokenInput.trim())
                    onSave(tokenInput.trim());
                }}
                autoFocus
              />
              <button
                className={`${styles.btn} ${styles.btnPrimary}`}
                disabled={!tokenInput.trim()}
                onClick={() => onSave(tokenInput.trim())}
              >
                Save token for this session
              </button>
            </div>
          </div>
        </section>
      </main>
    </>
  );
}
