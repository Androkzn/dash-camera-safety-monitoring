/**
 * Tests API surface — pytest runner integration.
 */
import { fetchJson } from "../../shared/lib/fetchClient";
import type { TestStatus } from "../../shared/types/common";

export const testsApi = {
  getStatus: () => fetchJson<TestStatus>("/api/tests/status"),
  run: () => fetchJson<{ ok: boolean }>("/api/tests/run", { method: "POST" }),
};

export const testsQueryKeys = {
  all: ["tests"] as const,
  status: ["tests", "status"] as const,
};
