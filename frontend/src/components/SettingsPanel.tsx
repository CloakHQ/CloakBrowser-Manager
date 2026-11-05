import { useEffect, useState } from "react";
import { ExternalLink, KeyRound, Loader2, Radio, Settings, X } from "lucide-react";
import { api, type ManagerSettings, type SystemStatus } from "../lib/api";
import { KeyPoolPanel } from "./KeyPoolPanel";
import { useConfirm } from "./ui/confirm";
import { useToast } from "./ui/toast";

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
  const confirm = useConfirm();
  const toast = useToast();
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
        setError(err instanceof Error ? err.message : "Không thể tải cài đặt"),
      );
  }, []);

  // Escape closes the panel, but not while a confirm dialog is on top of it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

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
      toast.success("Đã lưu cài đặt");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể lưu cài đặt");
    } finally {
      setSaving(false);
    }
  };

  const handleClearKey = async () => {
    const ok = await confirm({
      title: "Xóa key bản quyền?",
      description:
        "Ứng dụng sẽ quay về bản miễn phí không key. Các hồ sơ đang chạy vẫn tiếp tục, " +
        "nhưng lần khởi chạy tiếp theo sẽ cần key.",
      confirmLabel: "Xóa key",
      tone: "danger",
    });
    if (!ok) return;
    setSaving(true);
    setError(null);
    try {
      const status = await api.updateSettings({ license_key: "" });
      onSaved(status);
      toast.success("Đã xóa key bản quyền");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể xóa key");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        className="max-h-[90vh] w-full max-w-xl flex flex-col overflow-hidden rounded-2xl border border-line-strong bg-surface-1 shadow-popover"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line bg-surface-2/40 px-5 py-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand/15 border border-brand/25 text-brand">
              <Settings className="h-4 w-4" />
            </div>
            <div>
              <h2 id="settings-title" className="text-sm font-semibold text-fg">Cài đặt hệ thống</h2>
              <p className="text-2xs text-fg-muted">Quản lý bản quyền, kho key và kênh phát hành</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="rail-item"
            title="Đóng"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto space-y-5 p-5">
          <div className="rounded-xl border border-line bg-surface-0/50 p-4">
            <div className="flex items-center gap-2 mb-2">
              <KeyRound className="h-3.5 w-3.5 text-brand" />
              <label className="label mb-0">Key bản quyền CloakBrowser</label>
            </div>
            <input
              className="input font-mono"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={licenseKey}
              onChange={(e) => setLicenseKey(e.target.value)}
              placeholder={
                current?.license_key_set
                  ? `Hiện tại: ${current.license_key_masked ?? "đã đặt"} — nhập để thay thế`
                  : "cb_… (cần key để khởi chạy hồ sơ)"
              }
            />
            <p className="mt-2 text-xs text-fg-muted leading-relaxed">
              Key dùng chung cho ứng dụng. Mọi hồ sơ không có key riêng trong kho
              sẽ dùng key này, nên nó chỉ phục vụ được một hồ sơ tại một thời điểm.{" "}
              <a
                href="https://cloakbrowser.dev/free"
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 font-medium text-brand hover:underline"
              >
                <span>Lấy key miễn phí</span>
                <ExternalLink className="h-3 w-3" />
              </a>
              .
            </p>
          </div>

          <div className="rounded-xl border border-line bg-surface-0/50 p-4">
            <KeyPoolPanel />
          </div>

          <div className="rounded-xl border border-line bg-surface-0/50 p-4">
            <div className="flex items-center gap-2 mb-2">
              <Radio className="h-3.5 w-3.5 text-info" />
              <label className="label mb-0">Kênh phát hành</label>
            </div>
            <select
              className="input"
              value={channel}
              onChange={(e) =>
                setChannel(e.target.value === "preview" ? "preview" : "stable")
              }
            >
              <option value="stable">Ổn định</option>
              <option value="preview">Bản xem trước (truy cập sớm)</option>
            </select>
          </div>

          {error && (
            <div className="rounded-lg bg-danger/10 border border-danger/25 px-3 py-2 text-xs text-danger">
              {error}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-line bg-surface-2/30 px-5 py-3.5">
          {current?.license_key_set ? (
            <button
              onClick={handleClearKey}
              disabled={saving}
              className="btn-danger text-xs py-1 px-2.5"
            >
              Xóa key
            </button>
          ) : (
            <span />
          )}
          <div className="flex items-center gap-2">
            <button onClick={onClose} disabled={saving} className="btn-secondary">
              Hủy
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="btn-primary flex items-center gap-1.5"
            >
              {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              <span>{saving ? "Đang áp dụng…" : "Lưu"}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
