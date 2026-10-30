import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw, Trash2 } from "lucide-react";
import { api, type LicenseKeyEntry } from "../lib/api";

/** Badge colour per validation state. "unreachable" is not a failure: the
 * license server simply did not answer, so the key stays in rotation. */
const STATE_STYLE: Record<string, { cls: string; text: string }> = {
  valid: { cls: "badge badge-success", text: "valid" },
  invalid: { cls: "badge badge-danger", text: "invalid" },
  unreachable: { cls: "badge badge-warning", text: "unreachable" },
  unknown: { cls: "badge badge-neutral", text: "unchecked" },
};
const UNKNOWN_STATE = { cls: "badge badge-neutral", text: "unchecked" };

/**
 * The shared license key pool. Each running profile leases one key, so N
 * concurrent profiles need N keys — this list is where they live. Keys are
 * added one per line and validated on arrival, so a dead key is caught before
 * a launch fails on it.
 */
export function KeyPoolPanel() {
  const [entries, setEntries] = useState<LicenseKeyEntry[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<"add" | "check" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setEntries(await api.listKeys());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load keys");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleAdd = async () => {
    if (!draft.trim()) return;
    setBusy("add");
    setError(null);
    try {
      setEntries(await api.addKeys(draft));
      setDraft("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add keys");
    } finally {
      setBusy(null);
    }
  };

  const handleCheck = async () => {
    setBusy("check");
    setError(null);
    try {
      await api.checkKeys();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to validate keys");
    } finally {
      setBusy(null);
    }
  };

  const handleDelete = async (id: string) => {
    setError(null);
    try {
      await api.deleteKey(id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove key");
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <label className="label mb-0">
          Key pool{" "}
          <span className="font-normal text-fg-muted">
            ({entries.length} {entries.length === 1 ? "key" : "keys"})
          </span>
        </label>
        <button
          onClick={handleCheck}
          disabled={busy !== null || entries.length === 0}
          className="flex items-center gap-1 text-xs text-fg-muted hover:text-fg disabled:opacity-50"
          title="Re-validate every pooled key against the license server"
        >
          {busy === "check" ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
          Validate all
        </button>
      </div>

      {entries.length > 0 && (
        <ul className="divide-y divide-border rounded border border-border text-xs">
          {entries.map((entry) => {
            const style = STATE_STYLE[entry.state] ?? UNKNOWN_STATE;
            return (
              <li
                key={entry.id}
                className="flex items-center gap-2 px-2 py-1.5"
              >
                <span className="font-mono">{entry.key_masked}</span>
                <span className={style.cls}>{style.text}</span>
                {/* The server reports a placeholder plan for a key it rejected;
                    showing it next to "invalid" is just noise. */}
                {entry.state === "valid" && entry.plan && (
                  <span className="text-fg-muted">{entry.plan}</span>
                )}
                <span className="ml-auto text-fg-muted">
                  {entry.leased_to ? `in use: ${entry.leased_to}` : "free"}
                </span>
                <button
                  onClick={() => handleDelete(entry.id)}
                  disabled={entry.leased_to !== null}
                  className="text-fg-muted hover:text-danger disabled:opacity-30"
                  title={
                    entry.leased_to
                      ? "Stop the profile using this key first"
                      : "Remove key"
                  }
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <textarea
        className="input font-mono h-16 resize-y"
        spellCheck={false}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={"One key per line\ncb_…\ncb_…"}
      />
      <div className="flex items-center gap-2">
        <button
          onClick={handleAdd}
          disabled={busy !== null || !draft.trim()}
          className="btn-secondary flex items-center gap-1.5 text-xs"
        >
          {busy === "add" && <Loader2 className="h-3 w-3 animate-spin" />}
          Add keys
        </button>
        <p className="text-xs text-fg-muted">
          One key per running profile. Keys are validated on add.
        </p>
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}
