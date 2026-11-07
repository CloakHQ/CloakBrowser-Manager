import { useEffect, useState } from "react";
import { Cpu, Info, Loader2, LogOut, Monitor, Moon, Power, Radio, Server, Sun } from "lucide-react";
import { api, type ManagerSettings, type SystemStatus } from "../lib/api";
import { useToast } from "./ui/toast";
import { useTheme } from "../lib/useTheme";
import type { ThemePreference } from "../lib/theme";

interface SettingsPageProps {
  status: SystemStatus | null;
  authRequired: boolean;
  onLogout: () => void;
  onQuit: () => void;
}

/** App-wide settings: release channel, environment details, session controls. */
const THEME_OPTIONS: { key: ThemePreference; label: string; hint: string; icon: typeof Sun }[] = [
  { key: "system", label: "Theo hệ thống", hint: "Đổi theo cài đặt sáng/tối của máy", icon: Monitor },
  { key: "light", label: "Sáng", hint: "Nền sáng, luôn bật", icon: Sun },
  { key: "dark", label: "Tối", hint: "Nền tối, luôn bật", icon: Moon },
];

export function SettingsPage({ status, authRequired, onLogout, onQuit }: SettingsPageProps) {
  const toast = useToast();
  const theme = useTheme();
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
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-6 py-6">
      <div>
        <h1 className="text-xl font-bold tracking-tight text-fg">Cài đặt hệ thống</h1>
        <p className="mt-1 text-xs text-fg-muted">Tùy biến giao diện, kênh phát hành, môi trường chạy và phiên làm việc.</p>
      </div>

      {/* Theme Selection */}
      <section className="card p-5 shadow-card" aria-labelledby="appearance-heading">
        <div className="mb-2 flex items-center gap-2">
          <Sun className="h-4 w-4 text-warning" />
          <h2 id="appearance-heading" className="text-sm font-bold text-fg">
            Giao diện hiển thị
          </h2>
        </div>
        <p className="mb-4 text-xs text-fg-muted">
          Chọn bảng màu sáng hoặc tối. "Theo hệ thống" tự động đồng bộ theo cài đặt giao diện của hệ điều hành.
        </p>
        <div
          role="radiogroup"
          aria-label="Giao diện"
          className="grid grid-cols-1 gap-3 sm:grid-cols-3"
        >
          {THEME_OPTIONS.map(({ key, label, hint, icon: Icon }) => {
            const selected = theme.preference === key;
            return (
              <button
                key={key}
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => theme.setPreference(key)}
                className={`flex flex-col gap-2 rounded-2xl border p-3.5 text-left transition-all duration-150 ${
                  selected
                    ? "border-brand/40 bg-brand/12 shadow-sm"
                    : "border-line/80 bg-surface-2/40 hover:border-line-strong hover:bg-surface-2/80"
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className={`flex h-8 w-8 items-center justify-center rounded-xl border ${
                    selected
                      ? "border-brand/30 bg-brand/15 text-brand-fg"
                      : "border-line bg-surface-1 text-fg-muted"
                  }`}>
                    <Icon className="h-4 w-4" />
                  </div>
                  {selected && (
                    <span className="h-2 w-2 rounded-full bg-brand shadow-[0_0_8px_rgba(99,102,241,0.6)]" />
                  )}
                </div>
                <div className="min-w-0 mt-1">
                  <span className={`block text-xs font-semibold ${selected ? "text-fg" : "text-fg-secondary"}`}>
                    {label}
                  </span>
                  <span className="mt-0.5 block text-[11px] leading-snug text-fg-muted">{hint}</span>
                </div>
              </button>
            );
          })}
        </div>
        <p className="mt-3 text-xs text-fg-muted">
          Đang áp dụng:{" "}
          <span className="font-semibold text-fg-secondary">
            {theme.resolved === "dark" ? "Giao diện tối (Dark)" : "Giao diện sáng (Light)"}
          </span>
          {theme.preference === "system" && " · tự động theo hệ thống"}.
        </p>
      </section>

      {/* Release Channel */}
      <section className="card p-5 shadow-card" aria-labelledby="channel-heading">
        <div className="mb-2 flex items-center gap-2">
          <Radio className="h-4 w-4 text-info" />
          <h2 id="channel-heading" className="text-sm font-bold text-fg">
            Kênh phát hành CloakBrowser
          </h2>
        </div>
        <p className="mb-3 text-xs text-fg-muted">
          Kênh xác định bản dựng CloakBrowser mà Manager tải về và dùng để khởi chạy các hồ sơ.
        </p>
        <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
          <select
            className="input sm:max-w-xs"
            value={channel}
            onChange={(e) => setChannel(e.target.value === "preview" ? "preview" : "stable")}
            aria-label="Kênh phát hành"
          >
            <option value="stable">Ổn định (Stable)</option>
            <option value="preview">Bản xem trước (Preview / Early Access)</option>
          </select>
          <button onClick={handleSave} disabled={saving || !dirty} className="btn-primary h-9 px-4 text-xs">
            {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            <span>{saving ? "Đang áp dụng…" : "Lưu cài đặt"}</span>
          </button>
        </div>
      </section>

      {/* Environment Diagnostics */}
      <section className="card p-5 shadow-card" aria-labelledby="env-heading">
        <div className="mb-3 flex items-center gap-2">
          <Cpu className="h-4 w-4 text-brand-fg" />
          <h2 id="env-heading" className="text-sm font-bold text-fg">
            Thông tin môi trường
          </h2>
        </div>
        <dl className="divide-y divide-line/60 text-xs">
          {[
            ["Bản dựng CloakBrowser", status?.binary_version ?? "chưa xác định"],
            ["Hệ điều hành máy chủ", status?.host_os ?? "chưa xác định"],
            ["Chế độ chạy", status?.runtime_mode ?? "chưa xác định"],
            ["Chế độ hiển thị (Trình xem)", status?.viewer_mode ?? "chưa xác định"],
            [
              "Font chữ Windows persona",
              status?.windows_fonts_complete === null || status?.windows_fonts_complete === undefined
                ? "không áp dụng"
                : status.windows_fonts_complete
                  ? `đầy đủ (${status.windows_fonts_present}/${status.windows_fonts_required})`
                  : `thiếu (${status.windows_fonts_present}/${status.windows_fonts_required})`,
            ],
          ].map(([label, value]) => (
            <div key={label} className="flex items-center justify-between gap-4 py-2.5">
              <dt className="text-fg-muted font-medium">{label}</dt>
              <dd className="truncate text-right font-mono text-fg-secondary text-[11px]">{value}</dd>
            </div>
          ))}
        </dl>
      </section>

      {/* Session Controls */}
      <section className="card p-5 shadow-card" aria-labelledby="session-heading">
        <div className="mb-3 flex items-center gap-2">
          <Server className="h-4 w-4 text-warning" />
          <h2 id="session-heading" className="text-sm font-bold text-fg">
            Quản trị phiên & Máy chủ
          </h2>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {authRequired && (
            <button onClick={onLogout} className="btn-secondary h-8 px-3 text-xs">
              <LogOut className="h-3.5 w-3.5" />
              <span>Đăng xuất</span>
            </button>
          )}
          <button onClick={onQuit} className="btn-danger h-8 px-3.5 text-xs">
            <Power className="h-3.5 w-3.5" />
            <span>Dừng máy chủ & Thoát</span>
          </button>
          <p className="text-xs text-fg-muted">Dừng máy chủ sẽ đóng mọi hồ sơ đang chạy.</p>
        </div>
      </section>

      <section className="flex items-start gap-2 text-xs text-fg-muted">
        <Info className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-brand-fg" />
        <p>
          CloakBrowser Manager (Custom) · bản fork không chính thức. Khóa bản quyền và kho key
          được quản lý tập trung ở mục <span className="font-semibold text-fg">Bản quyền</span>.
        </p>
      </section>

      {error && (
        <div role="alert" className="rounded-xl border border-danger/30 bg-danger/10 p-3 text-xs text-danger flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-danger flex-shrink-0" />
          <span>{error}</span>
        </div>
      )}
    </div>
  );
}
