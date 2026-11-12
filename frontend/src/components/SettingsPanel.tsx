import { useCallback, useEffect, useState } from "react";
import { Loader2, Trash2, X } from "lucide-react";
import { api, type BrowserBinaryList } from "../lib/api";

interface SettingsPanelProps {
  onClose: () => void;
  onChanged: () => void;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function SettingsPanel({ onClose, onChanged }: SettingsPanelProps) {
  const [cache, setCache] = useState<BrowserBinaryList | null>(null);
  const [loading, setLoading] = useState(true);
  const [cleaning, setCleaning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setCache(await api.listBrowsers());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load browser cache");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const cleanup = async () => {
    if (!confirm("Delete every downloaded browser version not referenced by a profile or running session?")) return;
    setCleaning(true);
    setError(null);
    setMessage(null);
    try {
      const result = await api.cleanupUnusedBrowsers();
      setMessage(
        result.removed.length === 0
          ? "No unused browser versions found."
          : `Removed ${result.removed.length} version(s) and reclaimed ${formatBytes(result.reclaimed_bytes)}.`,
      );
      await load();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to clean browser cache");
    } finally {
      setCleaning(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="w-full max-w-2xl rounded-lg border border-border bg-surface-1 shadow-xl" onClick={(event) => event.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Browser cache</h2>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300" title="Close">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-4 p-4">
          <div>
            <label className="label">Storage path</label>
            <code className="block break-all rounded-md bg-surface-2 px-3 py-2 text-xs text-gray-300">
              {cache?.cache_dir ?? "Loading..."}
            </code>
            <p className="mt-1 text-xs text-gray-500">
              Set CLOAKBROWSER_CACHE_DIR to place this cache on an external path or volume.
            </p>
          </div>

          <div>
            <div className="mb-2 flex items-center justify-between">
              <label className="label mb-0">Installed versions</label>
              <button onClick={cleanup} disabled={loading || cleaning} className="btn-danger flex items-center gap-1.5">
                {cleaning ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                Clean unused
              </button>
            </div>
            {loading ? (
              <div className="flex items-center gap-2 py-6 text-sm text-gray-500">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading browser cache...
              </div>
            ) : cache?.binaries.length ? (
              <div className="max-h-80 space-y-2 overflow-y-auto">
                {cache.binaries.map((binary) => (
                  <div key={`${binary.tier}-${binary.version}`} className="rounded-md border border-border bg-surface-2 p-3">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="font-mono text-sm text-gray-200">{binary.version}</div>
                        <div className="mt-1 text-xs text-gray-500">
                          {binary.tier === "licensed" ? "Licensed" : "Keyless"} · {formatBytes(binary.size_bytes)}
                        </div>
                      </div>
                      <span className={binary.in_use ? "text-xs text-emerald-400" : "text-xs text-gray-500"}>
                        {binary.in_use
                          ? `${binary.profile_count} profile(s), ${binary.running_count} running`
                          : "Unused"}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="py-6 text-sm text-gray-500">No browser binary has been downloaded yet.</p>
            )}
          </div>

          {message && <p className="text-xs text-emerald-400">{message}</p>}
          {error && <p className="text-xs text-red-400">{error}</p>}
        </div>

        <div className="flex justify-end border-t border-border px-4 py-3">
          <button onClick={onClose} className="btn-secondary">Close</button>
        </div>
      </div>
    </div>
  );
}
