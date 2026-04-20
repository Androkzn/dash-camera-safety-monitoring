/**
 * useIncidentState / useLastVisit — client-only "unread" state persisted
 * to localStorage.
 *
 * - useIncidentState: per-incident map (acknowledged / resolved) keyed by id.
 * - useLastVisit:     epoch-ms of last time this page was viewed; used to
 *                     badge incidents newer than the last visit.
 */
import { useCallback, useEffect, useState } from "react";

export type UserStatus = "acknowledged" | "resolved";

export interface StoredIncidentState {
  status: UserStatus;
  at: string;
  by?: string;
}

type Store = Record<string, StoredIncidentState>;

const STORE_KEY = "watchdog:incidentState:v1";
const VISIT_KEY = "watchdog:lastVisit:v1";

function readStore(): Store {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    return raw ? (JSON.parse(raw) as Store) : {};
  } catch {
    return {};
  }
}

function writeStore(s: Store) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(s));
  } catch {
    /* ignore */
  }
}

export function useIncidentState() {
  const [store, setStore] = useState<Store>(() => readStore());

  const acknowledge = useCallback((id: string, by = "operator") => {
    setStore((prev) => {
      const next: Store = {
        ...prev,
        [id]: { status: "acknowledged", at: new Date().toISOString(), by },
      };
      writeStore(next);
      return next;
    });
  }, []);

  const resolve = useCallback((id: string, by = "operator") => {
    setStore((prev) => {
      const next: Store = {
        ...prev,
        [id]: { status: "resolved", at: new Date().toISOString(), by },
      };
      writeStore(next);
      return next;
    });
  }, []);

  const reopen = useCallback((id: string) => {
    setStore((prev) => {
      const next: Store = { ...prev };
      delete next[id];
      writeStore(next);
      return next;
    });
  }, []);

  const purgeAll = useCallback(() => {
    setStore({});
    writeStore({});
  }, []);

  return { store, acknowledge, resolve, reopen, purgeAll };
}

export function useLastVisit() {
  const [lastVisit] = useState<number>(() => {
    try {
      const raw = localStorage.getItem(VISIT_KEY);
      return raw ? Number(raw) : 0;
    } catch {
      return 0;
    }
  });

  useEffect(() => {
    const markVisited = () => {
      try {
        localStorage.setItem(VISIT_KEY, String(Date.now()));
      } catch {
        /* ignore */
      }
    };
    const timer = setTimeout(markVisited, 4000);
    window.addEventListener("beforeunload", markVisited);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("beforeunload", markVisited);
      markVisited();
    };
  }, []);

  return lastVisit;
}
