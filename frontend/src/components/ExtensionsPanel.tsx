import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Plus, Puzzle, RefreshCw, Trash2, Undo2, Upload, X } from "lucide-react";
import { api, type LibraryExtension } from "../lib/api";

interface ExtensionsPanelProps {
  onClose: () => void;
  /** Called after any library change so profiles (counts, attached IDs) can reload. */
  onChanged: () => void;
}

function byName(list: LibraryExtension[]): LibraryExtension[] {
  return [...list].sort((a, b) => a.name.localeCompare(b.name));
}

/** Where the extension came from: a Web Store link, the URL's host, or "Uploaded". */
function SourceLabel({ extension }: { extension: LibraryExtension }) {
  if (extension.source_kind === "upload") return <>Uploaded</>;
  if (extension.source_kind === "webstore" && extension.webstore_id) {
    return (
      <a
        href={`https://chromewebstore.google.com/detail/${extension.webstore_id}`}
        target="_blank"
        rel="noreferrer"
        className="text-indigo-400 hover:text-indigo-300"
      >
        Chrome Web Store
      </a>
    );
  }
  try {
    return <>{new URL(extension.source_url ?? "").host}</>;
  } catch {
    return <>URL</>;
  }
}

/** The library icon, with a Puzzle placeholder when there is none (or it fails). */
export function ExtensionIcon({ extension }: { extension: LibraryExtension }) {
  const [failed, setFailed] = useState(false);
  if (!extension.icon_url || failed) return <Puzzle className="h-5 w-5 text-gray-500" />;
  return (
    <img
      src={extension.icon_url}
      alt=""
      className="h-5 w-5"
      onError={() => setFailed(true)}
    />
  );
}

/**
 * The Manager-wide extension library. Extensions are downloaded and unpacked
 * once here; profiles attach them by ID, so an update reaches every profile.
 */
