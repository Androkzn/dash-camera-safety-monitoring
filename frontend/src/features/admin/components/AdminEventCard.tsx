/**
 * AdminEventCard — compact card representing one SafetyEvent on the admin
 * screen. Shown in AdminPage's live event list (see frontend/src/pages/AdminPage.tsx).
 *
 * There are two variants of event cards:
 *   - EventCard       (./EventCard.tsx)       — public-facing, bigger, includes
 *                                               FeedbackButtons and enrichment.
 *   - AdminEventCard  (this file)             — denser, admin-only; no feedback
 *                                               buttons, shows raw track IDs.
 *
 * Props:
 *   - event: a SafetyEvent object (see ../../types.ts) pushed from the SSE
 *            stream in AdminPage.
 *
 * React concepts first taught here:
 *   - Functional component with typed Props.
 *   - Destructuring a prop with a RENAME: `{ event: e }` pulls `props.event`
 *     and binds it locally to `e` (a one-letter alias used heavily below).
 *   - Child components (`RiskBadge`, `Tag`) receiving props.
 *   - Conditional rendering via `&&` and the ternary `cond ? a : b`.
 *   - Inline event handler on <img onError> with a DOM escape hatch.
 *   - CSS Modules styling via `styles.someClass`.
 *
 * NOTE: This component is purely presentational — it owns no state and calls
 *       no APIs. Parents are responsible for feeding it events.
 */

import type { SafetyEvent } from "../../../shared/types/common";
import { RiskBadge, Tag } from "../../../shared/ui";
import { formatWallTime, humanEventType, normalizeThumbnail } from "../../../shared/lib/format";
import styles from "./AdminEventCard.module.css";
// TEACH: `import type { ... }` imports ONLY the TypeScript type. The compiler
// erases it at build time (no runtime cost). Use this for pure-type imports.

// --- Types ---

// TEACH: The props interface is the component's contract. Parents must supply
// `event`; TypeScript will flag them if they forget.
interface AdminEventCardProps {
  event: SafetyEvent;
  // When supplied, the whole card becomes a button that opens the parent's
  // event-detail dialog. Optional so the card stays a pure presentational
  // row anywhere it's shown without a dialog (e.g. compact reports).
  onSelect?: (event: SafetyEvent) => void;
}

// --- Render ---

// TEACH: `{ event: e }` is destructuring-with-rename. The prop is named `event`
// on the outside, but inside the function body we call it `e`. This is only a
// local alias; callers still pass `<AdminEventCard event={...} />`.
// TEACH: Map camera_orientation → display label. For "side" we try to
// disambiguate left vs right from the source name/id substring; fall back to
// "SIDE" when it's ambiguous.
function orientationLabel(e: SafetyEvent): string | null {
  const o = e.camera_orientation;
  if (!o) return null;
  if (o === "forward") return "FRONT";
  if (o === "rear") return "REAR";
  if (o === "side") {
    const hay = `${e.source_name ?? ""} ${e.source_id ?? ""}`.toLowerCase();
    if (hay.includes("left")) return "LEFT";
    if (hay.includes("right")) return "RIGHT";
    return "SIDE";
  }
  return o.toUpperCase();
}

export function AdminEventCard({ event: e, onSelect }: AdminEventCardProps) {
  // Pre-compute display values once. Keeping them above the return keeps the
  // JSX below cleaner.
  const thumb = normalizeThumbnail(e.thumbnail);
  // TEACH: `||` gives the first truthy operand — a handy fallback when you
  // accept either of two optional string fields.
  const narr = e.narration || e.summary || "";
  const orientation = orientationLabel(e);
  const taxonomy = e.event_taxonomy && e.event_taxonomy !== "NONE" ? e.event_taxonomy : null;
  // Map taxonomy family → CSS class for a subtle per-family tint.
  const taxonomyClass = taxonomy
    ? styles[`taxonomy_${taxonomy}`] ?? styles.taxonomyBadge
    : "";

  // Card is keyboard-accessible only when an onSelect handler is wired —
  // otherwise it stays a plain div so screen-readers don't announce a
  // useless button.
  const interactive = typeof onSelect === "function";
  const cardClass = interactive ? `${styles.card} ${styles.interactive ?? ""}`.trim() : styles.card;

  return (
    <div
      className={cardClass}
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={interactive ? () => onSelect?.(e) : undefined}
      onKeyDown={
        interactive
          ? (ke) => {
              if (ke.key === "Enter" || ke.key === " ") {
                ke.preventDefault();
                onSelect?.(e);
              }
            }
          : undefined
      }
      aria-label={interactive ? `Open details for ${humanEventType(e.event_type)}` : undefined}
    >
      {/* Thumbnail area. */}
      <div className={styles.thumb}>
        {/* TEACH: Ternary for "either-or" rendering. If there's a thumbnail URL,
            render <img/>; otherwise render an em-dash placeholder. */}
        {thumb ? (
          <img
            src={thumb}
            alt=""
            // TEACH: `onError` fires when the browser fails to load the image.
            // We cast the event target to HTMLImageElement, then replace the
            // parent's text content so a broken image becomes a simple dash.
            // This is an "escape hatch" — direct DOM manipulation inside React.
            // Prefer state-driven fallbacks when you can; this one is fine
            // because it's purely cosmetic and local to this card.
            onError={(ev) => {
              (ev.target as HTMLImageElement).parentElement!.textContent = "—";
            }}
          />
        ) : (
          "—"
        )}
      </div>
      {/* Right-hand info column: risk, type, time, kinematic tags, narration. */}
      <div className={styles.info}>
        <div className={styles.top}>
          {/* TEACH: Child components receive data via props just like HTML
              attributes. `compact` is a boolean prop (shorthand for compact={true}). */}
          <RiskBadge level={e.risk_level} compact />
          {orientation && (
            <span className={styles.orientationBadge}>{orientation}</span>
          )}
          {taxonomy && (
            <span className={`${styles.taxonomyBadge} ${taxonomyClass}`}>
              {taxonomy}
            </span>
          )}
          <span className={styles.type}>{humanEventType(e.event_type)}</span>
          <span className={styles.time}>{formatWallTime(e.wall_time)}</span>
        </div>
        {/* Kinematic metadata row (time-to-collision, distance, track IDs, duration). */}
        <div className={styles.metaRow}>
          {/* TEACH: `e.ttc_sec != null` checks for both `null` and `undefined`
              in one go. Using `!=` rather than `!==` is intentional here. */}
          {e.ttc_sec != null && (
            <Tag variant={e.ttc_sec <= 1.5 ? "kin-warn" : "kin"}>
              TTC {Number(e.ttc_sec).toFixed(1)}s
            </Tag>
          )}
          {e.distance_m != null && (
            <Tag variant="kin">{Number(e.distance_m).toFixed(1)}m</Tag>
          )}
          {e.distance_px != null && <Tag>{Math.round(e.distance_px)}px</Tag>}
          {/* TEACH: `arr?.length` uses optional chaining — returns `undefined`
              if `arr` itself is undefined, else the length. Combined with `?`
              ternary, this avoids "cannot read length of undefined". */}
          {e.track_ids?.length ? <Tag>#{e.track_ids.join("/")}</Tag> : null}
          {e.episode_duration_sec != null && (
            <Tag>ep {Number(e.episode_duration_sec).toFixed(1)}s</Tag>
          )}
        </div>
        {/* Narration / summary line, shown only when one of them exists. */}
        {narr && (
          <div className={styles.narr} title={narr}>
            {narr}
          </div>
        )}
      </div>
    </div>
  );
}
