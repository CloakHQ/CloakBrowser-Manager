import { AppWindow, Globe, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, type Profile, type Tab } from "../lib/api";

interface TabManagerPanelProps {
  profiles: Profile[];
}

type TabsByProfile = Record<string, Tab[] | "loading" | "error">;

export function TabManagerPanel({ profiles }: TabManagerPanelProps) {
  const [open, setOpen] = useState(false);
  const [tabsByProfile, setTabsByProfile] = useState<TabsByProfile>({});
  const runningProfiles = profiles.filter((p) => p.status === "running");

  const refresh = useCallback((ids: string[]) => {
    setTabsByProfile((prev) => {
      const next = { ...prev };
      for (const id of ids) next[id] = "loading";
      return next;
    });
    for (const id of ids) {
      api.listTabs(id)
        .then(({ tabs }) => setTabsByProfile((prev) => ({ ...prev, [id]: tabs })))
        .catch(() => setTabsByProfile((prev) => ({ ...prev, [id]: "error" })));
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    refresh(runningProfiles.map((p) => p.id));
    // Only re-fetch when the panel opens or the set of running profiles
    // changes — not on every 3s profile poll tick, which would otherwise
    // reset an in-progress "loading" row on every render while it's open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, runningProfiles.map((p) => p.id).join(",")]);

  const handleClose = useCallback(async (profileId: string, index: number) => {
    try {
      await api.closeTab(profileId, index);
    } catch {
      // Fall through to a refresh either way — the list may already be
      // stale (the tab could have closed itself) and the refresh is what
      // corrects it either way.
    }
    refresh([profileId]);
  }, [refresh]);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="text-gray-500 hover:text-gray-300 p-1"
        title="Tab manager"
      >
        <AppWindow className="h-4 w-4" />
      </button>
      {open && (
        <div className="absolute right-0 mt-2 w-96 max-h-[70vh] overflow-y-auto bg-surface-2 border border-border rounded-lg shadow-lg p-4 z-50">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
              Open Tabs
            </h3>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-gray-500 hover:text-gray-300"
              aria-label="Close"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>

          {runningProfiles.length === 0 && (
            <p className="text-gray-500 text-xs">No profiles are running.</p>
          )}

          <div className="space-y-4">
            {runningProfiles.map((profile) => {
              const tabs = tabsByProfile[profile.id];
              return (
                <div key={profile.id}>
                  <p className="text-xs font-medium text-gray-300 mb-1.5">{profile.name}</p>
                  {tabs === "loading" && <p className="text-gray-500 text-xs">Loading...</p>}
                  {tabs === "error" && (
                    <p className="text-red-400 text-xs">Failed to load tabs.</p>
                  )}
                  {Array.isArray(tabs) && tabs.length === 0 && (
                    <p className="text-gray-500 text-xs">No open tabs.</p>
                  )}
                  {Array.isArray(tabs) && tabs.length > 0 && (
                    <ul className="space-y-1">
                      {tabs.map((tab) => (
                        <li
                          key={tab.index}
                          className="flex items-center gap-2 text-xs bg-surface-1 rounded px-2 py-1.5"
                        >
                          {tab.favicon ? (
                            <img
                              src={tab.favicon}
                              alt=""
                              className="h-3.5 w-3.5 flex-shrink-0"
                              onError={(e) => {
                                e.currentTarget.style.display = "none";
                              }}
                            />
                          ) : (
                            <Globe className="h-3.5 w-3.5 flex-shrink-0 text-gray-500" />
                          )}
                          <div className="min-w-0 flex-1">
                            <p className="text-gray-200 truncate" title={tab.title}>
                              {tab.title}
                            </p>
                            <p className="text-gray-500 truncate" title={tab.url}>
                              {tab.url}
                            </p>
                          </div>
                          <button
                            type="button"
                            onClick={() => handleClose(profile.id, tab.index)}
                            className="text-gray-500 hover:text-gray-300 flex-shrink-0"
                            title="Close tab"
                          >
                            <X className="h-3.5 w-3.5" />
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
