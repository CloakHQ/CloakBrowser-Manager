import { useCallback, useEffect, useState } from "react";
import { api, type Profile, type ProfileCreateData } from "../lib/api";

function message(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

export function useProfiles() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [loading, setLoading] = useState(true);
  // Two error channels: a poll error clears on the next good poll, an action
  // error stays until dismissed or replaced — otherwise the 3s poll would wipe
  // a failed save/delete before the user could read it.
  const [pollError, setPollError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.listProfiles();
      setProfiles(data);
      setPollError(null);
    } catch (err) {
      setPollError(message(err, "Không tải được danh sách hồ sơ"));
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

  const clearActionError = useCallback(() => setActionError(null), []);

  // Mutations record the error for the banner AND rethrow, so callers only
  // report success / change views when the server actually confirmed it.
  const fail = useCallback((err: unknown, fallback: string): never => {
    setActionError(message(err, fallback));
    throw err;
  }, []);

  const create = useCallback(
    async (data: ProfileCreateData): Promise<Profile> => {
      try {
        const profile = await api.createProfile(data);
        setProfiles((prev) => [profile, ...prev]);
        setActionError(null);
        return profile;
      } catch (err) {
        return fail(err, "Không tạo được hồ sơ");
      }
    },
    [fail],
  );

  const update = useCallback(
    async (id: string, data: Partial<ProfileCreateData>): Promise<Profile> => {
      try {
        const profile = await api.updateProfile(id, data);
        setProfiles((prev) => prev.map((p) => (p.id === id ? profile : p)));
        setActionError(null);
        return profile;
      } catch (err) {
        return fail(err, "Không lưu được hồ sơ");
      }
    },
    [fail],
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
        setActionError(message(err, "Không lưu được thứ tự hồ sơ"));
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
        setActionError(null);
      } catch (err) {
        fail(err, "Không xóa được hồ sơ");
      }
    },
    [fail],
  );

  const launch = useCallback(
    async (id: string) => {
      // Don't swallow: a launch denial (out of seats, bad/expired key) carries a
      // structured reason + upgrade CTA that the launch banner renders.
      const result = await api.launchProfile(id);
      await refresh();
      return result;
    },
    [refresh],
  );

  const stop = useCallback(
    async (id: string) => {
      try {
        await api.stopProfile(id);
        setActionError(null);
      } catch (err) {
        fail(err, "Không dừng được hồ sơ");
      } finally {
        await refresh();
      }
    },
    [refresh, fail],
  );

  // Wipe browser state + re-roll fingerprint. Stays stopped — the profile keeps
  // its config (proxy, locale, bookmarks, default search) and takes a fresh
  // identity; the user launches it when ready. Returns the reset profile so the
  // form can re-sync to the new seed.
  const reset = useCallback(
    async (id: string): Promise<Profile> => {
      try {
        const profile = await api.resetProfile(id);
        setProfiles((prev) => prev.map((p) => (p.id === id ? profile : p)));
        setActionError(null);
        await refresh();
        return profile;
      } catch (err) {
        return fail(err, "Không đặt lại được hồ sơ");
      }
    },
    [refresh, fail],
  );

  // Clone a profile into a new profile (same settings + fingerprint). With
  // includeBrowserState the source's cookies, logged-in sessions and history
  // come along too, so the clone opens already signed in. Returns the clone so
  // the caller can select it.
  const duplicate = useCallback(
    async (id: string, includeBrowserState = false): Promise<Profile> => {
      try {
        const profile = await api.duplicateProfile(id, includeBrowserState);
        setProfiles((prev) => [profile, ...prev]);
        setActionError(null);
        return profile;
      } catch (err) {
        return fail(err, "Không nhân bản được hồ sơ");
      }
    },
    [fail],
  );

  const error = actionError ?? pollError;

  return {
    profiles,
    loading,
    error,
    pollError,
    actionError,
    clearActionError,
    refresh,
    create,
    update,
    remove,
    reorder,
    launch,
    stop,
    reset,
    duplicate,
  };
}
