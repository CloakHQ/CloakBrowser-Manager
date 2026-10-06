import { useEffect, useState } from "react";
import { Loader2, X } from "lucide-react";
import { api, type ManagerSettings, type SystemStatus } from "../lib/api";

interface SettingsPanelProps {
  onClose: () => void;
  onSaved: (status: SystemStatus) => void;
}

/**
 * In-app settings — replaces hand-editing a .env for the native app.
 * v1 exposes the license key + release channel; the field list is kept flat so
 * future settings drop in as new rows.
 */
export function SettingsPanel({ onClose, onSaved }: SettingsPanelProps) {
  const [current, setCurrent] = useState<ManagerSettings | null>(null);
  const [licenseKey, setLicenseKey] = useState("");
  const [channel, setChannel] = useState<"stable" | "preview">("stable");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getSettings()
      .then((s) => {
        setCurrent(s);
        setChannel(s.release_channel === "preview" ? "preview" : "stable");
      })
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Failed to load settings"),
      );
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      // Send license_key only when the user typed something (empty input =
      // leave the existing key untouched, not clear it).
      const payload: { license_key?: string; release_channel: string } = {
        release_channel: channel,
      };
      if (licenseKey.trim()) payload.license_key = licenseKey.trim();
      const status = await api.updateSettings(payload);
      onSaved(status);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save settings");
    } finally {
      setSaving(false);
    }
  };

  const handleClearKey = async () => {
    setSaving(true);
    setError(null);
    try {
      const status = await api.updateSettings({ license_key: "" });
      onSaved(status);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to clear key");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-lg border border-border bg-surface-1 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Settings</h2>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-300"
            title="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-4 p-4">
          <div>
            <label className="label">CloakBrowser license key</label>
            <input
              className="input font-mono"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={licenseKey}
              onChange={(e) => setLicenseKey(e.target.value)}
              placeholder={
                current?.license_key_set
                  ? `Current: ${current.license_key_masked ?? "set"} — type to replace`
                  : "cb_… (leave empty to run the free keyless build)"
              }
            />
            <p className="mt-1 text-xs text-gray-500">
              One key per Manager instance — every profile shares its seat pool.{" "}
              <a
                href="https://cloakbrowser.dev/free"
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline"
              >
                Get a free key
              </a>
              .
            </p>
          </div>

          <div>
            <label className="label">Release channel</label>
            <select
              className="input"
              value={channel}
              onChange={(e) =>
                setChannel(e.target.value === "preview" ? "preview" : "stable")
              }
            >
              <option value="stable">Stable</option>
              <option value="preview">Preview (early access)</option>
            </select>
          </div>

          {error && <p className="text-xs text-red-400">{error}</p>}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-border px-4 py-3">
          {current?.license_key_set ? (
            <button
              onClick={handleClearKey}
              disabled={saving}
              className="text-xs text-gray-500 hover:text-red-400 disabled:opacity-50"
            >
              Remove key
            </button>
          ) : (
            <span />
          )}
          <div className="flex items-center gap-2">
            <button onClick={onClose} disabled={saving} className="btn-secondary">
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="btn-primary flex items-center gap-1.5"
            >
              {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              <span>{saving ? "Applying…" : "Save"}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
