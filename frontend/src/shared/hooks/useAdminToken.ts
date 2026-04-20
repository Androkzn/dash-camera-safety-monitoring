/**
 * useAdminToken — React hook around the sessionStorage-backed admin token.
 *
 * Returns the cached token plus setter / clearer helpers. Listens for
 * the "admin-token-changed" custom event so two SettingsPage instances
 * open in different tabs (or any other consumer) stay in sync.
 */

import { useCallback, useEffect, useState } from "react";

import {
  clearAdminToken,
  getAdminToken,
  setAdminToken,
} from "../lib/adminApi";

export function useAdminToken(): {
  token: string | null;
  setToken: (t: string) => void;
  clear: () => void;
} {
  const [token, setTokenState] = useState<string | null>(() => getAdminToken());

  useEffect(() => {
    const onChange = () => setTokenState(getAdminToken());
    window.addEventListener("admin-token-changed", onChange);
    return () => window.removeEventListener("admin-token-changed", onChange);
  }, []);

  const setToken = useCallback((t: string) => setAdminToken(t.trim()), []);
  const clear = useCallback(() => clearAdminToken(), []);

  return { token, setToken, clear };
}
