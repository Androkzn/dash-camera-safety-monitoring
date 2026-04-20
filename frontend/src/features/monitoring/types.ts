/**
 * Monitoring-feature types — derived from upstream WatchdogFinding.
 */
import type { WatchdogFinding } from "../../shared/types/common";

export type SevFilter = "all" | "error" | "warning" | "info";

export interface WatchdogIncident {
  id: string;
  fingerprint: string;
  severity: "error" | "warning" | "info";
  category: string;
  title: string;
  owner?: string;
  count: number;
  firstSeen: string;
  lastSeen: string;
  rawKeys: string[];
  latest: WatchdogFinding;
}
