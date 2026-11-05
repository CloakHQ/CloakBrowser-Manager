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
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-5 px-6 py-5">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-fg">Bản quyền</h1>
        <p className="mt-0.5 text-xs text-fg-muted">
          Mỗi hồ sơ đang chạy dùng một key. Kho key cho phép nhiều hồ sơ chạy song song.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <StatCard
          icon={<ShieldCheck className="h-3.5 w-3.5" />}
          iconClass="bg-brand/10 border-brand/20 text-brand-fg"
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
      </div>

      <section className="card p-4" aria-labelledby="pool-heading">
        <h2 id="pool-heading" className="mb-3 text-sm font-semibold text-fg">
          Kho key dùng chung
        </h2>
        <KeyPoolPanel />
        <p className="mt-3 text-xs leading-relaxed text-fg-muted">
          Mỗi hồ sơ đang chạy giữ một key trong kho. Hết key trống thì hồ sơ tiếp theo
          phải chờ.{" "}
          <a
            href="https://cloakbrowser.dev/free"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 font-medium text-brand-fg hover:underline"
          >
            <span>Lấy key miễn phí</span>
            <ExternalLink className="h-3 w-3" />
          </a>
          .
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
