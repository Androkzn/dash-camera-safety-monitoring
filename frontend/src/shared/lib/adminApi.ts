/**
 * adminApi.ts — admin-bearer fetch helpers used by any feature that
 * touches admin-tier endpoints (settings, watchdog mutations, baseline
 * capture, etc).
 *
 * Token storage decision (sessionStorage, not localStorage): smaller XSS
 * attack window and the operator who walks away from a shared workstation
 * doesn't leave a token sitting on disk.
 *
 * CSRF posture: the browser does NOT auto-attach the Authorization header
 * (unlike cookies), so CSRF is structurally impossible against these
 * endpoints. If we ever switch to cookie auth, add CSRF tokens.
 */

const STORAGE_KEY = "road_admin_token";

export class MissingAdminTokenError extends Error {
  constructor() {
    super("missing admin bearer token");
    this.name = "MissingAdminTokenError";
  }
}

export function getAdminToken(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setAdminToken(token: string): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, token);
    window.dispatchEvent(new CustomEvent("admin-token-changed"));
  } catch {
    // sessionStorage can be denied by privacy modes; ignore.
  }
}

export function clearAdminToken(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
    window.dispatchEvent(new CustomEvent("admin-token-changed"));
  } catch {
    /* ignore */
  }
}

function withAdminAuth(init?: RequestInit): RequestInit {
  const token = getAdminToken();
  if (!token) throw new MissingAdminTokenError();
  const headers = new Headers(init?.headers ?? {});
  headers.set("Authorization", `Bearer ${token}`);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return { ...init, headers, cache: "no-store" };
}

/**
 * Typed structured error returned by admin endpoints.
 * 422: validation `{errors: [{key, reason}]}`
 * 409: revision conflict `{error: "revision_conflict", expected, actual}`
 * 429: rate-limited (with `Retry-After` header)
 * 401/403/503: auth failures
 */
export interface AdminApiError extends Error {
  status: number;
  body: unknown;
  retryAfterSec?: number;
}

function buildErrorMessage(
  status: number,
  body: unknown,
  retryAfterSec: number | undefined,
): string {
  const detail =
    body && typeof body === "object" && "detail" in body
      ? String((body as { detail: unknown }).detail)
      : null;
  const errorField =
    body && typeof body === "object" && "error" in body
      ? String((body as { error: unknown }).error)
      : null;
  const base = detail ?? errorField ?? `HTTP ${status}`;
  if (status === 429 && retryAfterSec != null && retryAfterSec > 0) {
    return `Too many apply attempts. Retry in ${retryAfterSec}s. (${base})`;
  }
  return base;
}

export async function adminFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, withAdminAuth(init));
  if (res.ok) {
    return (await res.json()) as T;
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  const retryHeader = res.headers.get("Retry-After");
  const retryAfterSec =
    retryHeader != null && /^\d+$/.test(retryHeader.trim())
      ? parseInt(retryHeader, 10)
      : undefined;
  const err = new Error(buildErrorMessage(res.status, body, retryAfterSec)) as AdminApiError;
  err.status = res.status;
  err.body = body;
  err.retryAfterSec = retryAfterSec;
  throw err;
}

/** True if `exc` is an admin auth failure that should drop the cached token. */
export function isAdminAuthFailure(exc: unknown): boolean {
  const status = (exc as AdminApiError | undefined)?.status;
  return status === 401 || status === 403 || status === 503;
}
