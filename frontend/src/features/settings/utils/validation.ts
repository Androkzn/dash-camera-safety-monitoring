/**
 * Pure error-shape predicates for the Settings apply / rollback flow.
 *
 * The settings router throws structured errors with `body.error` /
 * `body.errors` discriminators; these helpers narrow them to typed
 * shapes the page can render.
 */
import type { AdminApiError } from "../../../shared/lib/adminApi";

export function isPrivacyConfirmRequired(exc: unknown): boolean {
  return (
    !!exc &&
    typeof exc === "object" &&
    (exc as AdminApiError).status === 400 &&
    (exc as AdminApiError).body !== null &&
    typeof (exc as AdminApiError).body === "object" &&
    ((exc as AdminApiError).body as { error?: string }).error ===
      "privacy_confirm_required"
  );
}

export function extractValidationErrors(
  exc: unknown,
): Array<{ key: string; reason: string }> | null {
  if (
    exc &&
    typeof exc === "object" &&
    (exc as AdminApiError).status === 422 &&
    (exc as AdminApiError).body !== null &&
    typeof (exc as AdminApiError).body === "object"
  ) {
    const body = (exc as AdminApiError).body as {
      errors?: Array<{ key: string; reason: string }>;
    };
    return body.errors ?? null;
  }
  return null;
}
