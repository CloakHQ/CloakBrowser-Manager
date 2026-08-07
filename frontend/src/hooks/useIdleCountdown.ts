import { useEffect, useRef, useState } from "react";

/**
 * Ticks a server-reported "seconds until idle auto-stop" down locally,
 * once a second, between the ~5s resource polls that actually refresh it.
 * A bare display of the raw polled value would otherwise sit frozen for up
 * to 5s at a time and then visibly jump — this makes it read as a live
 * countdown instead.
 *
 * Resyncs to `serverRemainingSeconds` the instant it changes (a fresh poll,
 * or the profile going from/to not-running), so a burst of real activity
 * that resets the reaper's clock server-side is reflected immediately
 * rather than waiting for the local countdown to happen to agree.
 */
export function useIdleCountdown(serverRemainingSeconds: number | null): number | null {
  const [display, setDisplay] = useState<number | null>(serverRemainingSeconds);
  const lastServerValue = useRef<number | null>(serverRemainingSeconds);

  useEffect(() => {
    if (serverRemainingSeconds !== lastServerValue.current) {
      lastServerValue.current = serverRemainingSeconds;
      setDisplay(serverRemainingSeconds);
    }
  }, [serverRemainingSeconds]);

  // Depends on presence, not the value itself: re-running this effect every
  // tick would reset the 1000ms window instead of letting it land on its
  // own schedule, and functional setState below never reads a stale value.
  const isTicking = display !== null;
  useEffect(() => {
    if (!isTicking) return;
    const interval = setInterval(() => {
      setDisplay((prev) => (prev === null ? null : Math.max(0, prev - 1)));
    }, 1000);
    return () => clearInterval(interval);
  }, [isTicking]);

  return display;
}

/** "12m34s" — same convention as CookieWarmupPanel's own formatRemaining,
 *  which sits in the same top bar this renders next to. */
export function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${minutes}m${secs.toString().padStart(2, "0")}s`;
}
