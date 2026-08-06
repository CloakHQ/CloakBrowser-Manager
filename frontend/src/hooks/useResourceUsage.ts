import { useEffect, useState } from "react";
import { api, type ResourceUsage } from "../lib/api";

/**
 * Polls a running profile's CPU/memory. Each request costs ~200ms server
 * side (resources.py takes two CPU samples a beat apart to get an instant
 * reading without caching state across polls), so this deliberately polls
 * slower than the 3s profile-list refresh — there's no reason to synchronize
 * with it, and the endpoint 404s for anything that isn't running anyway.
 */
export function useResourceUsage(
  profileId: string | null,
  isRunning: boolean,
): ResourceUsage | null {
  const [usage, setUsage] = useState<ResourceUsage | null>(null);

  useEffect(() => {
    if (!profileId || !isRunning) {
      setUsage(null);
      return;
    }
    let cancelled = false;

    const poll = async () => {
      try {
        const data = await api.profileResources(profileId);
        if (cancelled) return;
        setUsage(data);
      } catch {
        // A 404 mid-poll (the profile just stopped) or a transient network
        // hiccup — keep the last known reading rather than flashing it off.
      }
    };

    poll();
    const interval = setInterval(poll, 5000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [profileId, isRunning]);

  return usage;
}
