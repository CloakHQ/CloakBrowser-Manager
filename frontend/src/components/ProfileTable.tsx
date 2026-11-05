import {
  Fingerprint,
  Globe,
  KeyRound,
  Link2Off,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Square,
  Users,
} from "lucide-react";
import { useMemo, useState } from "react";
import type { Profile } from "../lib/api";
import { PROFILE_STATUS_LABEL, timeAgo } from "../lib/labels";
import { ProxyAssignDialog } from "./ProxyAssignDialog";
import {
  PROVIDER_LABEL,
  RowMenu,
  profileProxyLabel,
  proxyHost,
} from "./ProfileTableParts";

type StatusFilter = "all" | "running" | "stopped";

const REGION_LABEL: Record<string, string> = {
  bac: "Miền Bắc",
  trung: "Miền Trung",
  nam: "Miền Nam",
  random: "Ngẫu nhiên",
};

export interface ProfileTableProps {
  profiles: Profile[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onEdit: (id: string) => void;
  onNew: () => void;
  onLaunch: (id: string) => Promise<void>;
  onStop: (id: string) => Promise<void>;
  onDuplicate: (id: string) => void;
  onDelete: (id: string) => void;
  /** Persist a manual order (the sidebar list's drag-reorder, in menu form). */
  onReorder: (orderedIds: string[]) => void;
  /** Refresh the profile list after a bulk proxy write. */
  onApplied?: () => void;
}

const STATUS_LABEL: Record<string, string> = PROFILE_STATUS_LABEL;
const relativeTime = timeAgo;

/**
 * Profiles page: the inventory plus per-row actions and bulk proxy assignment.
 * The separate Proxy page was folded in here — it listed the same profiles.
 */
export function ProfileTable({
  profiles,
  selectedId,
  onSelect,
  onEdit,
  onNew,
  onLaunch,
  onStop,
  onDuplicate,
  onDelete,
  onReorder,
  onApplied,
}: ProfileTableProps) {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [busy, setBusy] = useState<Set<string>>(() => new Set());
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [assignOpen, setAssignOpen] = useState(false);

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

  const usage = useMemo(() => {
    const counts = new Map<string, number>();
    for (const profile of profiles) {
      const label = profileProxyLabel(profile);
      if (label) counts.set(label, (counts.get(label) ?? 0) + 1);
    }
    return counts;
  }, [profiles]);

  const runningCount = profiles.filter((p) => p.status === "running").length;
  const proxiedCount = profiles.filter((p) => Boolean(profileProxyLabel(p))).length;
  const providerCount = profiles.filter(
    (p) => p.proxy_provider === "kiotproxy" || p.proxy_provider === "tmproxy",
  ).length;

  const allVisibleSelected =
    rows.length > 0 && rows.every((p) => selected.has(p.id));

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleAllVisible = () =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) rows.forEach((p) => next.delete(p.id));
      else rows.forEach((p) => next.add(p.id));
      return next;
    });

  const openAssign = (ids: string[] = [...selected]) => {
    setSelected(new Set(ids));
    setAssignOpen(true);
  };

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
          <Fingerprint className="h-8 w-8 text-brand-fg" />
        </div>
        <p className="text-lg font-semibold text-fg">Chưa có hồ sơ</p>
        <p className="max-w-md text-sm leading-relaxed text-fg-secondary">
          Mỗi hồ sơ là một danh tính trình duyệt riêng — có vân tay, proxy, cookie
          và lịch sử riêng.
        </p>
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
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-6 py-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-fg">Hồ sơ trình duyệt</h1>
          <p className="mt-1 text-xs text-fg-muted">
            <span className="font-medium text-fg">{runningCount}</span> đang chạy · <span className="font-medium text-fg">{profiles.length}</span> hồ sơ đã tạo
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2.5">
          <button onClick={() => openAssign()} className="btn-secondary h-8 px-3 text-xs">
            <Users className="h-3.5 w-3.5 text-fg-muted" />
            <span>Gán proxy hàng loạt</span>
          </button>
          <button onClick={onNew} className="btn-primary h-8 px-3 text-xs">
            <Plus className="h-3.5 w-3.5" />
            <span>Hồ sơ mới</span>
          </button>
        </div>
      </div>

      {/* Compact Metric Strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="group relative overflow-hidden rounded-2xl border border-line/80 bg-surface-1/90 p-3.5 shadow-card backdrop-blur-md transition-all hover:border-line-strong">
          <div className="flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-fg-muted">
            <span>Hồ sơ</span>
            <Fingerprint className="h-4 w-4 text-brand-fg" />
          </div>
          <div className="mt-2 text-xl font-bold text-fg">{profiles.length}</div>
          <div className="mt-0.5 text-[11px] text-fg-muted">tổng số hồ sơ</div>
        </div>
        <div className="group relative overflow-hidden rounded-2xl border border-line/80 bg-surface-1/90 p-3.5 shadow-card backdrop-blur-md transition-all hover:border-line-strong">
          <div className="flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-fg-muted">
            <span>Đang chạy</span>
            <Play className="h-4 w-4 text-success" />
          </div>
          <div className="mt-2 text-xl font-bold text-success flex items-center gap-1.5">
            {runningCount > 0 && <span className="h-2 w-2 rounded-full bg-success animate-pulse" />}
            {runningCount}
          </div>
          <div className="mt-0.5 text-[11px] text-fg-muted">mỗi phiên một key</div>
        </div>
        <div className="group relative overflow-hidden rounded-2xl border border-line/80 bg-surface-1/90 p-3.5 shadow-card backdrop-blur-md transition-all hover:border-line-strong">
          <div className="flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-fg-muted">
            <span>Có proxy</span>
            <Globe className="h-4 w-4 text-info" />
          </div>
          <div className="mt-2 text-xl font-bold text-fg">
            {proxiedCount}
            <span className="text-xs font-normal text-fg-muted">/{profiles.length}</span>
          </div>
          <div className="mt-0.5 text-[11px] text-fg-muted">còn lại trực tiếp</div>
        </div>
        <div className="group relative overflow-hidden rounded-2xl border border-line/80 bg-surface-1/90 p-3.5 shadow-card backdrop-blur-md transition-all hover:border-line-strong">
          <div className="flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-fg-muted">
            <span>Nhà cung cấp</span>
            <RefreshCw className="h-4 w-4 text-brand-fg" />
          </div>
          <div className="mt-2 text-xl font-bold text-fg">{providerCount}</div>
          <div className="mt-0.5 text-[11px] text-fg-muted">KiotProxy / TMProxy</div>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2.5">
        <div
          className="flex items-center gap-0.5 rounded-lg border border-line bg-surface-2 p-0.5"
          role="group"
          aria-label="Lọc theo trạng thái"
        >
          {filters.map((f) => (
            <button
              key={f.key}
              type="button"
              aria-pressed={statusFilter === f.key}
              onClick={() => setStatusFilter(f.key)}
              className={`rounded-md px-2.5 py-1 text-xs transition-colors ${
                statusFilter === f.key
                  ? "bg-surface-3 font-medium text-fg"
                  : "text-fg-muted hover:text-fg"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <span className="text-2xs text-fg-muted">
          {rows.length} trong {profiles.length} hồ sơ
        </span>
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

      {selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-brand/30 bg-brand-subtle px-3.5 py-2.5">
          <span className="text-xs font-medium text-fg">
            Đã chọn {selected.size} hồ sơ
          </span>
          <button
            type="button"
            onClick={() => openAssign()}
            className="btn-primary px-2.5 py-1 text-xs"
          >
            <Users className="h-3.5 w-3.5" />
            Gán proxy
          </button>
          <button
            type="button"
            onClick={() => setSelected(new Set())}
            className="btn-ghost px-2 py-1 text-xs"
          >
            Bỏ chọn
          </button>
        </div>
      )}

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[58rem] text-left text-xs">
            <thead className="border-b border-line/80 bg-surface-2/60 text-[11px] font-semibold uppercase tracking-wider text-fg-muted select-none">
              <tr>
                <th className="w-10 px-3.5 py-3">
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleAllVisible}
                    aria-label="Chọn tất cả hồ sơ đang hiển thị"
                    className="h-4 w-4 cursor-pointer rounded-md border-line-strong accent-brand"
                  />
                </th>
                <th className="px-4 py-3">Hồ sơ</th>
                <th className="px-4 py-3">Trạng thái</th>
                <th className="px-4 py-3">Nguồn</th>
                <th className="px-4 py-3">Địa chỉ</th>
                <th className="px-4 py-3">Khu vực</th>
                <th className="px-4 py-3">Cập nhật</th>
                <th className="px-4 py-3 text-right">Thao tác</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line/60">
              {rows.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-4 py-12 text-center text-fg-muted">
                    Không có hồ sơ nào khớp với bộ lọc hiện tại.
                  </td>
                </tr>
              )}
              {rows.map((p, rowIndex) => {
                const stopped = p.status === "stopped";
                const isBusy = busy.has(p.id) || p.status === "initializing";
                const provider = p.proxy_provider ?? "none";
                const rotating = provider === "kiotproxy" || provider === "tmproxy";
                const address = rotating
                  ? (p.active_proxy?.host ??
                    (p.proxy_provider_key_set
                      ? "lấy mới khi khởi chạy"
                      : "chưa có API key"))
                  : (proxyHost(p.proxy) ?? null);
                const region = rotating
                  ? provider === "kiotproxy"
                    ? (REGION_LABEL[p.kiotproxy_region ?? "random"] ?? "Ngẫu nhiên")
                    : [
                        p.tmproxy_location_id
                          ? `KV #${p.tmproxy_location_id}`
                          : "KV ngẫu nhiên",
                        p.tmproxy_isp_id ? `ISP #${p.tmproxy_isp_id}` : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")
                  : null;
                const label = profileProxyLabel(p);
                const shared = label ? (usage.get(label) ?? 1) : 0;
                const checked = selected.has(p.id);
                const orderIndex = profiles.findIndex((x) => x.id === p.id);
                const move = (target: number) => {
                  const ids = profiles.map((x) => x.id);
                  if (target < 0 || target >= ids.length) return;
                  const [moved] = ids.splice(orderIndex, 1);
                  if (moved === undefined) return;
                  ids.splice(target, 0, moved);
                  onReorder(ids);
                };
                const isRunning = p.status === "running";
                return (
                  <tr
                    key={p.id}
                    onClick={() => onSelect(p.id)}
                    className={`group cursor-pointer transition-colors ${
                      checked
                        ? "bg-brand/10 hover:bg-brand/15"
                        : selectedId === p.id
                          ? "bg-brand/8 hover:bg-brand/12"
                          : "hover:bg-surface-2/70"
                    }`}
                  >
                    <td className="px-3.5 py-3">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggle(p.id)}
                        onClick={(e) => e.stopPropagation()}
                        aria-label={`Chọn ${p.name}`}
                        className="h-4 w-4 cursor-pointer rounded-md border-line-strong accent-brand"
                      />
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2.5">
                        <div className={`flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg border text-xs font-mono font-bold ${
                          isRunning
                            ? "border-success/30 bg-success/15 text-success"
                            : "border-line bg-surface-2 text-fg-muted"
                        }`}>
                          {p.name.charAt(0).toUpperCase()}
                        </div>
                        <div className="min-w-0">
                          <div className="font-semibold text-fg group-hover:text-brand-fg transition-colors truncate">
                            {p.name}
                          </div>
                          <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-fg-muted">
                            <span className="font-mono">seed {p.fingerprint_seed}</span>
                            {p.tags.slice(0, 3).map((t) => (
                              <span key={t.tag} className="badge-neutral text-[10px] px-1.5 py-0">
                                {t.tag}
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <span className="inline-flex items-center gap-1.5 text-xs text-fg-secondary">
                        <span
                          className={`h-2 w-2 rounded-full ${
                            isRunning
                              ? "bg-success shadow-[0_0_8px_rgba(16,185,129,0.6)] animate-pulse"
                              : stopped
                                ? "bg-fg-muted/40"
                                : "bg-warning shadow-[0_0_8px_rgba(245,158,11,0.6)]"
                          }`}
                        />
                        <span className="font-medium">{STATUS_LABEL[p.status] ?? p.status}</span>
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span className={rotating ? "badge-info" : "badge-neutral"}>
                        {PROVIDER_LABEL[provider] ?? provider}
                      </span>
                    </td>
                    <td className="max-w-[13rem] px-4 py-3 font-mono text-[11px] text-fg-secondary">
                      {address ? (
                        <span className="inline-flex max-w-full items-center gap-2">
                          <span className="truncate">{address}</span>
                          {shared > 1 && (
                            <span
                              className="badge-neutral flex-shrink-0 px-1.5 py-0 font-sans text-[10px]"
                              title={`${shared} hồ sơ cùng dùng địa chỉ này`}
                            >
                              ×{shared}
                            </span>
                          )}
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5 font-sans text-fg-muted text-[11px]">
                          <Link2Off className="h-3 w-3" />
                          trực tiếp
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-fg-muted text-[11px]">{region ?? "—"}</td>
                    <td className="whitespace-nowrap px-4 py-3 text-fg-muted text-[11px] font-mono">
                      {relativeTime(p.updated_at)}
                    </td>
                    <td className="px-4 py-3">
                      <div
                        className="flex items-center justify-end gap-1.5"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <button
                          type="button"
                          disabled={isBusy}
                          onClick={() => runBusy(p.id, stopped ? onLaunch : onStop)}
                          className={
                            stopped
                              ? "btn-primary h-7 px-2.5 text-xs"
                              : "btn-secondary h-7 px-2.5 text-xs !border-danger/30 !text-danger hover:!bg-danger/10"
                          }
                          aria-label={stopped ? `Mở ${p.name}` : `Dừng ${p.name}`}
                        >
                          {isBusy ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : stopped ? (
                            <Play className="h-3.5 w-3.5 fill-current" />
                          ) : (
                            <Square className="h-3.5 w-3.5 fill-current" />
                          )}
                          <span>{stopped ? "Mở" : "Dừng"}</span>
                        </button>
                        <button
                          type="button"
                          onClick={() => onEdit(p.id)}
                          className="btn-secondary h-7 px-2.5 text-xs"
                          title="Cấu hình hồ sơ và proxy"
                        >
                          <SlidersHorizontal className="h-3.5 w-3.5" />
                          <span>Cấu hình</span>
                        </button>
                        <RowMenu
                          profile={p}
                          onEdit={() => onEdit(p.id)}
                          onDuplicate={() => onDuplicate(p.id)}
                          onDelete={() => onDelete(p.id)}
                          onMoveUp={() => move(orderIndex - 1)}
                          onMoveDown={() => move(orderIndex + 1)}
                          canMoveUp={orderIndex > 0}
                          canMoveDown={
                            orderIndex >= 0 && orderIndex < profiles.length - 1
                          }
                          flipUp={rowIndex >= rows.length - 2}
                        />
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <p className="flex items-start gap-1.5 text-2xs text-fg-muted">
        <KeyRound className="mt-0.5 h-3 w-3 flex-shrink-0 text-warning" />
        KiotProxy và TMProxy giới hạn theo từng key: một key cấp một proxy tại một
        thời điểm. Proxy thủ công dùng chung được cho nhiều hồ sơ, kể cả khi chạy
        cùng lúc.
      </p>

      {assignOpen && (
        <ProxyAssignDialog
          profiles={profiles}
          initialSelection={[...selected]}
          onClose={() => setAssignOpen(false)}
          onApplied={() => onApplied?.()}
        />
      )}
    </div>
  );
}
