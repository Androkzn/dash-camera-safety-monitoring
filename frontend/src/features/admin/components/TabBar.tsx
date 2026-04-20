/**
 * TabBar — a small, generic tab switcher. Given a list of
 * `{ id, label, content }` tabs it renders a button row at the top and
 * shows the active tab's content below.
 *
 * Where it renders:
 *   Used inside pages/AdminPage.tsx to swap between DetectionsPanel,
 *   HistoryPanel, etc. without re-routing.
 *
 * Props:
 *   - tabs: Tab[]           (required) — the tabs to display
 *   - defaultTab?: string   (optional) — id of the tab to show first.
 *                            Falls back to `tabs[0].id` if omitted.
 *
 * Visual region:
 *   Two stacked sections: (1) the button bar and (2) the content
 *   panels. All panels are kept in the DOM; only the `activePanel`
 *   class is toggled, so CSS controls visibility. That preserves
 *   scroll position and component state inside inactive tabs (useful
 *   for expensive live data panels).
 *
 * React concepts demonstrated in this file:
 *   - `useState` for local UI state — `[value, setValue] = useState(...)`
 *   - `ReactNode` — the type for "anything renderable" (string, JSX,
 *     number, array, null). Lets the caller pass rich labels/content.
 *   - Event handler capturing a loop variable (`() => setActive(tab.id)`)
 *   - List rendering where the same array is mapped TWICE (buttons and
 *     panels) — each needs its own `key`s
 */

// TEACH: `type ReactNode` — importing just the type from React. Using
//        `type` keeps the import erased at runtime.
import { useState, type ReactNode } from "react";
import styles from "./TabBar.module.css";

// --- Types ---

interface Tab {
  id: string;
  // TEACH: `ReactNode` = "anything React can render". So a tab label
  //        can be `"History"`, `<>{icon} History</>`, `123`, etc.
  label: ReactNode;
  content: ReactNode;
}

interface TabBarProps {
  tabs: Tab[];
  // TEACH: Optional (note the `?`). When the consumer omits it we
  //        fall back to the first tab's id below.
  defaultTab?: string;
}

// --- Render ---

export function TabBar({ tabs, defaultTab }: TabBarProps) {
  // --- State ---

  // TEACH: `useState` returns a tuple: [current value, setter]. React
  //        preserves this value across re-renders; calling the setter
  //        schedules a re-render with the new value. The argument is
  //        the INITIAL value (only read on the very first render).
  // TEACH: `defaultTab ?? tabs[0]?.id ?? ""` — if the caller gave us
  //        a default use it, else fall back to the first tab's id
  //        (using optional chaining `?.` in case `tabs` is empty),
  //        else an empty string.
  const [active, setActive] = useState(defaultTab ?? tabs[0]?.id ?? "");

  return (
    <div className={styles.container}>
      {/* --- Button bar --- */}
      <div className={styles.bar}>
        {tabs.map((tab) => (
          // TEACH: `type="button"` prevents the <button> from
          //        defaulting to `type="submit"` inside any ancestor
          //        <form>. Safe habit in generic components.
          <button
            key={tab.id}
            // TEACH: Active class is toggled by comparing state to the
            //        item's id. Template-string composition.
            className={`${styles.tab} ${active === tab.id ? styles.active : ""}`}
            // TEACH: Inline arrow handler — captures `tab.id` via
            //        closure. New function each render (fine for a
            //        plain DOM button; matters only when passing
            //        callbacks into memoised children).
            onClick={() => setActive(tab.id)}
            type="button"
          >
            {tab.label}
          </button>
        ))}
      </div>
      {/* --- Panels --- */}
      {/* TEACH: We render ALL panels, and let CSS hide the inactive
           ones (via the `activePanel` class). Alternative would be
           `{tabs.find(...)?.content}` which unmounts inactive panels —
           that resets their internal state each time you switch. Keep
           this render-all approach when inactive panels are expensive
           to rebuild (e.g. live streams, long lists). */}
      {tabs.map((tab) => (
        <div
          key={tab.id}
          className={`${styles.panel} ${active === tab.id ? styles.activePanel : ""}`}
        >
          {tab.content}
        </div>
      ))}
    </div>
  );
}
