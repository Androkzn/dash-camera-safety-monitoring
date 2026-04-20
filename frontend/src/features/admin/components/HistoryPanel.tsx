/**
 * HistoryPanel — a filterable list of past safety events fetched from
 * /api/live/events (not the live SSE feed).
 *
 * Where it renders:
 *   One of the tabs inside pages/AdminPage.tsx's TabBar (see
 *   ./TabBar.tsx). Unlike DetectionsPanel (which is live), this panel
 *   queries the backend on mount and on filter change.
 *
 * Props:
 *   (none) — this component is self-contained: it owns its own data via
 *   the useHistory() hook. Most of our other admin panels are pure and
 *   prop-driven; this one is a good contrast for teaching "where should
 *   data live?" in React.
 *
 * Visual region:
 *   A filter bar at the top (risk level <select>, event type <select>,
 *   refresh <button>, count) followed by a scrollable list of
 *   <AdminEventCard> items. Handles loading / error / empty states
 *   inline.
 *
 * React concepts demonstrated in this file:
 *   - Consuming a *custom hook* (useHistory) for self-owned state
 *   - `useEffect(fn, [])` — run once after first mount
 *   - Controlled form inputs (<select value={...} onChange={...}>)
 *   - Event handler on DOM events (`e.target.value`)
 *   - Inline style prop (`style={{ color: "var(--high)" }}`)
 *   - Multi-state conditional rendering (loading / error / empty / list)
 *   - List rendering with a stable `key` from backend id
 */

// TEACH: `useEffect` is React's "do a side-effect after render" hook.
//        It runs after the DOM has been updated. The second argument is
//        the *dependency array*: React compares each item to the
//        previous render's values and re-runs the effect only if
//        something changed. `[]` = "run once, after the first mount".
import { useEffect, useState } from "react";

import { EventDialog } from "../../../shared/events";
import type { SafetyEvent } from "../../../shared/types/common";
// TEACH: `useHistory` is a *custom hook* — a plain function whose name
//        starts with `use` and which calls other hooks. Encapsulates
//        "fetch + filter + reload" logic so pages can just consume it.
import { useHistory } from "../hooks/useHistory";
// TEACH: `AdminEventCard` is re-exported from components/events/
//        via its own barrel index. See ../events/index.ts.
import { AdminEventCard } from "./AdminEventCard";
import styles from "./HistoryPanel.module.css";

// --- Render ---

// TEACH: No Props interface because this component takes zero props.
//        That's fine — keep the signature empty.
export function HistoryPanel() {
  // TEACH: The custom hook returns a big object of state + callbacks.
  //        We destructure everything we need in one line. If you want
  //        to know what each field is, open hooks/useHistory.ts.
  const { events, loading, error, filters, updateFilters, refresh } = useHistory();
  // Event-detail modal: clicking any AdminEventCard pops the same dialog
  // the validation page uses, so admins can scrub the annotated clip
  // straight from the history list.
  const [selectedEvent, setSelectedEvent] = useState<SafetyEvent | null>(null);

  // --- Effects ---

  // TEACH: Fire the initial fetch once after mount. Dep array `[]`
  //        means React runs this exactly once per component lifetime.
  //        NOTE: linters often warn about missing deps here — this is
  //        the classic "I really do mean only on mount" exception.
  //        `useEffect` can also return a cleanup function (called on
  //        unmount or before re-running); we don't need one here.
  useEffect(() => {
    refresh();
  }, []);

  return (
    <div>
      {/* --- Filter bar --- */}
      <div className={styles.filterBar}>
        {/* TEACH: Controlled input. React owns the value via
             `value={filters.risk_level}`; every keystroke/change fires
             `onChange`, which updates state, which re-renders with the
             new value. Never leave a controlled <select>'s `value`
             undefined — React will warn. */}
        <select
          className={styles.select}
          value={filters.risk_level}
          // TEACH: `e` is a React.ChangeEvent<HTMLSelectElement>.
          //        `e.target.value` is the selected <option>'s value.
          onChange={(e) => updateFilters({ risk_level: e.target.value })}
        >
          <option value="">All risks</option>
          <option value="high">High only</option>
          <option value="medium">Medium only</option>
          <option value="low">Low only</option>
        </select>
        <select
          className={styles.select}
          value={filters.event_type}
          onChange={(e) => updateFilters({ event_type: e.target.value })}
        >
          <option value="">All types</option>
          <option value="pedestrian_proximity">Pedestrian proximity</option>
          <option value="vehicle_close_interaction">Vehicle interaction</option>
        </select>
        {/* TEACH: Passing the `refresh` function directly to `onClick`.
             We could also write `onClick={() => refresh()}` — the
             difference is that the bare form reuses the same function
             reference across renders (slightly cheaper), while the
             inline arrow form creates a new function every render. For
             a plain DOM <button> that's almost never measurable; it
             matters only when passing callbacks into memoised children
             (React.memo / useCallback territory). */}
        <button className={styles.refreshBtn} onClick={refresh}>
          Refresh
        </button>
        <span className={styles.count}>{events.length} events</span>
      </div>
      {/* --- List + status messages --- */}
      <div className={styles.list}>
        {/* TEACH: Four mutually-exclusive states, rendered with `&&`.
             Order matters: loading wins over error which wins over
             empty which wins over the populated list. */}
        {loading && (
          <div className={styles.empty}>Loading&hellip;</div>
        )}
        {error && (
          // TEACH: Inline `style` prop takes an object (not a string).
          //        Keys are camelCased DOM style names. Using a CSS
          //        variable here (`var(--high)`) so the colour still
          //        comes from the design tokens in index.css.
          <div className={styles.empty} style={{ color: "var(--high)" }}>
            Failed to load: {error}
          </div>
        )}
        {!loading && !error && events.length === 0 && (
          <div className={styles.empty}>No events found</div>
        )}
        {/* TEACH: The list itself. `event_id` comes from the backend
             and is unique per event — an ideal `key`. Avoid index-based
             keys here: the list can reorder when filters change. */}
        {!loading &&
          events.map((ev) => (
            <AdminEventCard
              key={ev.event_id}
              event={ev}
              onSelect={setSelectedEvent}
            />
          ))}
      </div>
      <EventDialog
        event={selectedEvent}
        onClose={() => setSelectedEvent(null)}
      />
    </div>
  );
}
