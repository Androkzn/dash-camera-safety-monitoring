/**
 * SettingsPage — operator-facing tuning console (orchestrator).
 *
 * Was a 1,564-line god component. Now a thin (~250-line) page that:
 *   - reads settings/templates/impact via feature hooks
 *   - holds the local draft + apply pipeline + dialog flows
 *   - composes <TokenPrompt>, <SettingsHeader>, <ApplyResultBanner>,
 *     <TunablesColumn> on the left, <TemplatesCard>, <BaselineCard>,
 *     <ImpactCard>, <LivePreviewCard> on the right.
 *
 * Layout (responsive):
 *   [TopBar]
 *   ┌─────────────────────────────┬──────────────────┐
 *   │ Page header + apply bar     │ Templates        │
 *   │ Validation / warnings       │ Baseline         │
 *   │ <details> per category      │ Impact           │
 *   │   tunable rows…             │ (Live preview)   │
 *   └─────────────────────────────┴──────────────────┘
 */

import { useEffect, useMemo, useState } from "react";

import {
  type AdminApiError,
  MissingAdminTokenError,
} from "../../shared/lib/adminApi";
import { useAdminToken } from "../../shared/hooks/useAdminToken";
import { useLiveStatus } from "../../shared/hooks/useLiveStatus";
import { TopBar } from "../../shared/layout/TopBar";
import { useDialog } from "../../shared/ui";
import { useLiveSources } from "../admin/hooks/useLiveSources";
import { useDriftCount } from "../validation";
import { useWatchdogCtx } from "../watchdog";

import {
  ApplyResultBanner,
  type ApplyResultPayloadView,
  BaselineCard,
  ImpactCard,
  LivePreviewCard,
  SettingsHeader,
  TemplatesCard,
  TokenPrompt,
  TunablesColumn,
} from "./components";
import { useImpact } from "./hooks/useImpact";
import { useSettings } from "./hooks/useSettings";
import { useSettingsTemplates } from "./hooks/useSettingsTemplates";
import { shortSource } from "./utils/formatting";
import {
  extractValidationErrors,
  isPrivacyConfirmRequired,
} from "./utils/validation";
import type { ApplyResultPayload, DraftValue, SettingSpec } from "./types";

import styles from "./SettingsPage.module.css";

