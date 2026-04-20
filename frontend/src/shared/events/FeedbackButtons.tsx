/**
 * FeedbackButtons — "Correct" / "False alarm" pair rendered at the bottom
 * of each EventCard. POSTs the operator's verdict to `/api/feedback`,
 * which feeds into the drift / precision tracker (services/drift.py).
 *
 * Lives in `shared/events/` because both Dashboard and Admin event
 * surfaces reuse it.
 */

import { useState, useCallback } from "react";

import { postJson } from "../lib/fetchClient";

import styles from "./FeedbackButtons.module.css";

interface FeedbackButtonsProps {
  eventId: string;
}

export function FeedbackButtons({ eventId }: FeedbackButtonsProps) {
  const [state, setState] = useState<{
    submitted: boolean;
    verdict: string | null;
    error: boolean;
    loading: boolean;
  }>({ submitted: false, verdict: null, error: false, loading: false });

  const submit = useCallback(
    async (verdict: "tp" | "fp") => {
      if (state.submitted || state.loading) return;
      setState({ submitted: false, verdict: null, error: false, loading: true });
      try {
        await postJson<{ status: string }>("/api/feedback", {
          event_id: eventId,
          verdict,
        });
        setState({ submitted: true, verdict, error: false, loading: false });
      } catch {
        setState({ submitted: false, verdict: null, error: true, loading: false });
      }
    },
    [eventId, state.submitted, state.loading],
  );

  return (
    <div className={styles.fb}>
      <button
        type="button"
        className={`${styles.btn} ${styles.tp} ${state.verdict === "tp" ? styles.chosen : ""} ${state.submitted && state.verdict !== "tp" ? styles.faded : ""}`}
        disabled={state.submitted || state.loading}
        onClick={() => submit("tp")}
        title="True positive"
      >
        <svg className={styles.ico} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M7 11v9H4v-9zM7 11l4-8a2 2 0 0 1 3 1v5h5a2 2 0 0 1 2 2l-2 7a2 2 0 0 1-2 2H7" />
        </svg>
        Correct
      </button>
      <button
        type="button"
        className={`${styles.btn} ${styles.fp} ${state.verdict === "fp" ? styles.chosen : ""} ${state.submitted && state.verdict !== "fp" ? styles.faded : ""}`}
        disabled={state.submitted || state.loading}
        onClick={() => submit("fp")}
        title="False alarm"
      >
        <svg className={styles.ico} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M17 13V4h3v9zM17 13l-4 8a2 2 0 0 1-3-1v-5H5a2 2 0 0 1-2-2l2-7a2 2 0 0 1 2-2h10" />
        </svg>
        False alarm
      </button>
      {state.submitted && (
        <span className={`${styles.ack} ${styles.ok}`}>&#10003; thanks</span>
      )}
      {state.error && (
        <span className={`${styles.ack} ${styles.err}`}>(retry)</span>
      )}
    </div>
  );
}
