/**
 * Tunable — compound component for one tunable row.
 *
 * The original SettingsPage shipped a single ~200-line `TunableControl`
 * that hard-coded the slider+number+label+validation+help+reset combo.
 * Splitting it into compound parts gives callers a "use only what you
 * need" composition surface and makes each part independently testable.
 *
 * Default usage (matches the legacy layout):
 *
 *   <Tunable spec={spec} draft={draft} effective={eff} onChange={…} errorReason={…}>
 *     <Tunable.Label />
 *     <Tunable.Control />
 *     <Tunable.Meta />
 *   </Tunable>
 *
 * Each part reads context — the parent supplies it once.
 */

import {
  Fragment,
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { TUNABLE_HELP } from "../constants";
import type { DraftValue, SettingSpec } from "../types";
import { humanize } from "../utils/formatting";
import { stepFor } from "../utils/steps";

import styles from "../SettingsPage.module.css";

interface TunableContextValue {
  spec: SettingSpec;
  effective: DraftValue;
  draft: DraftValue;
  errorReason: string | null;
  onChange: (v: DraftValue) => void;
}

const TunableContext = createContext<TunableContextValue | null>(null);

function useTunable(): TunableContextValue {
  const ctx = useContext(TunableContext);
  if (!ctx) {
    throw new Error("Tunable.* parts must be rendered inside <Tunable>");
  }
  return ctx;
}

interface TunableRootProps extends TunableContextValue {
  children?: ReactNode;
}

export function Tunable(props: TunableRootProps) {
  const { children, ...ctx } = props;
  const dirty = ctx.draft !== ctx.effective;
  const cls = [
    styles.tunable,
    dirty ? styles.dirty : "",
    ctx.errorReason ? styles.error : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <TunableContext.Provider value={ctx}>
      <div className={cls}>
        {children ?? (
          <Fragment>
            <Label />
            <Control />
            <Meta />
          </Fragment>
        )}
      </div>
    </TunableContext.Provider>
  );
}

// ---------------------------------------------------------------------------
// Label + integrated help popover
// ---------------------------------------------------------------------------

function Label() {
  const { spec, errorReason } = useTunable();
  const help = TUNABLE_HELP[spec.key];

  const [helpOpen, setHelpOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [popoverPos, setPopoverPos] = useState<{ top: number; left: number } | null>(
    null,
  );

  useEffect(() => {
    if (!helpOpen) return;
    const onDown = (e: MouseEvent) => {
      const tgt = e.target as Node | null;
      if (
        tgt &&
        !popoverRef.current?.contains(tgt) &&
        !triggerRef.current?.contains(tgt)
      ) {
        setHelpOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setHelpOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [helpOpen]);

  useLayoutEffect(() => {
    if (!helpOpen) {
      setPopoverPos(null);
      return;
    }
    const POPOVER_W = 340;
    const POPOVER_H_EST = 220;
    const update = () => {
      const trigger = triggerRef.current;
      if (!trigger) return;
      const r = trigger.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      let top = r.bottom + 10;
      let left = r.left - 10;
      if (left + POPOVER_W > vw - 8) left = Math.max(8, vw - POPOVER_W - 8);
      if (top + POPOVER_H_EST > vh - 8)
        top = Math.max(8, r.top - POPOVER_H_EST - 10);
      setPopoverPos({ top, left });
    };
    update();
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [helpOpen]);

  return (
    <div className={styles.keyCol}>
      <span className={styles.keyLabel} title={spec.key}>
        {humanize(spec.key)}
        {help && (
          <span className={styles.helpAnchor}>
            <button
              ref={triggerRef}
              type="button"
              className={styles.infoBtn}
              aria-label={`More info about ${humanize(spec.key)}`}
              aria-expanded={helpOpen}
              onClick={() => setHelpOpen((o) => !o)}
            >
              i
            </button>
            {helpOpen && popoverPos && (
              <div
                ref={popoverRef}
                className={styles.helpPopover}
                role="dialog"
                style={{ top: popoverPos.top, left: popoverPos.left }}
              >
                <button
                  type="button"
                  className={styles.helpClose}
                  aria-label="Close"
                  onClick={() => setHelpOpen(false)}
                >
                  ×
                </button>
                <h4 className={styles.helpTitle}>{humanize(spec.key)}</h4>
                <p>
                  <strong>What it is.</strong> {help.what}
                </p>
                <p>
                  <strong>Affects.</strong> {help.affects}
                </p>
                {help.increase && (
                  <p>
                    <strong>↑ Increasing.</strong> {help.increase}
                  </p>
                )}
                {help.decrease && (
                  <p>
                    <strong>↓ Decreasing.</strong> {help.decrease}
                  </p>
                )}
                {help.options && (
                  <ul className={styles.helpOptions}>
                    {Object.entries(help.options).map(([opt, txt]) => (
                      <li key={opt}>
                        <code>{opt}</code> — {txt}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </span>
        )}
      </span>
      <span className={styles.keyDesc}>{spec.description}</span>
      {errorReason && <span className={styles.keyError}>{errorReason}</span>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Control — picks slider+number / select / checkbox / text based on spec.type
// ---------------------------------------------------------------------------

function Control() {
  const { spec, draft, onChange } = useTunable();

  if (spec.type === "enum" && spec.enum) {
    return (
      <div className={styles.controlCol}>
        <select value={String(draft)} onChange={(e) => onChange(e.target.value)}>
          {spec.enum.map((v) => (
            <option key={v} value={v}>
              {humanize(v)}
            </option>
          ))}
        </select>
      </div>
    );
  }
  if (spec.type === "bool") {
    return (
      <div className={styles.controlCol}>
        <input
          type="checkbox"
          checked={!!draft}
          onChange={(e) => onChange(e.target.checked)}
        />
      </div>
    );
  }
  if (spec.type === "int" || spec.type === "float") {
    const min = spec.min ?? 0;
    const max = spec.max ?? 100;
    const step = stepFor(spec, min, max);
    const parse = (s: string) => {
      const n = spec.type === "int" ? parseInt(s, 10) : parseFloat(s);
      if (!Number.isFinite(n)) return spec.type === "int" ? 0 : 0;
      const snapped = Math.round((n - min) / step) * step + min;
      const digits =
        step >= 1
          ? 0
          : Math.min(6, Math.max(0, -Math.floor(Math.log10(step))));
      return Number(snapped.toFixed(digits));
    };
    return (
      <div className={styles.controlCol}>
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={Number(draft)}
          onChange={(e) => onChange(parse(e.target.value))}
        />
        <input
          type="number"
          min={min}
          max={max}
          step={step}
          value={Number(draft)}
          onChange={(e) => onChange(parse(e.target.value))}
        />
      </div>
    );
  }
  return (
    <div className={styles.controlCol}>
      <input
        type="text"
        value={String(draft)}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Meta — reset button + mutability badges
// ---------------------------------------------------------------------------

function Meta() {
  const { spec, draft, onChange } = useTunable();
  return (
    <div className={styles.metaCol}>
      <button
        type="button"
        className={styles.resetBtn}
        disabled={draft === spec.default}
        onClick={() => onChange(spec.default as DraftValue)}
        title={`Reset to spec default (${String(spec.default)})`}
      >
        Reset to {String(spec.default)}
      </button>
      {spec.mutability === "read_only" && (
        <span className={`${styles.badge} ${styles.badgeReadonly}`}>read-only</span>
      )}
    </div>
  );
}

Tunable.Label = Label;
Tunable.Control = Control;
Tunable.Meta = Meta;
