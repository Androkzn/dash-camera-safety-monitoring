/**
 * Pure helpers that turn raw `WatchdogFinding[]` into `WatchdogIncident[]`
 * (deduped, fingerprinted, sorted by severity → priority → recency).
 *
 * No React, no I/O — easy to unit-test.
 */
import type { WatchdogFinding } from "../../../shared/types/common";

import type { WatchdogIncident } from "../types";

const SEV_ORDER: Record<string, number> = { error: 0, warning: 1, info: 2 };

export function findingKey(f: WatchdogFinding): string {
  return `${f.snapshot_id}_${f.ts}`;
}

export function incidentId(f: WatchdogFinding): string {
  return f.fingerprint || `${f.category}:${f.title}`;
}

export function buildIncidents(items: WatchdogFinding[]): WatchdogIncident[] {
  const groups = new Map<string, WatchdogIncident>();

  for (const finding of items) {
    const id = incidentId(finding);
    const key = findingKey(finding);
    const existing = groups.get(id);

    if (!existing) {
      groups.set(id, {
        id,
        fingerprint: finding.fingerprint || id,
        severity: finding.severity,
        category: finding.category || "system",
        title: finding.title,
        owner: finding.owner,
        count: 1,
        firstSeen: finding.ts,
        lastSeen: finding.ts,
        rawKeys: [key],
        latest: finding,
      });
      continue;
    }

    existing.count += 1;
    existing.rawKeys.push(key);

    if ((SEV_ORDER[finding.severity] ?? 9) < (SEV_ORDER[existing.severity] ?? 9)) {
      existing.severity = finding.severity;
    }
    if (new Date(finding.ts).getTime() < new Date(existing.firstSeen).getTime()) {
      existing.firstSeen = finding.ts;
    }
    if (new Date(finding.ts).getTime() >= new Date(existing.lastSeen).getTime()) {
      existing.lastSeen = finding.ts;
      existing.latest = finding;
      existing.category = finding.category || existing.category;
      existing.title = finding.title || existing.title;
      existing.owner = finding.owner || existing.owner;
    }
  }

  return Array.from(groups.values()).sort((a, b) => {
    const sev = (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9);
    if (sev !== 0) return sev;
    const pri = (b.latest.priority_score ?? 0) - (a.latest.priority_score ?? 0);
    if (pri !== 0) return pri;
    if (b.count !== a.count) return b.count - a.count;
    return b.lastSeen.localeCompare(a.lastSeen);
  });
}
