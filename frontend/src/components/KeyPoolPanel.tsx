import { useCallback, useEffect, useState } from "react";
import { KeyRound, Loader2, Plus, RefreshCw, Trash2 } from "lucide-react";
import { api, type LicenseKeyEntry } from "../lib/api";
import { KEY_STATE_LABEL } from "../lib/labels";
import { useConfirm } from "./ui/confirm";
import { useToast } from "./ui/toast";

/** Badge colour per validation state. "unreachable" is not a failure: the
 * license server simply did not answer, so the key stays in rotation. */
const STATE_STYLE: Record<string, string> = {
  valid: "badge badge-success",
  invalid: "badge badge-danger",
  unreachable: "badge badge-warning",
  unknown: "badge badge-neutral",
};
const UNKNOWN_STATE = "badge badge-neutral";

/**
 * The shared license key pool. Each running profile leases one key, so N
 * concurrent profiles need N keys — this list is where they live. Keys are
 * added one per line and validated on arrival, so a dead key is caught before
 * a launch fails on it.
 */
export function KeyPoolPanel() {
  const confirm = useConfirm();
  const toast = useToast();
  const [entries, setEntries] = useState<LicenseKeyEntry[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<"add" | "check" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setEntries(await api.listKeys());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể tải key");
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
      const next = await api.addKeys(draft);
      setEntries(next);
      setDraft("");
      toast.success(`Đã thêm key vào kho (${next.length} key)`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể thêm key");
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
      toast.success("Đã kiểm tra lại toàn bộ key");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể kiểm tra key");
    } finally {
      setBusy(null);
    }
  };

  const handleDelete = async (id: string) => {
    const entry = entries.find((e) => e.id === id);
    const ok = await confirm({
      title: `Xóa key ${entry?.key_masked ?? ""} khỏi kho?`,
      description: "Key sẽ bị gỡ khỏi danh sách và không còn được cấp cho hồ sơ nào khi khởi chạy.",
      confirmLabel: "Xóa key",
      tone: "danger",
    });
    if (!ok) return;
    setError(null);
    try {
      await api.deleteKey(id);
      await load();
      toast.success("Đã xóa key khỏi kho");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể xóa key");
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <KeyRound className="h-3.5 w-3.5 text-warning" />
          <label className="label mb-0">
            Kho key{" "}
            <span className="font-normal text-fg-muted">
              ({entries.length} key)
            </span>
          </label>
        </div>
        <button
          onClick={handleCheck}
          disabled={busy !== null || entries.length === 0}
          className="inline-flex items-center gap-1.5 rounded-md border border-line bg-surface-2 px-2.5 py-1 text-2xs font-medium text-fg-secondary hover:bg-surface-3 hover:text-fg disabled:opacity-50 transition-colors"
          title="Kiểm tra lại toàn bộ key trong kho với máy chủ bản quyền"
        >
          {busy === "check" ? (
            <Loader2 className="h-3 w-3 animate-spin text-brand-fg" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
          Kiểm tra tất cả
        </button>
      </div>

      {entries.length > 0 && (
        <ul className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface-1 text-xs">
          {entries.map((entry) => {
            const style = STATE_STYLE[entry.state] ?? UNKNOWN_STATE;
            const stateLabel = KEY_STATE_LABEL[entry.state] ?? KEY_STATE_LABEL.unknown;
            // The server still counts this key's last session, so it can't be
            // handed to a launch yet — and must not be deleted.
            const cooling =
              entry.available_at !== null && new Date(entry.available_at) > new Date();
            return (
              <li
                key={entry.id}
                className="flex items-center gap-2.5 px-3 py-2 hover:bg-surface-2/50 transition-colors"
              >
                <span className="font-mono text-fg">{entry.key_masked}</span>
                <span className={style}>{stateLabel}</span>
                {/* The server reports a placeholder plan for a key it rejected;
                    showing it next to "invalid" is just noise. */}
                {entry.state === "valid" && entry.plan && (
                  <span className="text-fg-muted">{entry.plan === "free" ? "miễn phí" : entry.plan}</span>
                )}
                <span className="ml-auto text-2xs text-fg-muted">
                  {cooling
                    ? "đang chờ"
                    : entry.leased_to
                      ? `đang dùng: ${entry.leased_to}`
                      : "trống"}
                </span>
                <button
                  onClick={() => handleDelete(entry.id)}
                  disabled={entry.leased_to !== null || cooling}
                  className="rounded p-1 text-fg-muted hover:bg-danger/10 hover:text-danger disabled:opacity-30 transition-colors"
                  title={
                    cooling
                      ? "Máy chủ vẫn tính phiên trước của key này; thử lại sau"
                      : entry.leased_to
                        ? "Dừng hồ sơ đang dùng key này trước"
                        : "Xóa key"
                  }
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <textarea
        className="input font-mono h-16 resize-y text-xs"
        spellCheck={false}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={"Mỗi dòng một key\ncb_…\ncb_…"}
      />
      <div className="flex items-center gap-2.5">
        <button
          onClick={handleAdd}
          disabled={busy !== null || !draft.trim()}
          className="btn-secondary flex items-center gap-1.5 text-xs py-1.5"
        >
          {busy === "add" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
          Thêm key
        </button>
        <p className="text-2xs text-fg-muted">
          Mỗi hồ sơ đang chạy cần một key. Key được kiểm tra khi thêm.
        </p>
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}
