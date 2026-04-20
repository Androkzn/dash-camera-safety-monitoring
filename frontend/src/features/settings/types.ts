/**
 * Settings Console types — feature-local backend contracts.
 *
 * Cross-feature types stay in `shared/types/common.ts`; everything in
 * here is consumed only by the Settings page.
 */

export type SettingType = "float" | "int" | "bool" | "str" | "enum";
export type Mutability =
  | "hot_apply"
  | "warm_reload"
  | "restart_required"
  | "read_only";

export interface SettingSpec {
  key: string;
  default: number | string | boolean;
  type: SettingType;
  category: string;
  mutability: Mutability;
  description: string;
  min: number | null;
  max: number | null;
  step: number | null;
  enum: string[] | null;
  requires_privacy_confirm: boolean;
}

export interface SettingsSchema {
  schema_version: number;
  categories: string[];
  settings: SettingSpec[];
}

export interface EffectiveSettings {
  schema_version: number;
  values: Record<string, number | string | boolean>;
  revision_hash: string;
  revision_no: number;
}

export interface SettingsTemplate {
  id: string;
  name: string;
  description: string;
  system: boolean;
  soft_deleted_at: number | null;
  created_at: number;
  updated_at: number;
  payload: Record<string, number | string | boolean>;
  latest_revision_no: number;
  payload_hash: string;
}

export interface ApplyResultPayload {
  ok: boolean;
  applied_now: string[];
  pending_restart: string[];
  warnings: string[];
  revision_hash_before: string;
  revision_hash_after: string;
  revision_no: number;
  audit_id?: string | null;
  template_id?: string;
}

export interface ValidationErrorBody {
  errors: Array<{ key: string; reason: string }>;
}

export interface WindowStats {
  window_start_ts: number;
  window_end_ts: number;
  duration_sec: number;
  sample_size: number;
  event_rate_per_min: number;
  severity_counts: Record<string, number>;
  severity_ratios: Record<string, number>;
  confidence_p50: number | null;
  confidence_p95: number | null;
  ttc_p50: number | null;
  ttc_p95: number | null;
  distance_p50_m: number | null;
  distance_p95_m: number | null;
  scene_distribution: Record<string, number>;
  quality_distribution: Record<string, number>;
  fp_rate: number | null;
  fp_rate_source: "feedback" | "proxy" | "insufficient";
  actual_fps_p50: number | null;
  actual_fps_p95: number | null;
  frames_dropped_ratio_p95: number | null;
  cpu_p50: number | null;
  cpu_p95: number | null;
  memory_p95: number | null;
  llm_cost_usd_per_min: number | null;
  llm_tokens_per_min: number | null;
  llm_latency_p95_ms: number | null;
  llm_skip_rate: number | null;
  llm_calls: number;
  ops_samples: number;
}

export type ConfidenceTier = "high" | "medium" | "low" | "insufficient";

export interface ImpactReport {
  audit_id: string;
  change_ts: number;
  actor_label: string;
  before: Record<string, number | string | boolean>;
  after: Record<string, number | string | boolean>;
  changed_keys: string[];
  baseline: WindowStats | null;
  after_window: WindowStats | null;
  deltas: Record<string, number>;
  confidence_tier: ConfidenceTier;
  confidence_reasons: string[];
  immediate_metrics: string[];
  lagging_metrics: string[];
  state: "monitoring" | "monitoring_unattended" | "archived";
  warnings: string[];
  last_good: Record<string, number | string | boolean>;
  narrative: string | null;
  recommendation: "keep" | "revert" | "monitor" | null;
}

export type DraftValue = number | string | boolean;
