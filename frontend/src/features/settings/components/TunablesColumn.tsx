/**
 * TunablesColumn — grouped <details> blocks for the schema's tunables.
 *
 * Renders one <details> per category, then one <Tunable> per spec inside.
 * Validation errors and the draft override come from the parent page.
 */

import { humanize } from "../utils/formatting";
import type { DraftValue, EffectiveSettings, SettingSpec } from "../types";

import { Tunable } from "./Tunable";

import styles from "../SettingsPage.module.css";

interface TunablesColumnProps {
  groupedSpecs: Array<[string, SettingSpec[]]>;
  effective: EffectiveSettings;
  draft: Record<string, DraftValue>;
  errorByKey: Record<string, string>;
  onChange: (key: string, value: DraftValue) => void;
}

export function TunablesColumn({
  groupedSpecs,
  effective,
  draft,
  errorByKey,
  onChange,
}: TunablesColumnProps) {
  return (
    <>
      {groupedSpecs.map(([cat, specs]) => (
        <details key={cat} className={styles.category} open>
          <summary>
            {humanize(cat)}{" "}
            <span style={{ float: "right", fontSize: 10 }}>{specs.length}</span>
          </summary>
          <div className={styles.categoryBody}>
            {specs.map((spec) => {
              const eff = effective.values[spec.key] as DraftValue;
              const cur = (draft[spec.key] ?? eff) as DraftValue;
              return (
                <Tunable
                  key={spec.key}
                  spec={spec}
                  effective={eff}
                  draft={cur}
                  errorReason={errorByKey[spec.key] ?? null}
                  onChange={(v) => onChange(spec.key, v)}
                />
              );
            })}
          </div>
        </details>
      ))}
    </>
  );
}
