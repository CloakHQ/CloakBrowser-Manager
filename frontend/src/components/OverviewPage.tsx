import {
  Activity,
  ArrowRight,
  Fingerprint,
  Globe,
  KeyRound,
  Layers,
  Plus,
  Server,
  ShieldCheck,
  Users,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, type Profile, type ProxyAccount } from "../lib/api";
import { PROFILE_STATUS_LABEL, TIER_LABEL, timeAgo } from "../lib/labels";
import {
  StatCard,
  TONE_DOT,
  buildActivity,
  profileProxyLabel,
} from "./ProfileTableParts";

interface SystemStatusLike {
  license_tier?: string | null;
  license_plan?: string | null;
  viewer_mode?: string;
  host_os?: string;
}

interface OverviewPageProps {
  profiles: Profile[];
  systemStatus: SystemStatusLike | null | undefined;
  onNew: () => void;
  onOpenProfiles: () => void;
  onOpenProviders: () => void;
  onOpenLicense: () => void;
  onSelect: (id: string) => void;
}

export function OverviewPage({
  profiles,
  systemStatus,
  onNew,
  onOpenProfiles,
  onOpenProviders,
  onOpenLicense,
  onSelect,
}: OverviewPageProps) {
  const [accounts, setAccounts] = useState<ProxyAccount[]>([]);

  useEffect(() => {
    api.listProxyAccounts().then(setAccounts).catch(() => setAccounts([]));
  }, []);

  const tier = systemStatus?.license_tier ?? "none";
  const plan = systemStatus?.license_plan ?? null;
  const noKey = tier === "none";
  const runningCount = profiles.filter((p) => p.status === "running").length;
  const proxiedCount = profiles.filter((p) => Boolean(profileProxyLabel(p))).length;
  const activity = useMemo(() => buildActivity(profiles), [profiles]);
  const recent = useMemo(
    () =>
      [...profiles]
        .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
        .slice(0, 5),
    [profiles],
  );
  const kiotAccounts = accounts.filter((a) => a.provider === "kiotproxy").length;
  const tmAccounts = accounts.filter((a) => a.provider === "tmproxy").length;

  return (
    <div className="flex w-full flex-col gap-6 px-4 sm:px-6 lg:px-8 py-5">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-fg">Tổng quan hệ thống</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-fg-muted">
            <span className="flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${runningCount > 0 ? "bg-success shadow-[0_0_8px_rgba(16,185,129,0.5)]" : "bg-fg-muted/40"}`} />
              <span className="font-medium text-fg">{runningCount}</span> đang chạy
            </span>
            <span>·</span>
            <span><span className="font-medium text-fg">{profiles.length}</span> hồ sơ</span>
            {systemStatus?.host_os && (
              <>
                <span>·</span>
                <span className="rounded-md border border-line bg-surface-2/60 px-1.5 py-0.2 font-mono text-[10px] text-fg-secondary">
                  {systemStatus.host_os}
                </span>
              </>
            )}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2.5">
          <button
            type="button"
            onClick={onOpenProfiles}
            className="btn-secondary h-8 px-3 text-xs"
          >
            <Fingerprint className="h-3.5 w-3.5 text-fg-muted" />
            <span>Quản lý hồ sơ</span>
          </button>
          <button
            type="button"
            onClick={onOpenProviders}
            className="btn-secondary h-8 px-3 text-xs"
          >
            <Server className="h-3.5 w-3.5 text-fg-muted" />
            <span>Nhà cung cấp</span>
          </button>
          <button onClick={onNew} className="btn-primary h-8 px-3.5 text-xs">
            <Plus className="h-3.5 w-3.5" />
            <span>Hồ sơ mới</span>
          </button>
        </div>
      </div>

      {/* Warning when no license key */}
      {noKey && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-warning/30 bg-warning/10 p-4 shadow-sm backdrop-blur-sm">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-warning/20 border border-warning/30 text-warning">
              <KeyRound className="h-5 w-5" />
            </div>
            <div>
              <p className="text-xs font-semibold text-fg">Chưa có key bản quyền</p>
              <p className="text-xs text-fg-secondary">
                Hồ sơ cần key bản quyền để khởi chạy. Mỗi phiên trình duyệt đang chạy sử dụng trọn một key.
              </p>
            </div>
          </div>
          <button onClick={onOpenLicense} className="btn-secondary border-warning/30 hover:border-warning/50 text-xs">
            Nhập key ngay
          </button>
        </div>
      )}

      {/* 4 Primary Metric Cards */}
      <div className="grid grid-cols-2 gap-3.5 lg:grid-cols-4">
        <StatCard
          icon={<Layers className="h-4 w-4" />}
          iconClass="bg-brand/10 border-brand/25 text-brand-fg"
          label="Tổng hồ sơ"
          value={profiles.length}
          caption="hồ sơ danh tính độc lập"
        />
        <StatCard
          icon={<Activity className="h-4 w-4" />}
          iconClass="bg-success/10 border-success/25 text-success"
          label="Đang chạy"
          value={runningCount}
          caption={noKey ? "thêm key để khởi chạy" : "mỗi phiên dùng một key"}
        />
        <StatCard
          icon={<Globe className="h-4 w-4" />}
          iconClass="bg-info/10 border-info/25 text-info"
          label="Proxy bảo vệ"
          value={
            <div className="flex items-baseline gap-1">
              <span>{proxiedCount}</span>
              <span className="text-sm font-normal text-fg-muted">/{profiles.length}</span>
            </div>
          }
          caption="hồ sơ có gắn proxy"
        />
        <StatCard
          icon={<KeyRound className="h-4 w-4" />}
          iconClass="bg-warning/10 border-warning/25 text-warning"
          label="Gói dịch vụ"
          value={<span className="text-lg font-bold">{TIER_LABEL[tier] ?? tier}</span>}
          caption={
            noKey ? (
              <button
                onClick={onOpenLicense}
                className="font-medium text-brand-fg hover:underline"
              >
                Kích hoạt trong Bản quyền
              </button>
            ) : plan ? (
              `gói: ${plan === "free" ? "miễn phí" : plan}`
            ) : (
              "đã kích hoạt"
            )
          }
        />
      </div>

      {/* Main Grid: Left Recent Profiles (65%), Right Activity & Telemetry (35%) */}
      <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        {/* Left Column: Recent Profiles */}
        <section className="min-w-0 space-y-3" aria-labelledby="recent-profiles-heading">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Fingerprint className="h-4 w-4 text-brand-fg" />
              <h2 id="recent-profiles-heading" className="text-sm font-bold text-fg">
                Hồ sơ gần đây
              </h2>
              <span className="badge-neutral text-[10px] px-1.5 py-0">
                {profiles.length}
              </span>
            </div>
            <button
              type="button"
              onClick={onOpenProfiles}
              className="inline-flex items-center gap-1 text-xs font-semibold text-brand-fg hover:underline"
            >
              Xem tất cả
              <ArrowRight className="h-3.5 w-3.5" />
            </button>
          </div>

          <div className="card overflow-hidden">
            {recent.length === 0 ? (
              <div className="flex flex-col items-center gap-3 px-6 py-12 text-center">
                <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-line/80 bg-surface-2 text-fg-muted">
                  <Users className="h-6 w-6" />
                </div>
                <div>
                  <p className="text-sm font-semibold text-fg">Chưa có hồ sơ nào</p>
                  <p className="text-xs text-fg-muted mt-0.5">Tạo hồ sơ đầu tiên để bắt đầu duyệt web an toàn.</p>
                </div>
                <button onClick={onNew} className="btn-primary mt-2 px-4 py-1.5 text-xs">
                  <Plus className="h-3.5 w-3.5" />
                  Tạo hồ sơ
                </button>
              </div>
            ) : (
              <div className="divide-y divide-line/60">
                {recent.map((profile) => {
                  const proxy = profileProxyLabel(profile);
                  const isRunning = profile.status === "running";
                  return (
                    <div
                      key={profile.id}
                      onClick={() => onSelect(profile.id)}
                      className="group flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-2/60 cursor-pointer"
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <div className={`relative flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-xl border text-xs font-mono font-semibold ${
                          isRunning
                            ? "border-success/30 bg-success/10 text-success"
                            : "border-line bg-surface-2 text-fg-muted"
                        }`}>
                          {profile.name.charAt(0).toUpperCase()}
                          <span
                            className={`absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full border border-surface-1 ${
                              isRunning
                                ? "bg-success"
                                : profile.status === "initializing"
                                  ? "bg-warning"
                                  : "bg-fg-muted/40"
                            }`}
                          />
                        </div>
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="truncate text-xs font-semibold text-fg group-hover:text-brand-fg transition-colors">
                              {profile.name}
                            </span>
                            {profile.tags.slice(0, 2).map((t) => (
                              <span key={t.tag} className="badge-neutral text-[10px] px-1.5 py-0">
                                {t.tag}
                              </span>
                            ))}
                          </div>
                          <div className="flex items-center gap-2 text-[11px] text-fg-muted mt-0.5 truncate">
                            <span>{PROFILE_STATUS_LABEL[profile.status] ?? profile.status}</span>
                            <span>·</span>
                            <span className="font-mono">{proxy || "kết nối trực tiếp"}</span>
                          </div>
                        </div>
                      </div>

                      <div className="flex items-center gap-3 flex-shrink-0">
                        <span className="text-[11px] text-fg-muted font-mono">
                          {timeAgo(profile.updated_at)}
                        </span>
                        <ArrowRight className="h-3.5 w-3.5 text-fg-muted opacity-0 group-hover:opacity-100 transition-opacity" />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </section>

        {/* Right Column: Providers & Activity */}
        <aside className="min-w-0 space-y-4" aria-labelledby="overview-activity-heading">
          {/* Connected Providers Card */}
          <div className="card p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <Server className="h-4 w-4 text-info" />
                <h3 className="text-xs font-bold text-fg uppercase tracking-wider">Nhà cung cấp</h3>
              </div>
              <button
                type="button"
                onClick={onOpenProviders}
                className="text-[11px] font-semibold text-brand-fg hover:underline"
              >
                Chi tiết
              </button>
            </div>
            <div className="space-y-2 text-xs">
              <div className="flex items-center justify-between rounded-xl border border-line/70 bg-surface-2/50 px-3 py-2">
                <span className="font-medium text-fg-secondary">KiotProxy</span>
                <span className="badge-neutral text-[11px]">{kiotAccounts} tài khoản</span>
              </div>
              <div className="flex items-center justify-between rounded-xl border border-line/70 bg-surface-2/50 px-3 py-2">
                <span className="font-medium text-fg-secondary">TMProxy</span>
                <span className="badge-neutral text-[11px]">{tmAccounts} tài khoản</span>
              </div>
            </div>
          </div>

          {/* Activity Feed */}
          <div className="card p-4">
            <div className="flex items-center gap-2 mb-3">
              <Activity className="h-4 w-4 text-brand-fg" />
              <h3 id="overview-activity-heading" className="text-xs font-bold text-fg uppercase tracking-wider">
                Hoạt động gần đây
              </h3>
            </div>
            {activity.length === 0 ? (
              <p className="text-xs text-fg-muted py-2 text-center">Chưa có hoạt động.</p>
            ) : (
              <ol className="space-y-3">
                {activity.map((item) => (
                  <li key={item.key} className="flex gap-2.5 items-start">
                    <span
                      className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[item.tone]}`}
                    />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-xs font-medium text-fg-secondary">{item.text}</p>
                      <p className="text-[10px] text-fg-muted">{timeAgo(item.at)}</p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </div>

          {/* Proxy Guidelines info box */}
          <div className="rounded-2xl border border-line/80 bg-surface-2/40 p-3.5 backdrop-blur-sm">
            <div className="flex items-center gap-2 text-xs font-semibold text-fg-secondary">
              <ShieldCheck className="h-4 w-4 text-success" />
              <span>Nguyên tắc proxy & bản quyền</span>
            </div>
            <p className="mt-1.5 text-xs leading-relaxed text-fg-muted">
              Mỗi key cấp một proxy tại một thời điểm. Hồ sơ dùng chế độ tự động sẽ chọn key còn trống khi mở để đảm bảo danh tính riêng biệt.
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}
