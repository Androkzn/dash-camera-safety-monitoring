/**
 * ApplyResultBanner — success/rollback/template summary that hangs
 * around until the operator dismisses it.
 */
import { humanize } from "../utils/formatting";
import type { DraftValue } from "../types";

import styles from "../SettingsPage.module.css";

export interface ApplyResultPayloadView {
  kind: "apply" | "rollback" | "template";
  diff: Record<string, { before: DraftValue; after: DraftValue }>;
  applied_now: string[];
  pending_restart: string[];
  audit_id?: string | null;
}

interface ApplyResultBannerProps {
  result: ApplyResultPayloadView | null;
  onDismiss: () => void;
}

export function ApplyResultBanner({ result, onDismiss }: ApplyResultBannerProps) {
  if (!result) return null;
  const diffCount = Object.keys(result.diff).length;
  return (
    <div className={styles.successBanner} role="status">
      <div>
        <strong>
          {result.kind === "rollback"
            ? "Rolled back to last-known-good."
            : result.kind === "template"
              ? "Template applied."
              : `Applied ${diffCount} change${diffCount === 1 ? "" : "s"}.`}
        </strong>
        {result.applied_now.length > 0 && (
          <>
            {" "}Live now:{" "}
            {result.applied_now.map((k, i) => (
              <span key={k}>
                {i > 0 ? ", " : ""}<code>{humanize(k)}</code>
              </span>
            ))}
            .
          </>
        )}
        {result.pending_restart.length > 0 && (
          <>
            {" "}<strong>Needs restart to take effect:</strong>{" "}
            {result.pending_restart.map((k, i) => (
              <span key={k}>
                {i > 0 ? ", " : ""}<code>{humanize(k)}</code>
              </span>
            ))}
            .
          </>
        )}
        {result.audit_id && (
          <>
            {" "}Impact session <code>{result.audit_id.slice(0, 18)}</code>{" "}
            started — watch the card on the right.
          </>
        )}
      </div>
      {result.kind === "apply" && diffCount > 0 && (
        <div className={styles.subtle} style={{ fontSize: 11 }}>
          {Object.entries(result.diff).map(([k, ba]) => (
            <div key={k}>
              <code>{humanize(k)}</code>: {String(ba.before)} →{" "}
              {String(ba.after)}
            </div>
          ))}
        </div>
      )}
      <button type="button" className={styles.dismiss} onClick={onDismiss}>
        Dismiss
      </button>
    </div>
  );
}
