/**
 * Tabs — minimal accessible tab strip. Pass `tabs` (id + label + content)
 * and an optional `defaultTab` id; Tabs handles the active state.
 *
 * The label can be any ReactNode (string, fragment with a count badge,
 * etc.) so callers don't lose composability.
 */
import { useState, type ReactNode } from "react";

import styles from "./Tabs.module.css";

export interface TabSpec {
  id: string;
  label: ReactNode;
  content: ReactNode;
  disabled?: boolean;
}

interface TabsProps {
  tabs: TabSpec[];
  defaultTab?: string;
  onChange?: (id: string) => void;
  className?: string;
}

export function Tabs({ tabs, defaultTab, onChange, className }: TabsProps) {
  const [active, setActive] = useState<string>(defaultTab ?? tabs[0]?.id ?? "");
  const current = tabs.find((t) => t.id === active) ?? tabs[0];
  const cls = [styles.root, className ?? ""].filter(Boolean).join(" ");
  return (
    <div className={cls}>
      <div role="tablist" className={styles.list}>
        {tabs.map((tab) => {
          const isActive = tab.id === active;
          const tabCls = [styles.tab, isActive ? styles.tabActive : ""]
            .filter(Boolean)
            .join(" ");
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={isActive}
              aria-controls={`tab-panel-${tab.id}`}
              disabled={tab.disabled}
              className={tabCls}
              onClick={() => {
                setActive(tab.id);
                onChange?.(tab.id);
              }}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`tab-panel-${current?.id ?? ""}`}
        className={styles.panel}
      >
        {current?.content}
      </div>
    </div>
  );
}
