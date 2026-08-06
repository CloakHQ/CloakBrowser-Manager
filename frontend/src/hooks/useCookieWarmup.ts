import { useCallback, useEffect, useRef, useState } from "react";
import { api, type CookieWarmupStatus } from "../lib/api";

// Only worth polling while a run is actually in flight — an idle/done/error
// profile's status cannot change on its own, so one fetch on mount (or right
// after start/stop) is enough; see refresh()'s self-rescheduling below.
const POLL_INTERVAL_MS = 3000;

export interface UseCookieWarmup {
  status: CookieWarmupStatus | null;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  busy: boolean;
}

export function useCookieWarmup(profileId: string | null): UseCookieWarmup {
  const [status, setStatus] = useState<CookieWarmupStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const refresh = useCallback(async () => {
    if (!profileId) return;
    try {
      const data = await api.cookieWarmupStatus(profileId);
      setStatus(data);
      clearTimer();
      if (data.state === "running") {
        timerRef.current = setTimeout(refresh, POLL_INTERVAL_MS);
      }
    } catch {
      // Profile deleted mid-poll or a transient hiccup — keep the last known
      // status rather than flashing it away, and don't reschedule blindly.
    }
  }, [profileId, clearTimer]);

  useEffect(() => {
    setStatus(null);
    clearTimer();
    if (profileId) refresh();
    return clearTimer;
  }, [profileId, refresh, clearTimer]);

  const start = useCallback(async () => {
    if (!profileId) return;
    setBusy(true);
    try {
      const data = await api.startCookieWarmup(profileId);
      setStatus(data);
      clearTimer();
      timerRef.current = setTimeout(refresh, POLL_INTERVAL_MS);
    } finally {
      setBusy(false);
    }
  }, [profileId, refresh, clearTimer]);

  const stop = useCallback(async () => {
    if (!profileId) return;
    setBusy(true);
    try {
      const data = await api.stopCookieWarmup(profileId);
      setStatus(data);
      clearTimer();
    } finally {
      setBusy(false);
    }
  }, [profileId, clearTimer]);

  return { status, start, stop, busy };
}
