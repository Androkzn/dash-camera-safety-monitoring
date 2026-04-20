/**
 * TemplatesCard — list saved settings templates + "Save current as template" form.
 */

import { useState } from "react";

import { useDialog } from "../../../shared/ui";
import type { SettingsTemplate } from "../types";

import styles from "../SettingsPage.module.css";

interface TemplatesCardProps {
  templates: SettingsTemplate[];
  busy: boolean;
  onApply: (id: string) => Promise<void>;
  onCreate: (name: string, description: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}

export function TemplatesCard({
  templates,
  busy,
  onApply,
  onCreate,
  onDelete,
}: TemplatesCardProps) {
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [creating, setCreating] = useState(false);
  const dialog = useDialog();

  return (
    <div className={styles.card}>
      <div className={styles.cardHeader}>
        <h3 className={styles.cardTitle}>Templates ({templates.length})</h3>
      </div>
      <div className={styles.templateList}>
        {templates.map((t) => (
          <div key={t.id} className={styles.templateItem}>
            <div className={styles.templateRow}>
              <div>
                <span className={styles.templateName}>{t.name}</span>
                {t.system && (
                  <span
                    className={`${styles.badge} ${styles.badgeReadonly}`}
                    style={{ marginLeft: 6 }}
                  >
                    system
                  </span>
                )}
              </div>
              <span className={styles.subtle}>r{t.latest_revision_no}</span>
            </div>
            {t.description && (
              <span className={styles.templateDesc}>{t.description}</span>
            )}
            <div className={styles.templateActions}>
              <button
                className={styles.btn}
                disabled={busy}
                onClick={() => onApply(t.id)}
              >
                Apply
              </button>
              {!t.system && (
                <button
                  className={`${styles.btn} ${styles.btnDanger}`}
                  disabled={busy}
                  onClick={async () => {
                    const ok = await dialog.confirm({
                      title: "Delete template",
                      message: `Soft-delete template "${t.name}"? Existing impact sessions that reference it stay intact.`,
                      okLabel: "Delete",
                      variant: "danger",
                    });
                    if (ok) onDelete(t.id);
                  }}
                >
                  Delete
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      <details>
        <summary className={styles.subtle} style={{ cursor: "pointer" }}>
          + Save current as template
        </summary>
        <div
          style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}
        >
          <input
            className={styles.tokenInput}
            placeholder="Template name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            className={styles.tokenInput}
            placeholder="Description (optional)"
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
          />
          <button
            className={`${styles.btn} ${styles.btnPrimary}`}
            disabled={!name.trim() || creating}
            onClick={async () => {
              setCreating(true);
              try {
                await onCreate(name.trim(), desc.trim());
                setName("");
                setDesc("");
              } finally {
                setCreating(false);
              }
            }}
          >
            {creating ? "Saving…" : "Save"}
          </button>
        </div>
      </details>
    </div>
  );
}