export function SettingsPage() {
  const { token, setToken, clear: clearToken } = useAdminToken();
  const settings = useSettings(token);
  const templates = useSettingsTemplates(token);
  const impact = useImpact(token);
  const { data: live, error: liveError } = useLiveStatus(5000);
  const liveSources = useLiveSources(5000);
  const { status: wdStatus } = useWatchdogCtx();
  const driftCount = useDriftCount();
  const dialog = useDialog();

  const connected: boolean | undefined =
    live ? !!live.running : liveError ? false : undefined;
  const sourceName = live?.source ? shortSource(live.source) : "—";
  const errorCount = wdStatus?.by_severity?.error ?? 0;

  const [draft, setDraft] = useState<Record<string, DraftValue>>({});
  const [validationErrors, setValidationErrors] = useState<
    Array<{ key: string; reason: string }>
  >([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [applyResult, setApplyResult] = useState<ApplyResultPayloadView | null>(
    null,
  );

  // Re-seed the draft from effective values whenever they appear and the
  // operator hasn't started editing.
  useEffect(() => {
    if (settings.effective && Object.keys(draft).length === 0) {
      setDraft(settings.effective.values as Record<string, DraftValue>);
    }
  }, [settings.effective, draft]);

  const dirtyKeys = useMemo(() => {
    if (!settings.effective) return [];
    return Object.keys(draft).filter(
      (k) => draft[k] !== settings.effective!.values[k],
    );
  }, [draft, settings.effective]);

  const errorByKey = useMemo(() => {
    const m: Record<string, string> = {};
    for (const e of validationErrors) m[e.key] = e.reason;
    return m;
  }, [validationErrors]);

  const groupedSpecs = useMemo<Array<[string, SettingSpec[]]>>(() => {
    if (!settings.schema) return [];
    const by: Record<string, SettingSpec[]> = {};
    for (const s of settings.schema.settings) (by[s.category] ??= []).push(s);
    return Object.entries(by);
  }, [settings.schema]);

  async function doApply(opts: { confirmPrivacy?: boolean } = {}) {
    if (!dirtyKeys.length || !settings.effective) return;
    const diff: Record<string, DraftValue> = {};
    const beforeAfter: Record<
      string,
      { before: DraftValue; after: DraftValue }
    > = {};
    for (const k of dirtyKeys) {
      const v = draft[k];
      if (v !== undefined) {
        diff[k] = v;
        beforeAfter[k] = {
          before: settings.effective.values[k] as DraftValue,
          after: v,
        };
      }
    }
    // Structured console log for operators tuning from DevTools.
    // eslint-disable-next-line no-console
    console.groupCollapsed(
      `[settings] apply → ${Object.keys(diff).length} key(s)`,
    );
    // eslint-disable-next-line no-console
    console.table(beforeAfter);
    // eslint-disable-next-line no-console
    console.groupEnd();
    setSubmitting(true);
    setValidationErrors([]);
    setWarnings([]);
    try {
      const res: ApplyResultPayload = await settings.apply(diff, {
        confirm_privacy_change: !!opts.confirmPrivacy,
      });
      setWarnings(res.warnings || []);
      setApplyResult({
        kind: "apply",
        diff: beforeAfter,
        applied_now: res.applied_now || [],
        pending_restart: res.pending_restart || [],
        audit_id: res.audit_id ?? null,
      });
      // eslint-disable-next-line no-console
      console.info("[settings] apply ok", {
        applied_now: res.applied_now,
        pending_restart: res.pending_restart,
        audit_id: res.audit_id,
        warnings: res.warnings,
      });
      setDraft({});
      void impact.refresh();
    } catch (exc) {
      // eslint-disable-next-line no-console
      console.warn("[settings] apply failed", exc);
      const errors = extractValidationErrors(exc);
      if (errors) {
        setValidationErrors(errors);
        return;
      }
      if (isPrivacyConfirmRequired(exc)) {
        const confirmed = await dialog.confirm({
          title: "Privacy-sensitive change",
          message:
            "This change touches a privacy-sensitive setting (ALPR_MODE). " +
            "Toggling License Plate Recognition changes what data leaves the edge — confirm to proceed.",
          okLabel: "Apply with privacy change",
          variant: "warning",
        });
        if (confirmed) {
          await doApply({ confirmPrivacy: true });
        }
        return;
      }
      if (exc instanceof MissingAdminTokenError) return;
      const status = (exc as AdminApiError).status;
      if (status === 409) {
        await dialog.alert({
          title: "Settings changed elsewhere",
          message:
            "Another operator (or another tab) updated the settings since you opened this page. Refreshing the view now.",
          variant: "warning",
        });
        await settings.refresh();
        setDraft({});
        return;
      }
      if (status === 429) {
        await dialog.alert({
          title: "Apply rate-limited",
          message:
            "Too many applies in quick succession. Wait a few seconds and try again.",
          variant: "warning",
        });
        return;
      }
      // eslint-disable-next-line no-console
      console.error(exc);
      await dialog.alert({
        title: "Apply failed",
        message: (exc as Error).message,
        variant: "danger",
      });
    } finally {
      setSubmitting(false);
    }
  }

  async function doRollback() {
    const ok = await dialog.confirm({
      title: "Rollback to last-known-good",
      message:
        "Restore the snapshot that was active immediately before the most recent apply. " +
        "Subscribers (LLM bucket, track-history rebuild, etc.) will re-fire.",
      okLabel: "Rollback",
      variant: "danger",
    });
    if (!ok) return;
    setSubmitting(true);
    try {
      const res = await settings.rollback();
      setWarnings(res.warnings || []);
      setApplyResult({
        kind: "rollback",
        diff: {},
        applied_now: res.applied_now || [],
        pending_restart: res.pending_restart || [],
        audit_id: res.audit_id ?? null,
      });
      // eslint-disable-next-line no-console
      console.info("[settings] rollback ok", {
        applied_now: res.applied_now,
        pending_restart: res.pending_restart,
      });
      setDraft({});
      await impact.refresh();
    } catch (exc) {
      await dialog.alert({
        title: "Rollback failed",
        message: (exc as Error).message,
        variant: "danger",
      });
    } finally {
      setSubmitting(false);
    }
  }

  async function doApplyTemplate(id: string) {
    setSubmitting(true);
    try {
      const res = await templates.applyTemplate(id);
      setWarnings(res.warnings || []);
      setApplyResult({
        kind: "template",
        diff: {},
        applied_now: res.applied_now || [],
        pending_restart: res.pending_restart || [],
        audit_id: res.audit_id ?? null,
      });
      // eslint-disable-next-line no-console
      console.info("[settings] template apply ok", {
        template_id: id,
        applied_now: res.applied_now,
        pending_restart: res.pending_restart,
      });
      setDraft({});
      await impact.refresh();
    } catch (exc) {
      if (isPrivacyConfirmRequired(exc)) {
        const confirmed = await dialog.confirm({
          title: "Template touches privacy setting",
          message:
            "This template changes a privacy-sensitive setting (ALPR_MODE). Confirm to proceed.",
          okLabel: "Apply template",
          variant: "warning",
        });
        if (confirmed) {
          await templates.applyTemplate(id, { confirm_privacy_change: true });
          await settings.refresh();
        }
        return;
      }
      await dialog.alert({
        title: "Apply template failed",
        message: (exc as Error).message,
        variant: "danger",
      });
    } finally {
      setSubmitting(false);
    }
  }

  // -------------------------------------------------------------------------
  // Token-prompt empty state
  // -------------------------------------------------------------------------
  if (!token || settings.needsToken) {
    return (
      <TokenPrompt
        sourceName={sourceName}
        connected={connected}
        errorCount={errorCount}
        driftCount={driftCount}
        error={settings.error}
        onSave={setToken}
      />
    );
  }

  // -------------------------------------------------------------------------
  // Main page
  // -------------------------------------------------------------------------
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
          <SettingsHeader
            dirtyCount={dirtyKeys.length}
            submitting={submitting}
            onDiscard={() => setDraft({})}
            onRollback={doRollback}
            onApply={() => doApply()}
          />

          {validationErrors.length > 0 && (
            <div className={styles.errorList}>
              {validationErrors.map((e) => (
                <div key={`${e.key}:${e.reason}`}>
                  <strong>{e.key}</strong>: {e.reason}
                </div>
              ))}
            </div>
          )}
          {warnings.length > 0 && (
            <div className={styles.warnings}>
              {warnings.map((w) => (
                <div key={w}>{w}</div>
              ))}
            </div>
          )}
          <ApplyResultBanner
            result={applyResult}
            onDismiss={() => setApplyResult(null)}
          />

          {settings.error && !settings.schema && (
            <div className={styles.errorList}>
              <div>
                <strong>Failed to load settings.</strong> {settings.error}
              </div>
              <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
                <button
                  className={styles.btn}
                  onClick={() => void settings.refresh()}
                  disabled={settings.loading}
                >
                  {settings.loading ? "Retrying…" : "Retry"}
                </button>
                <button
                  className={styles.btn}
                  onClick={() => clearToken()}
                  title="Clear the cached admin token and re-prompt"
                >
                  Forget token
                </button>
              </div>
            </div>
          )}

          {settings.schema && settings.effective && (
            <TunablesColumn
              groupedSpecs={groupedSpecs}
              effective={settings.effective}
              draft={draft}
              errorByKey={errorByKey}
              onChange={(key, value) =>
                setDraft((prev) => ({ ...prev, [key]: value }))
              }
            />
          )}

          {!settings.schema && !settings.error && (
            <p className={styles.subtle}>
              {settings.loading ? "Loading settings…" : "Settings not loaded yet."}
            </p>
          )}
        </section>

        <aside className={styles.right}>
          <TemplatesCard
            templates={templates.templates}
            busy={submitting}
            onApply={doApplyTemplate}
            onCreate={async (name, description) => {
              if (!settings.effective) return;
              await templates.create(name, description, settings.effective.values);
            }}
            onDelete={async (id) => templates.remove(id)}
          />
          <BaselineCard onCaptured={() => impact.refresh()} />
          <ImpactCard
            report={impact.report}
            refreshing={impact.refreshing}
            lastUpdatedTs={impact.lastUpdatedTs}
            onRefresh={() => impact.refresh()}
          />
          <LivePreviewCard
            sources={liveSources.sources}
            primaryId={liveSources.primaryId}
            fallbackSourceName={sourceName}
            targetFps={live?.target_fps}
          />
        </aside>
      </main>
    </>
  );
}
