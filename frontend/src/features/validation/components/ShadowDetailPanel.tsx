/**
 * ShadowDetailPanel — the shadow-only section slotted into EventDialog.
 *
 * Renders inside the dialog body via the ``children`` prop so we can
 * reuse the shared dialog without muddying its shape. Responsibilities:
 *
 *   * Fetch + render the miss-reason diagnostic from
 *     ``GET /api/shadow/{id}/analysis``.
 *   * Offer a "Re-run primary" action (admin-only) that shows the
 *     newly-computed primary detections next to the stored ones.
 *   * Offer an "Add to event list" action (admin-only) that promotes
 *     the shadow pair into the live event buffer + closes the dialog.
 *
 * TanStack Query handles caching + retries for the analysis GET. The
 * two admin mutations use plain ``useMutation`` so errors surface
 * inline (401/403 → prompt for token; other errors → inline red).
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { useAdminToken } from "../../../shared/hooks/useAdminToken";
import {
  MissingAdminTokenError,
  isAdminAuthFailure,
} from "../../../shared/lib/adminApi";

import {
  type GateVerdict,
  type ShadowAnalysis,
  type ShadowDetection,
  type ShadowRerunResponse,
  fetchShadowAnalysis,
  promoteShadow,
  rerunShadowPrimary,
} from "../api";

import styles from "./ShadowDetailPanel.module.css";

interface ShadowDetailPanelProps {
  shadowId: string;
  /** Called after a successful promote so the parent can close the dialog. */
  onPromoted?: (promotedEventId: string) => void;
}

function formatError(err: unknown): string {
  if (err instanceof MissingAdminTokenError) {
    return "Missing admin bearer token — set it in Settings to use this action.";
  }
  if (err instanceof Error) return err.message;
  return "Unknown error";
}

export function ShadowDetailPanel({ shadowId, onPromoted }: ShadowDetailPanelProps) {
  const { token: adminToken } = useAdminToken();
  const [rerunResult, setRerunResult] = useState<ShadowRerunResponse | null>(null);

  const analysisQuery = useQuery<ShadowAnalysis, Error>({
    queryKey: ["shadow-analysis", shadowId],
    queryFn: () => fetchShadowAnalysis(shadowId),
    // Analysis is a pure function over a stored record — safe to cache.
    staleTime: 60_000,
    retry: 1,
  });

  const rerunMut = useMutation<ShadowRerunResponse, Error, void>({
    mutationFn: () => rerunShadowPrimary(shadowId),
    onSuccess: (data) => setRerunResult(data),
  });

  const promoteMut = useMutation<{ promoted_event_id: string }, Error, void>({
    mutationFn: () => promoteShadow(shadowId),
    onSuccess: (data) => onPromoted?.(data.promoted_event_id),
  });

  const analysis = analysisQuery.data;

  return (
    <div className={styles.wrap}>
      <header className={styles.header}>
        <span className={styles.kicker}>Shadow-only detection</span>
        <span className={styles.id}>#{shadowId}</span>
      </header>

      <section className={styles.section}>
        <div className={styles.sectionLabel}>Why the primary missed this</div>
        {analysisQuery.isLoading && (
          <div className={styles.muted}>Loading analysis…</div>
        )}
        {analysisQuery.isError && (
          <div className={styles.error}>
            Failed to load analysis: {formatError(analysisQuery.error)}
          </div>
        )}
        {analysis && (
          <>
            <div className={styles.reason}>{analysis.miss_reason}</div>
            <GateTable title="Pair members" members={analysis.members} />
            <div className={styles.sectionLabel}>Pair-level gates</div>
            <ul className={styles.gateList}>
              {analysis.pair_gates.map((g) => (
                <GateRow key={g.gate} gate={g} />
              ))}
            </ul>
          </>
        )}
      </section>

      <section className={styles.section}>
        <div className={styles.sectionLabel}>Actions</div>
        {!adminToken && (
          <div className={styles.muted}>
            Re-run and Promote require an admin bearer token. Set one in
            Settings first — this keeps operator-promoted events out of
            the public surface by default.
          </div>
        )}

        <div className={styles.actionRow}>
          <button
            type="button"
            className={styles.btn}
            onClick={() => rerunMut.mutate()}
            disabled={rerunMut.isPending || !adminToken}
          >
            {rerunMut.isPending ? "Running primary…" : "Re-run primary"}
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnPrimary}`}
            onClick={() => promoteMut.mutate()}
            disabled={promoteMut.isPending || !adminToken}
          >
            {promoteMut.isPending ? "Promoting…" : "Add to event list"}
          </button>
        </div>

        {rerunMut.isError && (
          <div className={styles.error}>
            Re-run failed{isAdminAuthFailure(rerunMut.error) ? " (auth)" : ""}:{" "}
            {formatError(rerunMut.error)}
          </div>
        )}
        {promoteMut.isError && (
          <div className={styles.error}>
            Promote failed{isAdminAuthFailure(promoteMut.error) ? " (auth)" : ""}:{" "}
            {formatError(promoteMut.error)}
          </div>
        )}
        {promoteMut.isSuccess && (
          <div className={styles.ok}>
            Promoted — event id {promoteMut.data.promoted_event_id}
          </div>
        )}

        {rerunResult && (
          <div className={styles.rerun}>
            <div className={styles.sectionLabel}>Re-run output</div>
            <div className={styles.detRow}>
              <div className={styles.detCol}>
                <div className={styles.detHeader}>
                  Stored (at finding time) — {rerunResult.stored_primary.length}
                </div>
                <DetectionList dets={rerunResult.stored_primary} />
              </div>
              <div className={styles.detCol}>
                <div className={styles.detHeader}>
                  Re-run (now) — {rerunResult.rerun_primary.length}
                </div>
                <DetectionList dets={rerunResult.rerun_primary} />
              </div>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

function GateTable({
  title,
  members,
}: {
  title: string;
  members: ShadowAnalysis["members"];
}) {
  return (
    <div className={styles.memberWrap}>
      <div className={styles.sectionLabel}>{title}</div>
      {members.map((m, i) => (
        <div key={`${m.cls}-${i}`} className={styles.memberCard}>
          <div className={styles.memberHead}>
            <span className={styles.memberClass}>{m.cls}</span>
            <span className={styles.memberConf}>
              conf {(m.conf * 100).toFixed(0)}%
            </span>
          </div>
          <ul className={styles.gateList}>
            {m.gates.map((g) => (
              <GateRow key={g.gate} gate={g} />
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function GateRow({ gate }: { gate: GateVerdict }) {
  return (
    <li className={gate.passed ? styles.gatePass : styles.gateFail}>
      <span className={styles.gateName}>{gate.gate}</span>
      <span className={styles.gateActual} title={gate.threshold}>
        {gate.actual}
      </span>
      <span className={styles.gateVerdict}>{gate.passed ? "PASS" : "FAIL"}</span>
      {gate.note && <div className={styles.gateNote}>{gate.note}</div>}
    </li>
  );
}

function DetectionList({ dets }: { dets: ShadowDetection[] }) {
  if (dets.length === 0) {
    return <div className={styles.muted}>(no detections)</div>;
  }
  return (
    <ul className={styles.detList}>
      {dets.map((d, i) => (
        <li key={`${d.cls}-${d.x1}-${d.y1}-${i}`}>
          <span className={styles.detCls}>{d.cls}</span>
          <span className={styles.detConf}>{(d.conf * 100).toFixed(0)}%</span>
          <span className={styles.detBbox}>
            {d.x1},{d.y1}–{d.x2},{d.y2}
          </span>
        </li>
      ))}
    </ul>
  );
}
