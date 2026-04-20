/**
 * HealthStrip — a single horizontal row of compact "status cells" that
 * summarise pipeline & integration health at a glance.
 *
 * Where it renders:
 *   Top of pages/AdminPage.tsx, above the video feed. Meant to be the
 *   first thing an operator looks at: "is everything green?".
 *
 * Props:
 *   - health: HealthData | null  (required)
 *       The full health payload fetched from /api/live/status (see
 *       hooks/useHealth.ts or the AdminPage fetch). `null` means we
 *       haven't received a reply yet — we render a skeleton row of em
 *       dashes so the layout doesn't jump once data arrives.
 *
 * Visual region:
 *   A strip of ~10 cells. Each cell has a label, a big value, an
 *   optional sub-text, and a colour variant (ok/warn/err/accent). Order
 *   mirrors the processing pipeline: Stream -> Frames -> Events ->
 *   Perception -> Scene -> Tracker -> LLM -> Slack -> Edge Pub -> PII.
 *
 * React concepts demonstrated in this file:
 *   - TWO functional components in one file (HealthCell is an internal
 *     helper; only HealthStrip is exported)
 *   - Typed Props interfaces, including OPTIONAL props (`sub?`,
 *     `variant?`) and a default value via destructuring (`variant = ""`)
 *   - Union-literal types for the `variant` prop — TypeScript forces
 *     callers to pick from the allowed set
 *   - Object destructuring with renames inside the component body
 *   - Conditional CSS class composition
 *   - Nullish coalescing (`??`), Array.filter(Boolean) + join for
 *     building sub-text strings
 */

import type { HealthData } from "../../../shared/types/common";
import styles from "./HealthStrip.module.css";

// --- Types ---

// TEACH: `variant` is a *string literal union* — the type system will
//        only let a caller pass one of these exact strings. Autocomplete
//        in the editor will also suggest them. The trailing `""` lets
//        us express "no variant / default styling".
interface HealthCellProps {
  label: string;
  value: string;
  // TEACH: The `?` after the name makes the prop OPTIONAL. Inside the
  //        function, `sub` will have type `string | undefined`.
  sub?: string;
  variant?: "ok" | "warn" | "err" | "accent" | "";
}

// --- Internal component ---

// TEACH: This is a "private" sub-component — not exported, used only
//        inside this file. Splitting repeated markup into a tiny helper
//        keeps the main HealthStrip render readable.
// TEACH: `variant = ""` is a default value. If the caller omits
//        `variant`, it becomes `""` inside the function.
function HealthCell({ label, value, sub, variant = "" }: HealthCellProps) {
  return (
    <div className={styles.hg}>
      <div className={styles.label}>{label}</div>
      {/* TEACH: Dynamic class composition. When `variant` is "ok" this
           becomes `"<hash>val <hash>ok"`; when it's "" we append an
           empty string so we never emit a stray "undefined" class. */}
      <div className={`${styles.val} ${variant ? styles[variant] : ""}`}>{value}</div>
      {/* TEACH: `sub ?? "\u00a0"` — nullish coalescing. If `sub` is
           null/undefined, fall back to a non-breaking space so the cell
           keeps its vertical rhythm even when there is no sub-text. */}
      <div className={styles.sub}>{sub ?? "\u00a0"}</div>
    </div>
  );
}

// --- Exported component ---

interface HealthStripProps {
  // TEACH: `| null` makes the component aware of the "no data yet"
  //        state. We branch on it below to render a placeholder.
  health: HealthData | null;
}

export function HealthStrip({ health }: HealthStripProps) {
  // TEACH: Loading-state branch. Returning a placeholder shape that
  //        mirrors the real layout prevents the page from jumping when
  //        the fetch resolves — a classic React UX pattern.
  if (!health) {
    return (
      <div className={styles.strip}>
        <HealthCell label="Stream" value="—" />
        <HealthCell label="Frames" value="—" variant="accent" />
        <HealthCell label="Events" value="0" />
        <HealthCell label="Perception" value="—" />
        <HealthCell label="Scene" value="—" />
        <HealthCell label="Tracker" value="—" variant="accent" />
        <HealthCell label="LLM" value="—" />
        <HealthCell label="Slack" value="—" />
        <HealthCell label="Edge Pub" value="—" />
        <HealthCell label="PII" value="—" variant="ok" />
      </div>
    );
  }

  // TEACH: Destructuring with RENAMES. `server: srv` means "take the
  //        `server` field and bind it to a local variable named `srv`".
  //        Pure ergonomics — shorter names in the JSX below.
  const { server: srv, pipeline: pip, integrations: intg, perception: perc, scene: sc } = health;

  return (
    <div className={styles.strip}>
      {/* TEACH: Each HealthCell call is a prop-driven render. The
           `variant` prop is computed with a ternary on the backing
           boolean — green when healthy, red when down. */}
      <HealthCell
        label="Stream"
        value={srv.running ? "Active" : "Down"}
        variant={srv.running ? "ok" : "err"}
        sub={(srv.source || "").substring(0, 50)}
      />
      <HealthCell
        label="Frames"
        // TEACH: `.toLocaleString()` adds thousands separators for the
        //        operator's locale — "12,345" instead of "12345".
        value={pip.frames_processed.toLocaleString()}
        variant="accent"
        sub={`${pip.frames_read.toLocaleString()} read / ${srv.target_fps} fps target`}
      />
      <HealthCell
        label="Events"
        value={String(pip.event_count)}
        variant={pip.event_count > 0 ? "accent" : ""}
        // TEACH: Tiny inline pluralisation — no i18n lib needed for
        //        english-only admin UIs.
        sub={`${pip.active_episodes} active episode${pip.active_episodes !== 1 ? "s" : ""}`}
      />
      <HealthCell
        label="Perception"
        value={perc.state}
        variant={perc.state === "nominal" ? "ok" : "warn"}
        // TEACH: Common idiom for "join the non-empty parts with a
        //        separator, else fall back". Build an array, drop
        //        empty strings with `.filter(Boolean)`, then `.join`.
        sub={
          [
            perc.avg_confidence != null ? `conf ${Number(perc.avg_confidence).toFixed(2)}` : "",
            perc.luminance != null ? `lum ${Math.round(perc.luminance)}` : "",
          ]
            .filter(Boolean)
            .join(" / ") || perc.reason
        }
      />
      <HealthCell
        label="Scene"
        value={sc.label}
        variant="accent"
        sub={
          [
            sc.speed_proxy_mps != null ? `~${sc.speed_proxy_mps} m/s` : "",
            sc.reason || "",
          ]
            .filter(Boolean)
            .join(" / ")
        }
      />
      <HealthCell
        label="Tracker"
        value={pip.tracker}
        variant="accent"
        sub={pip.risk_model}
      />
      <HealthCell
        label="LLM"
        value={intg.llm_configured ? "On" : "Off"}
        variant={intg.llm_configured ? "ok" : ""}
      />
      <HealthCell
        label="Slack"
        value={intg.slack_configured ? "On" : "Off"}
        variant={intg.slack_configured ? "ok" : ""}
      />
      <HealthCell
        label="Edge Pub"
        value={intg.edge_publisher ? "On" : "Off"}
        variant={intg.edge_publisher ? "ok" : ""}
      />
      <HealthCell
        label="PII"
        value={intg.pii_redaction}
        variant="ok"
      />
    </div>
  );
}
