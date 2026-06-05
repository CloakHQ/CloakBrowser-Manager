import { Plus, Search, Monitor, Terminal } from "lucide-react";
import { useState } from "react";
import type { Profile } from "../lib/api";
import { StatusIndicator } from "./StatusIndicator";

interface ProfileListProps {
  profiles: Profile[];
  selectedId: string | null;
  scriptsSelected: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onScripts: () => void;
}

export function ProfileList({
  profiles,
  selectedId,
  scriptsSelected,
  onSelect,
  onNew,
  onScripts,
}: ProfileListProps) {
  const [search, setSearch] = useState("");

  const filtered = profiles.filter((p) =>
    p.name.toLowerCase().includes(search.toLowerCase()),
  );

  const runningCount = profiles.filter((p) => p.status === "running").length;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-border">
        <div className="flex items-center gap-2 mb-3">
          <Monitor className="h-4 w-4 text-accent" />
          <h1 className="text-sm font-semibold tracking-tight">CloakBrowser Manager</h1>
        </div>
        {runningCount > 0 && (
          <div className="text-xs text-gray-500 mb-3">
            {runningCount} running
          </div>
        )}
        {/* Search */}
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-500" />
          <input
            type="text"
            placeholder="Search profiles..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input pl-8 py-1.5 text-xs"
          />
        </div>
      </div>

      {/* Profile list */}
      <div className="flex-1 overflow-y-auto p-2">
        {filtered.length === 0 && (
          <div className="text-center text-gray-500 text-xs py-8">
            {profiles.length === 0 ? "No profiles yet" : "No matches"}
          </div>
        )}
        {filtered.map((profile) => (
          <button
            key={profile.id}
            onClick={() => onSelect(profile.id)}
            className={`w-full text-left px-3 py-2.5 rounded-md mb-1 transition-colors ${
              selectedId === profile.id
                ? "bg-surface-3 border border-border-hover"
                : "hover:bg-surface-2 border border-transparent"
            }`}
          >
            <div className="flex items-center gap-2">
              <StatusIndicator status={profile.status} />
              <span className="text-sm font-medium truncate">{profile.name}</span>
            </div>
            <div className="flex items-center gap-2 mt-1 ml-4">
              <span className="text-xs text-gray-500 capitalize">{profile.platform}</span>
              {profile.proxy && (
                <>
                  <span className="text-xs text-gray-600">·</span>
                  <span className="text-xs text-gray-500">Proxy</span>
                </>
              )}
            </div>
            {profile.tags.length > 0 && (
              <div className="flex gap-1 mt-1.5 ml-4 flex-wrap">
                {profile.tags.map((t) => (
                  <span
                    key={t.tag}
                    className="text-[10px] px-1.5 py-0.5 rounded-full bg-surface-4 text-gray-400"
                    style={t.color ? { backgroundColor: `${t.color}20`, color: t.color } : undefined}
                  >
                    {t.tag}
                  </span>
                ))}
              </div>
            )}
          </button>
        ))}
      </div>

      {/* New profile button */}
      <div className="p-3 border-t border-border space-y-2">
        <button
          onClick={onScripts}
          className={`btn-secondary w-full flex items-center justify-center gap-1.5 ${
            scriptsSelected ? "bg-surface-4" : ""
          }`}
        >
          <Terminal className="h-3.5 w-3.5" />
          <span>Scripts</span>
        </button>
        <button onClick={onNew} className="btn-secondary w-full flex items-center justify-center gap-1.5">
          <Plus className="h-3.5 w-3.5" />
          <span>New Profile</span>
        </button>
      </div>
    </div>
  );
}
