import { useEffect, useState } from "react";
import { Cpu, Info, Loader2, LogOut, Power, Radio, Server } from "lucide-react";
import { api, type ManagerSettings, type SystemStatus } from "../lib/api";
import { useToast } from "./ui/toast";

interface SettingsPageProps {
  status: SystemStatus | null;
  authRequired: boolean;
  onLogout: () => void;
  onQuit: () => void;
}

/** App-wide settings: release channel, environment details, session controls. */
export function SettingsPage({ status, authRequired, onLogout, onQuit }: SettingsPageProps) {
  const toast = useToast();
  const [current, setCurrent] = useState<ManagerSettings | null>(null);
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
      .catch((err) => setError(err instanceof Error ? err.message : "Không thể tải cài đặt"));
  }, []);

  const dirty = current !== null && channel !== current.release_channel;

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const next = await api.updateSettings({ release_channel: channel });
      setCurrent((prev) => (prev ? { ...prev, release_channel: channel } : prev));
      toast.success(
        `Kênh phát hành: ${channel === "preview" ? "Bản xem trước" : "Ổn định"}`,
        next.binary_version ? `Bản dựng: ${next.binary_version}` : undefined,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể lưu cài đặt");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-6 py-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-fg">Cài đặt</h1>
        <p className="mt-0.5 text-xs text-fg-muted">Kênh phát hành, môi trường chạy và phiên làm việc.</p>
      </div>

      <section className="card p-4" aria-labelledby="channel-heading">
        <div className="mb-2 flex items-center gap-2">
          <Radio className="h-4 w-4 text-info" />
          <h2 id="channel-heading" className="text-sm font-semibold text-fg">
            Kênh phát hành
          </h2>
        </div>
        <p className="mb-3 text-xs text-fg-muted">
          Kênh xác định bản dựng CloakBrowser mà Manager tải về và dùng để khởi chạy hồ sơ.
        </p>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <select
            className="input sm:max-w-xs"
            value={channel}
            onChange={(e) => setChannel(e.target.value === "preview" ? "preview" : "stable")}
            aria-label="Kênh phát hành"
          >
            <option value="stable">Ổn định</option>
            <option value="preview">Bản xem trước (truy cập sớm)</option>
          </select>
          <button onClick={handleSave} disabled={saving || !dirty} className="btn-primary">
            {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {saving ? "Đang áp dụng…" : "Lưu"}
          </button>
        </div>
      </section>

      <section className="card p-4" aria-labelledby="env-heading">
        <div className="mb-3 flex items-center gap-2">
          <Cpu className="h-4 w-4 text-brand" />
          <h2 id="env-heading" className="text-sm font-semibold text-fg">
            Môi trường
          </h2>
        </div>
        <dl className="divide-y divide-line/60 text-xs">
          {[
            ["Bản dựng CloakBrowser", status?.binary_version ?? "chưa xác định"],
            ["Máy chủ", status?.host_os ?? "chưa xác định"],
            ["Chế độ chạy", status?.runtime_mode ?? "chưa xác định"],
            ["Trình xem", status?.viewer_mode ?? "chưa xác định"],
            [
              "Font Windows persona",
              status?.windows_fonts_complete === null || status?.windows_fonts_complete === undefined
                ? "không áp dụng"
                : status.windows_fonts_complete
                  ? `đủ (${status.windows_fonts_present}/${status.windows_fonts_required})`
                  : `thiếu (${status.windows_fonts_present}/${status.windows_fonts_required})`,
            ],
          ].map(([label, value]) => (
            <div key={label} className="flex items-center justify-between gap-4 py-2">
              <dt className="text-fg-muted">{label}</dt>
              <dd className="truncate text-right font-mono text-fg-secondary">{value}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="card p-4" aria-labelledby="session-heading">
        <div className="mb-3 flex items-center gap-2">
          <Server className="h-4 w-4 text-warning" />
          <h2 id="session-heading" className="text-sm font-semibold text-fg">
            Phiên làm việc
          </h2>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {authRequired && (
            <button onClick={onLogout} className="btn-secondary">
              <LogOut className="h-3.5 w-3.5" />
              Đăng xuất
            </button>
          )}
          <button onClick={onQuit} className="btn-danger">
            <Power className="h-3.5 w-3.5" />
            Thoát Manager
          </button>
          <p className="text-2xs text-fg-muted">Thoát sẽ dừng máy chủ và đóng mọi hồ sơ đang chạy.</p>
        </div>
      </section>

      <section className="flex items-start gap-2 text-2xs text-fg-muted">
        <Info className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
        <p>
          CloakBrowser Manager (Custom) · bản fork không chính thức. Key bản quyền và kho key
          nằm ở mục <span className="font-medium text-fg-secondary">Bản quyền</span>.
        </p>
      </section>

      {error && (
        <div role="alert" className="rounded-lg border border-danger/25 bg-danger/10 px-3 py-2 text-xs text-danger">
          {error}
        </div>
      )}
    </div>
  );
}
