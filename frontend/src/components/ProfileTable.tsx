import {
  Activity,
  Fingerprint,
  Globe,
  KeyRound,
  Layers,
  Loader2,
  Play,
  Plus,
  Search,
  ShieldCheck,
  Square,
} from "lucide-react";
import { useMemo, useState } from "react";
import type { Profile } from "../lib/api";
import { PROFILE_STATUS_LABEL, TIER_LABEL, timeAgo } from "../lib/labels";
import { RowMenu, StatCard, TONE_DOT, buildActivity, profileProxyLabel } from "./ProfileTableParts";

type StatusFilter = "all" | "running" | "stopped";

interface SystemStatusLike {
  license_tier?: string | null;
  license_plan?: string | null;
  viewer_mode?: string;
  host_os?: string;
}

export interface ProfileTableProps {
  profiles: Profile[];
  selectedId: string | null;
  systemStatus: SystemStatusLike | null | undefined;
  onSelect: (id: string) => void;
  onEdit: (id: string) => void;
  onNew: () => void;
  onOpenSettings: () => void;
  onLaunch: (id: string) => Promise<void>;
  onStop: (id: string) => Promise<void>;
  onDuplicate: (id: string) => void;
  onDelete: (id: string) => void;
}

const STATUS_LABEL: Record<string, string> = PROFILE_STATUS_LABEL;
const relativeTime = timeAgo;

/**
 * Overview page: stat tiles, the profile inventory with per-row actions, and
 * recent activity. Deliberately no bulk launch — each running profile leases
 * one key and the backend enforces the seat limit.
 */
