import { Download, FileDown, FileUp, Loader2, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { ProfileFile } from "../lib/api";
import { useConfirm } from "./ui/confirm";
import { useToast } from "./ui/toast";

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

function sameFiles(a: ProfileFile[], b: ProfileFile[]): boolean {
  return (
    a.length === b.length &&
    a.every((file, i) => {
      const other = b[i];
      return (
        !!other &&
        file.id === other.id &&
        file.state === other.state &&
        file.size === other.size &&
        file.name === other.name
      );
    })
  );
}

/** One file on its way into a profile. Survives the panel being closed. */
export interface Transfer {
  id: string;
  name: string;
  state: "uploading" | "done" | "failed";
  error?: string;
  file: File;
}

/** Files attached to one profile, shared by the toolbar panel and the drop target. */
export function useProfileFiles(profileId: string) {
  const confirm = useConfirm();
  const toast = useToast();
  const [files, setFiles] = useState<ProfileFile[]>([]);
  const [transfers, setTransfers] = useState<Transfer[]>([]);
  const [error, setError] = useState<string | null>(null);
  // A list response that started before a mutation describes a world that no longer exists.
  const mutations = useRef(0);
  const listing = useRef(false);

  const refresh = useCallback(async () => {
    if (listing.current) return; // single-flight: a slow poll must not stack up
    listing.current = true;
    const generation = mutations.current;
    try {
      const next = await api.listProfileFiles(profileId);
      if (generation !== mutations.current) return;
      // Replacing the array on every poll re-renders rows the user is aiming at. Only
      // publish a genuinely different list.
      setFiles((prev) => (sameFiles(prev, next) ? prev : next));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể tải danh sách tệp");
    } finally {
      listing.current = false;
    }
  }, [profileId]);

  const uploadFiles = useCallback(
    async (chosen: File[]) => {
      setError(null);
      // Sequential on purpose: each upload is bounded server-side and the order is the
      // order the user dropped them in.
      for (const file of chosen) {
        const id = `${Date.now()}-${file.name}-${Math.random().toString(36).slice(2, 8)}`;
        setTransfers((prev) => [...prev, { id, name: file.name, state: "uploading", file }]);
        mutations.current += 1;
        try {
          const added = await api.uploadProfileFile(profileId, file);
          setFiles((prev) => [added, ...prev.filter((f) => f.id !== added.id)]);
          setTransfers((prev) => prev.map((t) => (t.id === id ? { ...t, state: "done" } : t)));
        } catch (err) {
          const message = err instanceof Error ? err.message : "Tải lên thất bại";
          setTransfers((prev) =>
            prev.map((t) => (t.id === id ? { ...t, state: "failed", error: message } : t)),
          );
        } finally {
          // Bump on settle as well as on start: a poll issued while this upload was in
          // flight would otherwise land afterwards and erase the row it just added.
          mutations.current += 1;
        }
      }
    },
    [profileId],
  );

  const dismissTransfer = useCallback(
    (id: string) => setTransfers((prev) => prev.filter((t) => t.id !== id)),
    [],
  );

  const retryTransfer = useCallback(
    async (id: string) => {
      const transfer = transfers.find((t) => t.id === id);
      if (!transfer) return;
      dismissTransfer(id);
      await uploadFiles([transfer.file]);
    },
    [transfers, dismissTransfer, uploadFiles],
  );

  // A finished upload announces itself, then gets out of the way. Failures stay put.
  useEffect(() => {
    const done = transfers.filter((t) => t.state === "done");
    if (done.length === 0) return;
    const timer = setTimeout(
      () => setTransfers((prev) => prev.filter((t) => t.state !== "done")),
      6000,
    );
    return () => clearTimeout(timer);
  }, [transfers]);

  const uploading = transfers.some((t) => t.state === "uploading");

  const remove = useCallback(
    async (fileId: string) => {
      const name = files.find((f) => f.id === fileId)?.name ?? "tệp này";
      const ok = await confirm({
        title: `Xóa "${name}" khỏi hồ sơ?`,
        description: "Tệp sẽ bị xóa khỏi hồ sơ và không thể tải lại.",
        confirmLabel: "Xóa tệp",
        tone: "danger",
      });
      if (!ok) return;
      try {
        mutations.current += 1;
        await api.deleteProfileFile(profileId, fileId);
        setFiles((prev) => prev.filter((f) => f.id !== fileId));
        toast.success(`Đã xóa "${name}"`);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Xóa thất bại");
      } finally {
        mutations.current += 1;
      }
    },
    [profileId, files, confirm, toast],
  );

  useEffect(() => {
    refresh();
  }, [refresh]);

  return {
    files, transfers, uploading, error, refresh,
    uploadFiles, remove, dismissTransfer, retryTransfer,
    clearError: () => setError(null),
  };
}

interface ProfileFilesButtonProps {
  profileId: string;
  profileName: string;
  files: ProfileFile[];
  uploading: boolean;
  error: string | null;
  onUpload: (files: File[]) => Promise<void>;
  onRemove: (fileId: string) => Promise<void>;
  onRefresh: () => Promise<void>;
  onClearError: () => void;
}

export function ProfileFilesButton({
  profileId,
  profileName,
  files,
  uploading,
  error,
  onUpload,
  onRemove,
  onRefresh,
  onClearError,
}: ProfileFilesButtonProps) {
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!panelRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // A download started in the browser lands here on its own; poll so it appears while open.
  useEffect(() => {
    if (!open) return;
    void onRefresh();
    const timer = setInterval(() => void onRefresh(), 2000);
    return () => clearInterval(timer);
  }, [open, onRefresh]);

  const pick = async (list: FileList | null) => {
    const chosen = list ? Array.from(list) : [];
    if (chosen.length) await onUpload(chosen);
    if (inputRef.current) inputRef.current.value = "";
  };

  return (
    <div ref={panelRef} className="relative flex items-center">
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        multiple
        onChange={(e) => pick(e.target.files)}
        data-testid="profile-file-input"
      />
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Tệp của hồ sơ, ${files.length} tệp`}
        title="Các tệp của hồ sơ này"
        className={`relative inline-flex items-center justify-center rounded-md p-1.5 transition-colors focus:outline-none focus:ring-2 focus:ring-brand/50 ${
          open || files.length ? "bg-brand-subtle text-brand-fg" : "text-fg-muted hover:bg-surface-3 hover:text-fg"
        }`}
      >
        {uploading ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <FileUp className="h-3.5 w-3.5" />
        )}
        {files.length > 0 && !uploading && (
          <span
            aria-hidden="true"
            className="absolute -right-1 -top-1 rounded-full bg-brand px-1 text-[9px] font-semibold leading-[14px] text-white shadow-sm"
          >
            {files.length}
          </span>
        )}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Tệp của hồ sơ"
          className="absolute right-0 top-full z-20 mt-1.5 w-80 overflow-hidden rounded-xl border border-line-strong bg-surface-1 shadow-popover"
        >
          <div className="flex items-center justify-between border-b border-line bg-surface-2/50 px-3.5 py-2.5">
            <span className="text-xs font-semibold text-fg">Tệp của hồ sơ này</span>
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                triggerRef.current?.focus();
              }}
              className="rounded p-1 text-fg-muted hover:bg-surface-3 hover:text-fg focus:outline-none focus:ring-2 focus:ring-brand/50"
              aria-label="Đóng"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>

          {error && (
            <div className="flex items-start justify-between gap-2 border-b border-line bg-danger/10 px-3.5 py-2">
              <span className="text-xs text-danger">{error}</span>
              <button
                type="button"
                onClick={onClearError}
                className="rounded p-1 text-danger focus:outline-none focus:ring-2 focus:ring-brand/50"
                aria-label="Bỏ qua lỗi"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          )}

          <div className="max-h-64 divide-y divide-line/60 overflow-y-auto">
            {files.length === 0 ? (
              <p className="px-4 py-6 text-center text-xs text-fg-muted leading-relaxed">
                Chưa có tệp. Hãy tải lên, kéo thả vào màn hình hoặc tải xuống trong trình duyệt.
              </p>
            ) : (
              files.map((file) => (
                <div key={file.id} className="flex items-center gap-2.5 px-3.5 py-2.5 hover:bg-surface-2/70 transition-colors">
                  {file.kind === "download" ? (
                    <FileDown className="h-3.5 w-3.5 shrink-0 text-info" aria-label="Trình duyệt đã tải xuống" />
                  ) : (
                    <FileUp className="h-3.5 w-3.5 shrink-0 text-brand-fg" aria-label="Đã tải lên" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs font-medium text-fg" title={file.name}>
                      {file.name}
                    </div>
                    <div className="text-[11px] text-fg-muted">
                      {file.state === "pending" ? (
                        <span className="text-brand-fg">Đang tải xuống...</span>
                      ) : file.state === "failed" ? (
                        <span className="text-danger">Tải xuống thất bại</span>
                      ) : (
                        formatSize(file.size)
                      )}
                    </div>
                  </div>
                  {file.state === "ready" && (
                    <a
                      href={api.profileFileUrl(profileId, file.id)}
                      className="rounded p-1 text-fg-muted hover:bg-surface-3 hover:text-fg focus:outline-none focus:ring-2 focus:ring-brand/50"
                      title="Tải về máy này"
                    >
                      <Download className="h-3.5 w-3.5" />
                    </a>
                  )}
                  <button
                    type="button"
                    onClick={() => onRemove(file.id)}
                    className="rounded p-1 text-fg-muted hover:bg-danger/10 hover:text-danger focus:outline-none focus:ring-2 focus:ring-brand/50"
                    title="Xóa khỏi hồ sơ"
                    aria-label={`Xóa ${file.name}`}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))
            )}
          </div>

          <div className="border-t border-line bg-surface-2/30 px-3.5 py-3">
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              disabled={uploading}
              className="btn-primary flex w-full items-center justify-center gap-1.5 text-xs py-1.5"
            >
              <FileUp className="h-3.5 w-3.5" />
              <span>{uploading ? "Đang tải lên..." : "Tải tệp lên"}</span>
            </button>
            <p className="mt-2 text-[11px] leading-snug text-fg-muted">
              Trong hộp thoại tệp của trang, chọn{" "}
              <span className="font-medium text-fg-secondary">Uploads — {profileName}</span> ở thanh bên.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
