/**
 * fetchClient — minimal typed fetch wrapper used by every feature's
 * `api.ts`. Centralises:
 *   - `cache: "no-store"` (we never want stale safety data)
 *   - HTTP-error-to-Error coercion (fetch doesn't throw on 4xx/5xx)
 *   - JSON parsing + return-type narrowing
 *
 * Auth-bearing requests live in `adminApi.ts` so the public-tier surface
 * never grows a credential by accident.
 */
export async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { cache: "no-store", ...init });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** Convenience wrapper for JSON POST requests with a typed body. */
export async function postJson<T, B = unknown>(
  url: string,
  body: B,
  init?: RequestInit,
): Promise<T> {
  return fetchJson<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    body: JSON.stringify(body),
    ...init,
  });
}
