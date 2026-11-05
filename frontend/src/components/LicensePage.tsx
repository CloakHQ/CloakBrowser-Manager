import { useEffect, useState } from "react";
import { ExternalLink, KeyRound, Loader2, ShieldCheck } from "lucide-react";
import { api, type ManagerSettings, type SystemStatus } from "../lib/api";
import { TIER_LABEL } from "../lib/labels";
import { KeyPoolPanel } from "./KeyPoolPanel";
import { StatCard } from "./ProfileTableParts";
import { useConfirm } from "./ui/confirm";
import { useToast } from "./ui/toast";

interface LicensePageProps {
  status: SystemStatus | null;
  onStatusChange: (status: SystemStatus) => void;
}

/**
 * License page: the app-wide key, the pool that lets several profiles run at
 * once, and what the current plan allows. Enforcement stays in the binary and
 * the license server — nothing here can raise the seat limit.
 */
export function LicensePage({ status, onStatusChange }: LicensePageProps) {
  const confirm = useConfirm();
  const toast = useToast();
  const [current, setCurrent] = useState<ManagerSettings | null>(null);
  const [licenseKey, setLicenseKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getSettings()
      .then(setCurrent)
      .catch((err) => setError(err instanceof Error ? err.message : "Không thể tải cài đặt"));
  }, []);

  const tier = status?.license_tier ?? "none";
  const plan = status?.license_plan ?? null;

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      // An empty input leaves the stored key untouched; clearing is its own action.
      const next = await api.updateSettings(
        licenseKey.trim() ? { license_key: licenseKey.trim() } : {},
      );
      onStatusChange(next);
      setLicenseKey("");
      setCurrent(await api.getSettings().catch(() => current));
      toast.success("Đã cập nhật key bản quyền");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể lưu key");
    } finally {
      setSaving(false);
    }
  };

  const handleClearKey = async () => {
    const ok = await confirm({
      title: "Xóa key bản quyền?",
      description:
        "Ứng dụng sẽ không còn key dùng chung. Các hồ sơ đang chạy vẫn tiếp tục, " +
        "nhưng lần khởi chạy tiếp theo sẽ cần key trong kho hoặc key dùng chung.",
      confirmLabel: "Xóa key",
      tone: "danger",
    });
    if (!ok) return;
    setClearing(true);
    setError(null);
    try {
      const next = await api.updateSettings({ license_key: "" });
      onStatusChange(next);
      setCurrent(await api.getSettings().catch(() => current));
      toast.success("Đã xóa key bản quyền");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thể xóa key");
    } finally {
      setClearing(false);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-5 px-6 py-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-fg">Bản quyền</h1>
        <p className="mt-0.5 text-xs text-fg-muted">
          Mỗi hồ sơ đang chạy dùng một key. Kho key cho phép nhiều hồ sơ chạy song song.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatCard
          icon={<ShieldCheck className="h-3.5 w-3.5" />}
          iconClass="bg-brand/10 border-brand/20 text-brand"
          label="Gói hiện tại"
          value={<span className="text-lg">{TIER_LABEL[tier] ?? tier}</span>}
          caption={plan ? `gói: ${plan === "free" ? "miễn phí" : plan}` : "chưa xác định"}
        />
        <StatCard
          icon={<KeyRound className="h-3.5 w-3.5" />}
          iconClass="bg-info/10 border-info/20 text-info"
          label="Kho key"
          value={current?.key_pool_size ?? 0}
          caption="key dùng cho các hồ sơ đang chạy"
        />
        <StatCard
          icon={<ShieldCheck className="h-3.5 w-3.5" />}
          iconClass="bg-success/10 border-success/20 text-success"
          label="Key dùng chung"
          value={<span className="text-lg">{current?.license_key_set ? "Đã đặt" : "Chưa có"}</span>}
          caption={current?.license_key_masked ?? "không có key dùng chung"}
        />
      </div>

      <section className="card p-4" aria-labelledby="shared-key-heading">
        <div className="mb-2 flex items-center gap-2">
          <KeyRound className="h-3.5 w-3.5 text-brand" />
          <h2 id="shared-key-heading" className="text-sm font-semibold text-fg">
            Key dùng chung
          </h2>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            className="input flex-1 font-mono"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={licenseKey}
            onChange={(e) => setLicenseKey(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void handleSave();
              }
            }}
            placeholder={
              current?.license_key_set
                ? `Hiện tại: ${current.license_key_masked ?? "đã đặt"} — nhập để thay thế`
                : "cb_… (cần key để khởi chạy hồ sơ)"
            }
            aria-label="Key bản quyền dùng chung"
          />
          <button onClick={handleSave} disabled={saving || !licenseKey.trim()} className="btn-primary">
            {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {saving ? "Đang lưu…" : "Lưu key"}
          </button>
          {current?.license_key_set && (
            <button onClick={handleClearKey} disabled={clearing} className="btn-danger">
              {clearing ? "Đang xóa…" : "Xóa key"}
            </button>
          )}
        </div>
        <p className="mt-2 text-xs leading-relaxed text-fg-muted">
          Key dùng chung chỉ phục vụ một hồ sơ tại một thời điểm. Hồ sơ không ghim key
          riêng sẽ dùng key này khi còn trống.{" "}
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
      </section>

      <section className="card p-4" aria-labelledby="pool-heading">
        <h2 id="pool-heading" className="mb-3 text-sm font-semibold text-fg">
          Kho key dùng chung
        </h2>
        <KeyPoolPanel />
      </section>

      {error && (
        <div role="alert" className="rounded-lg border border-danger/25 bg-danger/10 px-3 py-2 text-xs text-danger">
          {error}
        </div>
      )}
    </div>
  );
}
