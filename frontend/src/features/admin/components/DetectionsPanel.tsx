/**
 * DetectionsPanel — a scrollable list of per-frame detection snapshots
 * coming from the edge perception pipeline.
 *
 * Where it renders:
 *   Rendered inside pages/AdminPage.tsx, usually as one of the tabs in a
 *   TabBar (see ./TabBar.tsx). The parent owns the frame buffer and passes
 *   the latest N snapshots down via props.
 *
 * Props:
 *   - frames: DetectionSnapshot[]  (required)
 *       A list of per-frame snapshots produced by the SSE stream hook in
 *       AdminPage.tsx (see hooks/useLiveStream.ts). Each DetectionSnapshot
 *       carries a timestamp, per-frame counters (detections/interactions),
 *       and an `objects` array of YOLO+ByteTrack detections.
 *
 * Visual region:
 *   Produces a vertical list. Each list item is a "frame group" that
 *   shows the frame's clock time + summary line, followed by one row per
 *   detected object (class, confidence, track id, bbox size).
 *   When `frames` is empty the panel renders a single placeholder line.
 *
 * React concepts demonstrated in this file:
 *   - Functional component with a typed Props interface
 *   - Destructured props in the function signature
 *   - Early `return` for a conditional "empty state" render
 *   - List rendering with `.map()` and the required `key` prop
 *   - Nested list rendering (frames -> objects inside each frame)
 *   - Inline conditional rendering with the `&&` short-circuit
 *   - CSS Modules (`styles.foo`) + template-string class composition
 *   - Pure component: no state, no effects — output is a function of props
 */

// TEACH: `import type { ... }` imports ONLY TypeScript types. It is erased
//        at build time and produces zero JS. Use it for types/interfaces
//        so the bundler knows not to emit a runtime import.
import type { DetectionSnapshot } from "../../../shared/types/common";
// TEACH: CSS Modules. `styles` is an object where each key is the class
//        name you wrote in DetectionsPanel.module.css, and the value is a
//        *hashed* class string (e.g. "DetectionsPanel_list__a3f9c").
//        That hashing is what scopes styles to this component — you can
//        safely reuse a class name like `.list` in another module.css
//        file without collisions.
import styles from "./DetectionsPanel.module.css";

// --- Types ---

// TEACH: A Props interface declares the shape of the data this component
//        accepts. TypeScript will red-underline callers that forget a
//        required prop or pass the wrong type. Convention: name it
//        `<ComponentName>Props`.
interface DetectionsPanelProps {
  frames: DetectionSnapshot[];
}

// --- Render ---

// TEACH: Functional component = a plain function whose return value is
//        JSX (which compiles to React.createElement calls). The argument
//        is "props"; here we destructure `{ frames }` right in the
//        parameter list, and annotate it with our Props interface.
export function DetectionsPanel({ frames }: DetectionsPanelProps) {
  // TEACH: Conditional rendering via early return. If there are no
  //        frames yet, we short-circuit and render a placeholder. React
  //        is happy returning *any* single JSX element (or null) from a
  //        component — no need for a top-level wrapper if you return
  //        early.
  if (frames.length === 0) {
    return <div className={styles.empty}>Waiting for detections&hellip;</div>;
  }

  return (
    // TEACH: The outer <div> is the "list container". Parent's CSS Grid
    //        / flex in AdminPage gives it a fixed height + scroll.
    <div className={styles.list}>
      {/* TEACH: `.map()` turns an array of data into an array of JSX
           elements. React renders arrays of elements inline. Every
           element in the array MUST have a stable, unique `key` prop so
           React can tell items apart between renders (this is called
           "reconciliation"). Avoid using the array index alone when the
           list can reorder — here we combine `frame.ts` with `i` to get
           both stability and uniqueness in the degenerate case where
           two frames share a timestamp. */}
      {frames.map((frame, i) => {
        // TEACH: The callback passed to `.map` can do arbitrary
        //        per-item work before returning JSX. We compute a
        //        human-readable time string up front to keep the JSX
        //        below clean.
        const ts = frame.ts ? new Date(frame.ts * 1000) : new Date();
        const tStr = isNaN(ts.getTime())
          ? "—"
          : `${String(ts.getHours()).padStart(2, "0")}:${String(ts.getMinutes()).padStart(2, "0")}:${String(ts.getSeconds()).padStart(2, "0")}`;

        return (
          // TEACH: First (outer) `key` — unique among sibling frame
          //        groups. Template literal combines two values so the
          //        result is stable across re-renders of the same list.
          <div className={styles.frameGroup} key={`${frame.ts}-${i}`}>
            {/* TEACH: JSX-internal comments use `{/* ... *\/}` form.
                 This block is the per-frame header row. */}
            <div className={styles.frameHdr}>
              <span>{tStr}</span>
              <span>
                {frame.detections} det / {frame.interactions} int
              </span>
            </div>
            {/* TEACH: Nested `.map()` — one per detected object inside
                 this frame. Each nested list also needs its own `key`
                 that is unique among its siblings (not globally). */}
            {frame.objects.map((o, j) => {
              const isPerson = o.cls === "person";
              // TEACH: Derived values (width/height from a bbox tuple)
              //        computed per-render. React re-runs the component
              //        body on every render, so keep per-item math cheap
              //        or memoize upstream.
              const w = o.bbox[2] - o.bbox[0];
              const h = o.bbox[3] - o.bbox[1];

              return (
                // TEACH: Prefer a stable id (here `track_id` from the
                //        ByteTrack tracker) over array index. We fall
                //        back to the index `j` with the `??` nullish
                //        coalescing operator when `track_id` is null.
                <div className={styles.detRow} key={`${o.track_id ?? j}-${j}`}>
                  {/* TEACH: Classname composition with a template
                       string. Combining a base class with a
                       conditional modifier class is the CSS-Modules
                       equivalent of the `classnames` library. */}
                  <span
                    className={`${styles.detCls} ${isPerson ? styles.person : styles.vehicle}`}
                  >
                    {o.cls}
                  </span>
                  <span className={styles.detConf}>
                    {(o.conf * 100).toFixed(0)}%
                  </span>
                  {/* TEACH: `{cond && <X/>}` — the `&&` short-circuit
                       renders <X/> only when `cond` is truthy.
                       IMPORTANT gotcha: if `cond` is the number 0 React
                       will render "0" instead of nothing. Using an
                       explicit `!= null` comparison (as here) avoids
                       that. */}
                  {o.track_id != null && (
                    <span className={styles.detTrack}>#{o.track_id}</span>
                  )}
                  {o.distance_m != null && (
                    <span
                      className={styles.detDist}
                      // Side-window cams report a sideways gap, not a forward
                      // range — make the semantic explicit so operators don't
                      // read a "3.0 m" left-cam reading as "3 m ahead". The
                      // chip text adds " lat" suffix; the tooltip spells it
                      // out for the unfamiliar.
                      title={
                        o.distance_axis === "lateral"
                          ? "Lateral proximity — distance to the side, not ahead"
                          : "Longitudinal range — distance from the host vehicle's nearest body edge"
                      }
                    >
                      {o.distance_m.toFixed(1)} m
                      {o.distance_axis === "lateral" ? " lat" : ""}
                    </span>
                  )}
                  <span className={styles.detBbox}>
                    {w}×{h}
                  </span>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}