export function ExtensionsPanel({ onClose, onChanged }: ExtensionsPanelProps) {
  const [extensions, setExtensions] = useState<LibraryExtension[]>([]);
  const [loading, setLoading] = useState(true);
  const [source, setSource] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  // Two-click delete: the first click only asks, the second runs it.
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  // Inline "paste a new URL" row for one extension.
  const [urlTarget, setUrlTarget] = useState<string | null>(null);
  const [urlInput, setUrlInput] = useState("");

  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadTarget = useRef<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setExtensions(await api.listExtensions());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load the extension library");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const resetStatus = () => {
    setError(null);
    setMessage(null);
  };

  const replace = useCallback((extension: LibraryExtension) => {
    setExtensions((prev) => byName(prev.map((e) => (e.id === extension.id ? extension : e))));
  }, []);

  const add = async () => {
    const value = source.trim();
    if (!value) return;
    resetStatus();
    setBusy("add");
    try {
      const added = await api.addExtension(value);
      setExtensions((prev) => byName([...prev.filter((e) => e.id !== added.id), added]));
      setSource("");
      setMessage(`Added ${added.name} ${added.version}.`);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add the extension");
    } finally {
      setBusy(null);
    }
  };

  const pickFile = (extensionId?: string) => {
    uploadTarget.current = extensionId ?? null;
    fileInputRef.current?.click();
  };

  const uploadFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    const target = uploadTarget.current;
    event.target.value = "";
    uploadTarget.current = null;
    if (!file) return;
    resetStatus();
    setBusy(target ? `update:${target}` : "upload");
    try {
      const saved = await api.uploadExtension(file, target ?? undefined);
      setExtensions((prev) => byName([...prev.filter((e) => e.id !== saved.id), saved]));
      setMessage(
        target
          ? `Installed ${saved.name} ${saved.version}.`
          : `Added ${saved.name} ${saved.version}.`,
      );
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setBusy(null);
    }
  };

  const update = async (extension: LibraryExtension, newSource: string | null) => {
    resetStatus();
    setBusy(`update:${extension.id}`);
    try {
      const result = await api.updateExtension(extension.id, newSource);
      replace(result.extension);
      setUrlTarget(null);
      setUrlInput("");
      setMessage(
        result.updated
          ? `Updated ${result.extension.name} to ${result.extension.version}.`
          : "Already up to date",
      );
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Update failed");
    } finally {
      setBusy(null);
    }
  };

  const rollback = async (extension: LibraryExtension) => {
    resetStatus();
    setBusy(`rollback:${extension.id}`);
    try {
      const restored = await api.rollbackExtension(extension.id);
      replace(restored);
      setMessage(`Rolled back ${restored.name} to ${restored.version}.`);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rollback failed");
    } finally {
      setBusy(null);
    }
  };

  const remove = async (extension: LibraryExtension) => {
    setPendingDelete(null);
    resetStatus();
    setBusy(`delete:${extension.id}`);
    try {
      await api.deleteExtension(extension.id);
      setExtensions((prev) => prev.filter((e) => e.id !== extension.id));
      setMessage(`Deleted ${extension.name}.`);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-label="Extension library"
        className="w-full max-w-2xl rounded-lg border border-border bg-surface-1 shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Extension library</h2>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300" title="Close">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-4 p-4">
          <div>
            <label className="label" htmlFor="extension-source">
              Chrome Web Store link, extension ID, or CRX/ZIP URL
            </label>
            <div className="flex gap-2">
              <input
                id="extension-source"
                className="input flex-1"
                value={source}
                onChange={(e) => setSource(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void add();
                  }
                }}
                placeholder="https://chromewebstore.google.com/detail/.../ddkjiahejlhfcafbddmgiahcphecmpfh"
              />
              <button
                type="button"
                onClick={() => void add()}
                disabled={!source.trim() || busy !== null}
                className="btn-primary flex items-center gap-1.5"
              >
                {busy === "add" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
                Add
              </button>
              <button
                type="button"
                onClick={() => pickFile()}
                disabled={busy !== null}
                className="btn-secondary flex items-center gap-1.5"
              >
                {busy === "upload" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
                Upload CRX/ZIP
              </button>
            </div>
            <p className="mt-1 text-xs text-gray-500">
              Extensions are unpacked once into the data directory and attached to profiles by ID.
            </p>
            <input
              ref={fileInputRef}
              type="file"
              accept=".crx,.zip,application/zip,application/x-chrome-extension"
              className="hidden"
              onChange={(e) => void uploadFile(e)}
            />
          </div>

          {message && <p className="text-xs text-emerald-400">{message}</p>}
          {error && <p className="text-xs text-red-400">{error}</p>}

          {loading ? (
            <div className="flex items-center gap-2 py-6 text-sm text-gray-500">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading extensions...
            </div>
          ) : extensions.length === 0 ? (
            <p className="py-6 text-sm text-gray-500">
              No extensions yet. Paste a Chrome Web Store link or upload a CRX/ZIP.
            </p>
          ) : (
            <div className="max-h-96 space-y-2 overflow-y-auto">
              {extensions.map((extension) => {
                const busyWith = busy?.endsWith(`:${extension.id}`) ?? false;
                const inUse = extension.profile_count > 0;
                return (
                  <div key={extension.id} className="rounded-md border border-border bg-surface-2 p-3">
                    <div className="flex flex-col gap-2">
                      <div className="flex min-w-0 items-center gap-2">
                        <ExtensionIcon extension={extension} />
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                            <span className="truncate text-sm text-gray-200">{extension.name}</span>
                            <span className="rounded bg-surface-3 px-1.5 py-0.5 text-[10px] text-gray-400">
                              {extension.version}
                            </span>
                            {extension.manifest_version === 2 ? (
                              <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-400">
                                MV2 — disabled by Chromium
                              </span>
                            ) : (
                              <span className="rounded bg-surface-3 px-1.5 py-0.5 text-[10px] text-gray-400">MV3</span>
                            )}
                          </div>
                          <div className="mt-1 text-xs text-gray-500">
                            <SourceLabel extension={extension} /> · Used by {extension.profile_count}{" "}
                            {extension.profile_count === 1 ? "profile" : "profiles"}
                          </div>
                        </div>
                      </div>
                      <div className="flex flex-wrap items-center gap-1.5 pl-7">
                        <button
                          type="button"
                          onClick={() =>
                            extension.source_kind === "upload"
                              ? pickFile(extension.id)
                              : void update(extension, null)
                          }
                          disabled={busy !== null}
                          className="btn-secondary flex items-center gap-1.5 text-xs"
                          title={
                            extension.source_kind === "upload"
                              ? "Upload a new version"
                              : "Download the latest version"
                          }
                        >
                          {busyWith && busy?.startsWith("update") ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            <RefreshCw className="h-3 w-3" />
                          )}
                          Update
                        </button>
                        {extension.source_kind !== "upload" && (
                          <button
                            type="button"
                            onClick={() => {
                              setUrlTarget(urlTarget === extension.id ? null : extension.id);
                              setUrlInput("");
                            }}
                            disabled={busy !== null}
                            className="text-xs text-gray-500 hover:text-gray-300"
                            title="Update from a different URL"
                          >
                            New URL
                          </button>
                        )}
                        {extension.previous_version && (
                          <button
                            type="button"
                            onClick={() => void rollback(extension)}
                            disabled={busy !== null}
                            className="btn-secondary flex items-center gap-1.5 text-xs"
                          >
                            {busyWith && busy?.startsWith("rollback") ? (
                              <Loader2 className="h-3 w-3 animate-spin" />
                            ) : (
                              <Undo2 className="h-3 w-3" />
                            )}
                            Roll back to {extension.previous_version}
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => setPendingDelete(extension.id)}
                          disabled={busy !== null || inUse || pendingDelete === extension.id}
                          className="btn-danger flex items-center gap-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-40"
                          title={
                            inUse
                              ? `Used by ${extension.profile_count} profile(s); detach it there first`
                              : "Delete from the library"
                          }
                        >
                          {busyWith && busy?.startsWith("delete") ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            <Trash2 className="h-3 w-3" />
                          )}
                          Delete
                        </button>
                      </div>
                    </div>

                    {urlTarget === extension.id && (
                      <div className="mt-2 flex gap-2">
                        <input
                          className="input flex-1"
                          value={urlInput}
                          onChange={(e) => setUrlInput(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault();
                              if (urlInput.trim()) void update(extension, urlInput.trim());
                            }
                          }}
                          placeholder="New CRX/ZIP URL or Chrome Web Store link"
                          autoFocus
                        />
                        <button
                          type="button"
                          onClick={() => urlInput.trim() && void update(extension, urlInput.trim())}
                          disabled={!urlInput.trim() || busy !== null}
                          className="btn-secondary text-xs"
                        >
                          Use URL
                        </button>
                      </div>
                    )}

                    {pendingDelete === extension.id && (
                      <div
                        role="alert"
                        className="mt-2 flex items-center justify-between gap-3 rounded-md border border-red-800/70 bg-red-950/40 px-3 py-2"
                      >
                        <span className="text-xs text-red-300">
                          Deletes the unpacked files. This cannot be undone.
                        </span>
                        <div className="flex shrink-0 gap-2">
                          <button type="button" onClick={() => setPendingDelete(null)} className="btn-secondary text-xs">
                            Cancel
                          </button>
                          <button
                            type="button"
                            onClick={() => void remove(extension)}
                            className="btn flex items-center gap-1.5 bg-red-600 text-xs text-white hover:bg-red-500"
                          >
                            <Trash2 className="h-3 w-3" />
                            Confirm: Delete
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="flex justify-end border-t border-border px-4 py-3">
          <button onClick={onClose} className="btn-secondary">
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
