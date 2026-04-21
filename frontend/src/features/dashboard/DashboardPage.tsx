/**
 * DashboardPage — fleet overview + LLM copilot.
 *
 * Reads (mostly via TanStack Query): live status, scene context, drift,
 * pytest status, plus the SSE event stream. Owns local UI state for the
 * filter bar and TestDrawer toggle. Everything else is composition.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useEventStream } from "../../shared/hooks/useEventStream";
import { useLiveStatus } from "../../shared/hooks/useLiveStatus";
import {
  adminFetch,
  isAdminAuthFailure,
  MissingAdminTokenError,
  setAdminToken,
} from "../../shared/lib/adminApi";
import { humanEventType } from "../../shared/lib/format";
import { TopBar } from "../../shared/layout/TopBar";
import { useDialog } from "../../shared/ui";
import { EventCard, EventDialog } from "../../shared/events";
import type { SafetyEvent } from "../../shared/types/common";
import { TestBadge, TestDrawer, useTests } from "../tests";
import { useDriftCount } from "../validation";
import { useWatchdogCtx } from "../watchdog";

import {
  CopilotPanel,
  DriftBannerRow,
  PerceptionBannerRow,
  SceneBannerRow,
  SummaryTiles,
} from "./components";
import { useDrift } from "./hooks/useDrift";
import { useScene } from "./hooks/useScene";

import styles from "./DashboardPage.module.css";

export function DashboardPage() {
  const { events, perception, connected, counts, clearEvents } = useEventStream();
  const { data: liveStatus } = useLiveStatus();
  const { data: scene } = useScene();
  const { data: drift, refetch: refreshDrift } = useDrift();
  const { status: testStatus, rerun: rerunTests } = useTests();
  const {
    status: wdStatus,
    findings,
    clearAll: clearAllFindings,
  } = useWatchdogCtx();
  const driftCount = useDriftCount();
  const [clearingEvents, setClearingEvents] = useState(false);
  const hasFindings = (findings?.length ?? 0) > 0;
  const dialog = useDialog();

  const handleClearEvents = useCallback(async () => {
    const ok = await dialog.confirm({
      title: "Clear all events?",
      message:
        hasFindings
          ? "This wipes the event feed and all watchdog findings. The action is local and can't be undone."
          : "This wipes the event feed. The action is local and can't be undone.",
      okLabel: "Clear all",
      cancelLabel: "Cancel",
      variant: "danger",
    });
    if (!ok) return;

    // Run the admin-tier clear, with one inline token-prompt retry if the
    // first attempt fails with a missing/invalid admin token.
    const runClear = async (): Promise<void> => {
      await adminFetch<{ cleared: number }>("/api/events", { method: "DELETE" });
      clearEvents();
      if (hasFindings) {
        await clearAllFindings();
      }
    };

    setClearingEvents(true);
    try {
      try {
        await runClear();
      } catch (exc) {
        if (!(exc instanceof MissingAdminTokenError) && !isAdminAuthFailure(exc)) {
          throw exc;
        }
        const title =
          exc instanceof MissingAdminTokenError
            ? "Enter admin token to clear events"
            : "Admin token rejected — enter a new one";
        const entered = await dialog.prompt({
          title,
          message:
            "Paste your admin token below. It's stored in this session only " +
            "(sessionStorage) and sent as Authorization: Bearer.",
          placeholder: "admin token",
          inputType: "password",
          okLabel: "Save & clear",
          cancelLabel: "Cancel",
          variant: "warning",
        });
        if (!entered) return;
        setAdminToken(entered);
        await runClear();
      }
    } catch (exc) {
      await dialog.alert({
        title: "Clear all events failed",
        message: (exc as Error)?.message ?? "unknown error",
        variant: "danger",
      });
    } finally {
      setClearingEvents(false);
    }
  }, [clearEvents, clearAllFindings, hasFindings, dialog]);

  const [drawerOpen, setDrawerOpen] = useState(false);
  const prevTestStatus = useRef<string>("idle");

  const [filterRisk, setFilterRisk] = useState("");
  const [filterType, setFilterType] = useState("");
  const [showLow, setShowLow] = useState(false);
  // Event-detail modal: clicking any EventCard opens the same dialog
  // already used by the validation page, so reviewers can scrub the
  // annotated \u00b13s clip without leaving the dashboard.
  const [selectedEvent, setSelectedEvent] = useState<SafetyEvent | null>(null);

  // Auto-open the TestDrawer when tests flip running → failed.
  useEffect(() => {
    if (
      testStatus?.status === "failed" &&
      prevTestStatus.current === "running" &&
      !drawerOpen
    ) {
      setDrawerOpen(true);
    }
    if (testStatus) prevTestStatus.current = testStatus.status;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [testStatus?.status]);

  const sourceName = liveStatus?.source ?? "—";
  const [uptimeSec, setUptimeSec] = useState<number | null>(null);

  useEffect(() => {
    if (!liveStatus?.started_at) return;
    const startedAt = liveStatus.started_at;
    const tick = () => setUptimeSec(Math.max(0, Date.now() / 1000 - startedAt));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [liveStatus?.started_at]);

  const mergedPerception = perception ?? (liveStatus?.perception || null);

  const eventTypes = useMemo(() => {
    const seen = new Set<string>();
    for (const ev of events) if (ev.event_type) seen.add(ev.event_type);
    return Array.from(seen).sort();
  }, [events]);

  const filtered = useMemo(() => {
    let list = events;
    if (!showLow && filterRisk !== "low") {
      list = list.filter((e) => e.risk_level !== "low");
    }
    if (filterRisk) list = list.filter((e) => e.risk_level === filterRisk);
    if (filterType) list = list.filter((e) => e.event_type === filterType);
    return list;
  }, [events, filterRisk, filterType, showLow]);

  const hasFilters = filterRisk !== "" || filterType !== "";

  return (
    <>
      <TopBar
        sourceName={sourceName}
        connected={connected}
        errorCount={wdStatus?.by_severity?.error ?? 0}
        driftCount={driftCount}
      >
        <TestBadge status={testStatus} onClick={() => setDrawerOpen((o) => !o)} />
      </TopBar>

      <TestDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        status={testStatus}
        onRerun={rerunTests}
      />

      <div className={styles.app}>
        <section className={styles.panel}>
          <SummaryTiles
            total={counts.total}
            high={counts.high}
            medium={counts.medium}
            uptimeSec={uptimeSec}
          />
          <PerceptionBannerRow perception={mergedPerception} />
          <SceneBannerRow scene={scene ?? null} />
          <DriftBannerRow drift={drift ?? null} onRefresh={() => refreshDrift()} />

          <div className={styles.filterBar}>
            <select
              className={styles.filterSelect}
              value={filterRisk}
              onChange={(e) => setFilterRisk(e.target.value)}
            >
              <option value="">All risks</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
            <select
              className={styles.filterSelect}
              value={filterType}
              onChange={(e) => setFilterType(e.target.value)}
            >
              <option value="">All types</option>
              {eventTypes.map((t) => (
                <option key={t} value={t}>
                  {humanEventType(t)}
                </option>
              ))}
            </select>
            {hasFilters && (
              <button
                className={styles.clearBtn}
                onClick={() => {
                  setFilterRisk("");
                  setFilterType("");
                }}
              >
                Clear
              </button>
            )}
            <label className={styles.showLow}>
              <input
                type="checkbox"
                checked={showLow}
                onChange={(e) => setShowLow(e.target.checked)}
              />
              Show low risk
            </label>
            <span className={styles.filterCount}>
              {hasFilters
                ? `${filtered.length} / ${events.length}`
                : `${events.length}`}{" "}
              events
            </span>
            <button
              type="button"
              className={styles.clearAllBtn}
              onClick={() => void handleClearEvents()}
              disabled={clearingEvents || (events.length === 0 && !hasFindings)}
              title="Clear events and all corresponding monitoring records"
            >
              {clearingEvents ? "Clearing…" : "Clear all events"}
            </button>
          </div>

          <div className={styles.stream}>
            {events.length === 0 && (
              <div className={styles.empty}>Waiting for events…</div>
            )}
            {events.length > 0 && filtered.length === 0 && (
              <div className={styles.empty}>No events match filters</div>
            )}
            {filtered.map((ev, i) => (
              <EventCard
                key={ev.event_id}
                event={ev}
                isNew={i === 0}
                onSelect={setSelectedEvent}
              />
            ))}
          </div>
        </section>

        <CopilotPanel />
      </div>
      <EventDialog
        event={selectedEvent}
        onClose={() => setSelectedEvent(null)}
      />
    </>
  );
}
