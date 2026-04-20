/**
 * Slider-step selection. Honours an explicit `spec.step` when set;
 * otherwise picks the largest "nice" increment (1, 0.5, 0.1, 0.05, …)
 * that yields at least 20 slider stops over the range. Keeps sliders
 * responsive without producing values like `5.0125`.
 */
import type { SettingSpec } from "../types";

export function stepFor(spec: SettingSpec, min: number, max: number): number {
  if (spec.step != null && spec.step > 0) return spec.step;
  if (spec.type === "int") return 1;
  const range = Math.max(max - min, 0.0001);
  const candidates = [10, 5, 2, 1, 0.5, 0.25, 0.1, 0.05, 0.025, 0.01];
  for (const c of candidates) {
    if (range / c >= 20) return c;
  }
  return 0.01;
}
