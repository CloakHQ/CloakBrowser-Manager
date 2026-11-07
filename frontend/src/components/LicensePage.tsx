import { useEffect, useState } from "react";
import { ExternalLink, KeyRound, ShieldCheck } from "lucide-react";
import { api, type ManagerSettings, type SystemStatus } from "../lib/api";
import { TIER_LABEL } from "../lib/labels";
import { KeyPoolPanel } from "./KeyPoolPanel";
import { StatCard } from "./ProfileTableParts";

interface LicensePageProps {
  status: SystemStatus | null;
  onStatusChange: (status: SystemStatus) => void;
}

/**
 * License page: the key pool that lets several profiles run at once, and what
 * the current plan allows. Enforcement stays in the binary and the license
 * server — nothing here can raise the seat limit.
 */
export function LicensePage({ status }: LicensePageProps) {
  const [current, setCurrent] = useState<ManagerSettings | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getSettings()
      .then(setCurrent)
      .catch((err) => setError(err instanceof Error ? err.message : "Không thể tải cài đặt"));
  }, []);

  const tier = status?.license_tier ?? "none";
  const plan = status?.license_plan ?? null;

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 px-6 py-6">
      <div>
        <h1 className="text-xl font-bold tracking-tight text-fg">Quản lý bản quyền</h1>
        <p className="mt-1 text-xs text-fg-muted">
          Mỗi hồ sơ đang chạy dùng trọn một key. Kho key cho phép khởi chạy song song nhiều hồ sơ cùng lúc.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2">
        <StatCard
          icon={<ShieldCheck className="h-4 w-4" />}
          iconClass="bg-brand/10 border-brand/25 text-brand-fg"
          label="Gói hiện tại"
          value={<span className="text-lg font-bold">{TIER_LABEL[tier] ?? tier}</span>}
          caption={plan ? `gói: ${plan === "free" ? "miễn phí" : plan}` : "chưa xác định"}
        />
        <StatCard
          icon={<KeyRound className="h-4 w-4" />}
          iconClass="bg-info/10 border-info/25 text-info"
          label="Kho key"
          value={<span className="font-bold">{current?.key_pool_size ?? 0}</span>}
          caption="key dùng cho các hồ sơ đang chạy"
        />
      </div>

      <section className="card p-5 shadow-card" aria-labelledby="pool-heading">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 id="pool-heading" className="text-sm font-bold text-fg">
              Kho key dùng chung
            </h2>
            <p className="text-xs text-fg-muted mt-0.5">
              Hệ thống tự động cấp phát key trống cho các hồ sơ khi khởi chạy.
            </p>
          </div>
        </div>
        <KeyPoolPanel />
        <div className="mt-4 flex items-center justify-between rounded-xl border border-line/80 bg-surface-2/40 px-3.5 py-2.5 text-xs text-fg-muted">
          <span>Mỗi hồ sơ đang chạy giữ một key trong kho. Hết key trống thì hồ sơ tiếp theo phải chờ.</span>
          <a
            href="https://cloakbrowser.dev/free"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 font-semibold text-brand-fg hover:underline ml-3 flex-shrink-0"
          >
            <span>Lấy key miễn phí</span>
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        </div>
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