export function ProfileTable({
  profiles,
  selectedId,
  systemStatus,
  onSelect,
  onEdit,
  onNew,
  onOpenSettings,
  onLaunch,
  onStop,
  onDuplicate,
  onDelete,
}: ProfileTableProps) {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [busy, setBusy] = useState<Set<string>>(() => new Set());

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    return profiles.filter((p) => {
      if (statusFilter === "running" && p.status === "stopped") return false;
      if (statusFilter === "stopped" && p.status !== "stopped") return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) ||
        String(p.fingerprint_seed).includes(q) ||
        (profileProxyLabel(p) ?? "").toLowerCase().includes(q) ||
        p.tags.some((t) => t.tag.toLowerCase().includes(q))
      );
    });
  }, [profiles, search, statusFilter]);

  const activity = useMemo(() => buildActivity(profiles), [profiles]);

  const tier = systemStatus?.license_tier ?? "none";
  const noKey = tier === "none";
  // The profile list is polled every 3s, so it is fresher than system status.
  const runningCount = profiles.filter((p) => p.status === "running").length;
  const proxyCount = profiles.filter((p) => Boolean(profileProxyLabel(p))).length;
  const plan = systemStatus?.license_plan ?? null;

  const runBusy = async (id: string, fn: (id: string) => Promise<void>) => {
    setBusy((prev) => new Set(prev).add(id));
    try {
      await fn(id);
    } catch {
      // The caller reports the failure (banner/toast); just clear the spinner.
    } finally {
      setBusy((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  if (profiles.length === 0) {
    return (
      <div className="empty-state">
        <div className="relative mb-2 flex h-16 w-16 items-center justify-center rounded-2xl border border-brand/30 bg-gradient-to-br from-brand/20 to-brand-deep/5 shadow-glow">
          <Fingerprint className="h-8 w-8 text-brand" />
        </div>
        <p className="text-lg font-semibold text-fg">Chưa có hồ sơ</p>
        <p className="max-w-md text-sm leading-relaxed text-fg-secondary">
          Mỗi hồ sơ là một danh tính trình duyệt riêng — có vân tay, proxy, cookie và lịch sử riêng.
        </p>
        <div className="my-2 flex flex-wrap items-center justify-center gap-2 text-xs text-fg-muted">
          <span className="badge-neutral px-3 py-1">
            <Fingerprint className="h-3 w-3 text-brand" />
            Vân tay độc lập
          </span>
          <span className="badge-neutral px-3 py-1">
            <Globe className="h-3 w-3 text-info" />
            Proxy và GeoIP tự động
          </span>
          <span className="badge-neutral px-3 py-1">
            <ShieldCheck className="h-3 w-3 text-success" />
            Cách ly hoàn toàn
          </span>
        </div>
        <button onClick={onNew} className="btn-primary mt-2 px-5 py-2">
          <Plus className="h-4 w-4" />
          Tạo hồ sơ đầu tiên
        </button>
      </div>
    );
  }

  const filters: { key: StatusFilter; label: string }[] = [
    { key: "all", label: "Tất cả" },
    { key: "running", label: "Đang chạy" },
    { key: "stopped", label: "Đã dừng" },
  ];

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-6 py-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">Tổng quan</h1>
          <p className="mt-0.5 text-xs text-fg-muted">
            {runningCount} đang chạy
            {systemStatus?.viewer_mode && ` · ${systemStatus.viewer_mode}`}
            {systemStatus?.host_os && ` · ${systemStatus.host_os}`}
          </p>
        </div>
        <button onClick={onNew} className="btn-primary">
          <Plus className="h-3.5 w-3.5" />
          Hồ sơ mới
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          icon={<Layers className="h-3.5 w-3.5" />}
          iconClass="bg-brand/10 border-brand/20 text-brand"
          label="Hồ sơ"
          value={profiles.length}
          caption="tổng số hồ sơ trình duyệt"
        />
        <StatCard
          icon={<Activity className="h-3.5 w-3.5" />}
          iconClass="bg-success/10 border-success/20 text-success"
          label="Đang chạy"
          value={runningCount}
          caption={noKey ? "thêm key trong Cài đặt" : "mỗi phiên dùng một key"}
        />
        <StatCard
          icon={<Globe className="h-3.5 w-3.5" />}
          iconClass="bg-info/10 border-info/20 text-info"
          label="Proxy"
          value={
            <>
              {proxyCount}
              <span className="text-base font-normal text-fg-muted">/{profiles.length}</span>
            </>
          }
          caption="hồ sơ có gắn proxy"
        />
        <StatCard
          icon={<KeyRound className="h-3.5 w-3.5" />}
          iconClass="bg-warning/10 border-warning/20 text-warning"
          label="Gói dịch vụ"
          value={<span className="text-lg">{TIER_LABEL[tier] ?? tier}</span>}
          caption={
            noKey ? (
              <button onClick={onOpenSettings} className="font-medium text-brand hover:underline">
                Thêm key trong Cài đặt
              </button>
            ) : plan ? (
              `gói: ${plan === "free" ? "miễn phí" : plan}`
            ) : (
              "đã kích hoạt"
            )
          }
        />
      </div>

      <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1fr)_18rem]">
        <section className="min-w-0" aria-labelledby="profiles-heading">
          <div className="mb-3 flex flex-wrap items-center gap-2.5">
            <h2 id="profiles-heading" className="mr-1 text-sm font-semibold text-fg">
              Danh sách hồ sơ
            </h2>
            <div className="flex items-center gap-0.5 rounded-lg border border-line bg-surface-2 p-0.5" role="group" aria-label="Lọc theo trạng thái">
              {filters.map((f) => (
                <button
                  key={f.key}
                  type="button"
                  aria-pressed={statusFilter === f.key}
                  onClick={() => setStatusFilter(f.key)}
                  className={`rounded-md px-2.5 py-1 text-xs transition-colors ${
                    statusFilter === f.key ? "bg-surface-3 font-medium text-fg" : "text-fg-muted hover:text-fg"
                  }`}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <label className="relative ml-auto w-full sm:w-64">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted" />
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Tìm theo tên, seed, proxy, tag…"
                aria-label="Tìm hồ sơ"
                className="input w-full py-1.5 pl-8 text-xs"
              />
            </label>
          </div>

          <div className="card overflow-x-auto">
            <table className="w-full min-w-[40rem] text-left text-xs">
              <thead className="border-b border-line text-2xs uppercase tracking-wide text-fg-muted">
                <tr>
                  <th className="px-4 py-2.5 font-medium">Hồ sơ</th>
                  <th className="px-4 py-2.5 font-medium">Trạng thái</th>
                  <th className="px-4 py-2.5 font-medium">Proxy</th>
                  <th className="px-4 py-2.5 font-medium">Cập nhật</th>
                  <th className="px-4 py-2.5 text-right font-medium">Thao tác</th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-4 py-8 text-center text-fg-muted">
                      Không có hồ sơ khớp bộ lọc.
                    </td>
                  </tr>
                )}
                {rows.map((p) => {
                  const stopped = p.status === "stopped";
                  const isBusy = busy.has(p.id) || p.status === "initializing";
                  const proxy = profileProxyLabel(p);
                  return (
                    <tr
                      key={p.id}
                      onClick={() => onSelect(p.id)}
                      className={`cursor-pointer border-b border-line/60 transition-colors last:border-0 hover:bg-surface-3/50 ${
                        selectedId === p.id ? "bg-brand/5" : ""
                      }`}
                    >
                      <td className="px-4 py-2.5">
                        <div className="font-medium text-fg">{p.name}</div>
                        <div className="mt-0.5 flex flex-wrap items-center gap-1 text-2xs text-fg-muted">
                          <span>seed {p.fingerprint_seed}</span>
                          {p.tags.slice(0, 3).map((t) => (
                            <span key={t.tag} className="badge-neutral px-1.5 py-0">
                              {t.tag}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td className="px-4 py-2.5">
                        <span className="inline-flex items-center gap-1.5 text-fg-secondary">
                          <span
                            className={`h-1.5 w-1.5 rounded-full ${
                              p.status === "running" ? "bg-success" : stopped ? "bg-fg-muted/60" : "bg-warning"
                            }`}
                          />
                          {STATUS_LABEL[p.status] ?? p.status}
                        </span>
                      </td>
                      <td className="max-w-[12rem] truncate px-4 py-2.5 font-mono text-2xs text-fg-secondary">
                        {proxy ?? <span className="font-sans text-fg-muted">Kết nối trực tiếp</span>}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2.5 text-fg-muted">{relativeTime(p.updated_at)}</td>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center justify-end gap-1" onClick={(e) => e.stopPropagation()}>
                          <button
                            type="button"
                            disabled={isBusy}
                            onClick={() => runBusy(p.id, stopped ? onLaunch : onStop)}
                            className={stopped ? "btn-primary px-2.5 py-1 text-xs" : "btn-secondary px-2.5 py-1 text-xs"}
                            aria-label={stopped ? `Mở ${p.name}` : `Dừng ${p.name}`}
                          >
                            {isBusy ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : stopped ? (
                              <Play className="h-3.5 w-3.5" />
                            ) : (
                              <Square className="h-3.5 w-3.5" />
                            )}
                            {stopped ? "Mở" : "Dừng"}
                          </button>
                          <RowMenu
                            profile={p}
                            onEdit={() => onEdit(p.id)}
                            onDuplicate={() => onDuplicate(p.id)}
                            onDelete={() => onDelete(p.id)}
                          />
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <aside className="min-w-0" aria-labelledby="activity-heading">
          <h2 id="activity-heading" className="mb-3 text-sm font-semibold text-fg">
            Hoạt động gần đây
          </h2>
          <div className="card p-4">
            {activity.length === 0 ? (
              <p className="text-xs text-fg-muted">Chưa có hoạt động.</p>
            ) : (
              <ol className="space-y-3">
                {activity.map((a) => (
                  <li key={a.key} className="flex gap-2.5">
                    <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[a.tone]}`} />
                    <div className="min-w-0">
                      <p className="truncate text-xs text-fg-secondary">{a.text}</p>
                      <p className="text-2xs text-fg-muted">{relativeTime(a.at)}</p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
