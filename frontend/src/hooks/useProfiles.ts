import { useCallback, useEffect, useState } from "react";
import { api, type Profile, type ProfileCreateData } from "../lib/api";

export function useProfiles() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.listProfiles();
      setProfiles(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch profiles");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    // Poll for status changes every 3 seconds
    const interval = setInterval(refresh, 3000);
    return () => clearInterval(interval);
  }, [refresh]);

  const create = useCallback(
    async (data: ProfileCreateData): Promise<Profile | undefined> => {
      try {
        const profile = await api.createProfile(data);
        setProfiles((prev) => [profile, ...prev]);
        return profile;
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to create profile");
      }
    },
    [],
  );

  const update = useCallback(
    async (id: string, data: Partial<ProfileCreateData>) => {
      try {
        const profile = await api.updateProfile(id, data);
        setProfiles((prev) => prev.map((p) => (p.id === id ? profile : p)));
        return profile;
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to update profile");
      }
    },
    [],
  );

  // Persist a manual order. Optimistic so the UI updates instantly; the 3s poll
  // then confirms the server order rather than reverting it. Resync on failure.
  const reorder = useCallback(
    async (orderedIds: string[]) => {
      setProfiles((prev) => {
        const byId = new Map(prev.map((p) => [p.id, p]));
        return orderedIds
          .map((id) => byId.get(id))
          .filter((p): p is Profile => p !== undefined);
      });
      try {
        await api.reorderProfiles(orderedIds);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to reorder profiles");
        await refresh();
      }
    },
    [refresh],
  );

  const remove = useCallback(
    async (id: string) => {
      try {
        await api.deleteProfile(id);
        setProfiles((prev) => prev.filter((p) => p.id !== id));
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to delete profile");
      }
    },
    [],
  );

  const launch = useCallback(
    async (id: string) => {
      try {
        const result = await api.launchProfile(id);
        await refresh();
        return result;
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to launch profile");
      }
    },
    [refresh],
  );

  const stop = useCallback(
    async (id: string) => {
      try {
        await api.stopProfile(id);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to stop profile");
      }
    },
    [refresh],
  );

  // Wipe browser state + re-roll fingerprint. Stays stopped — the profile keeps
  // its config (proxy, locale, bookmarks, default search) and takes a fresh
  // identity; the user launches it when ready.
  const reset = useCallback(
    async (id: string) => {
      try {
        await api.resetProfile(id);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to reset profile");
      }
    },
    [refresh],
  );

  // Clone a profile's config into a new profile (same settings + fingerprint,
  // fresh browser state). Returns the clone so the caller can select it.
  const duplicate = useCallback(
    async (id: string): Promise<Profile | undefined> => {
      try {
        const profile = await api.duplicateProfile(id);
        setProfiles((prev) => [profile, ...prev]);
        return profile;
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to duplicate profile");
      }
    },
    [],
  );

  return { profiles, loading, error, refresh, create, update, remove, reorder, launch, stop, reset, duplicate };
}
