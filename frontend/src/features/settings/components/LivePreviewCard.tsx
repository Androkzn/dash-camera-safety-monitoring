/**
 * LivePreviewCard — collapsed-by-default video preview on the right rail.
 * Defaults to the stream the operator focused on the Admin page; the
 * <select> lets them preview any active source.
 */

import { useEffect, useState } from "react";

import { StreamImage } from "../../admin/components/StreamImage";
import type { LiveSourceStatus } from "../../../shared/types/common";

import styles from "../SettingsPage.module.css";

interface LivePreviewCardProps {
  sources: LiveSourceStatus[];
  primaryId: string | null;
  fallbackSourceName: string;
  targetFps?: number;
}

export function LivePreviewCard({
  sources,
  primaryId,
  fallbackSourceName,
  targetFps,
}: LivePreviewCardProps) {
  const [previewSourceId, setPreviewSourceId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return window.localStorage.getItem("road_admin_focused_id");
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    const onChange = () => {
      setPreviewSourceId(window.localStorage.getItem("road_admin_focused_id"));
    };
    window.addEventListener("admin-focused-id-changed", onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener("admin-focused-id-changed", onChange);
      window.removeEventListener("storage", onChange);
    };
  }, []);

  const previewSource =
    sources.find((s) => s.id === previewSourceId) ??
    sources.find((s) => s.id === primaryId) ??
    sources[0] ??
    null;

  return (
    <details className={`${styles.card} ${styles.videoCard}`}>
      <summary className={styles.cardTitle}>Live preview</summary>
      {sources.length > 1 && (
        <select
          value={previewSource?.id ?? ""}
          onChange={(e) => setPreviewSourceId(e.target.value || null)}
          aria-label="Preview source"
          style={{ marginBottom: 8, width: "100%" }}
        >
          {sources.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
              {s.id === primaryId ? " (primary)" : ""}
            </option>
          ))}
        </select>
      )}
      {previewSource ? (
        <StreamImage
          source={previewSource}
          onError={() => {
            /* swallow — next poll / reconnect will recover */
          }}
        />
      ) : (
        <span className={styles.subtle}>No active source.</span>
      )}
      <span className={styles.subtle}>
        source: {previewSource?.name ?? fallbackSourceName}
        {targetFps ? ` @ ${targetFps}fps` : ""}
      </span>
    </details>
  );
}
