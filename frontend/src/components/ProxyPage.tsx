import { useMemo, useState } from "react";
import {
  Globe,
  KeyRound,
  Link2Off,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Users,
} from "lucide-react";
import type { Profile } from "../lib/api";
import { PROVIDER_LABEL, StatCard, profileProxyLabel, proxyHost } from "./ProfileTableParts";
import { ProxyAssignDialog } from "./ProxyAssignDialog";

const REGION_LABEL: Record<string, string> = {
  bac: "Miền Bắc",
  trung: "Miền Trung",
  nam: "Miền Nam",
  random: "Ngẫu nhiên",
};

type Filter = "all" | "proxied" | "direct";

interface ProxyPageProps {
  profiles: Profile[];
  onEdit: (id: string) => void;
  /** Re-read the list after a bulk write so the table reflects what was saved. */
  onApplied?: () => void;
}

/**
 * Where each profile's traffic exits, plus one place to hand the same proxy to
 * a whole group at once. Grouping is derived from the profiles themselves — a
 * proxy is not a stored entity — so nothing here can drift from a profile's
 * own settings.
 */
export function ProxyPage({ profiles, onEdit, onApplied }: ProxyPageProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [assignOpen, setAssignOpen] = useState(false);

  const matching = useMemo(() => {
    const q = search.trim().toLowerCase();
    return profiles.filter((p) => {
      const hasProxy = Boolean(profileProxyLabel(p));
      if (filter === "proxied" && !hasProxy) return false;
      if (filter === "direct" && hasProxy) return false;
      if (!q) return true;
      return (
        p.name.toLowerCase().includes(q) ||
        (profileProxyLabel(p) ?? "").toLowerCase().includes(q)
      );
    });
  }, [profiles, filter, search]);

  // How many profiles share each exit. A manual proxy can be shared; a
  // provider key gets one proxy at a time, hence the separate wording.
  const usage = useMemo(() => {
    const counts = new Map<string, number>();
    for (const p of profiles) {
      const label = profileProxyLabel(p);
      if (label) counts.set(label, (counts.get(label) ?? 0) + 1);
    }
    return counts;
  }, [profiles]);

  const proxied = profiles.filter((p) => Boolean(profileProxyLabel(p))).length;
  const providerCount = profiles.filter(
    (p) => p.proxy_provider === "kiotproxy" || p.proxy_provider === "tmproxy",
  ).length;
  const running = profiles.filter((p) => p.status === "running" && p.active_proxy).length;

  const allVisibleSelected =
    matching.length > 0 && matching.every((p) => selected.has(p.id));

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
      if (allVisibleSelected) matching.forEach((p) => next.delete(p.id));
      else matching.forEach((p) => next.add(p.id));
      return next;
    });

  const openAssign = (ids: string[] = [...selected]) => {
    setSelected(new Set(ids));
    setAssignOpen(true);
  };

  if (profiles.length === 0) {
    return (
      <div className="empty-state">
        <div className="mb-2 flex h-14 w-14 items-center justify-center rounded-2xl border border-info/30 bg-info/10">
          <Globe className="h-7 w-7 text-info" />
        </div>
        <p className="text-base font-semibold text-fg">Chưa có hồ sơ để cấu hình proxy</p>
        <p className="max-w-md text-sm text-fg-secondary">
          Tạo hồ sơ trước, rồi quay lại đây để gán proxy cho một hoặc nhiều hồ sơ cùng lúc.
        </p>
      </div>
    );
  }

  const filters: { key: Filter; label: string }[] = [
    { key: "all", label: "Tất cả" },
    { key: "proxied", label: "Có proxy" },
    { key: "direct", label: "Kết nối trực tiếp" },
  ];

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 px-6 py-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-fg">Proxy</h1>
          <p className="mt-0.5 text-xs text-fg-muted">
            Mỗi hồ sơ một proxy; gán cùng lúc cho cả nhóm, không phải mở từng hồ sơ.
          </p>
        </div>
        <button onClick={() => openAssign()} className="btn-primary">
          <Users className="h-3.5 w-3.5" />
          Gán proxy hàng loạt
        </button>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatCard
          icon={<Globe className="h-3.5 w-3.5" />}
          iconClass="bg-info/10 border-info/20 text-info"
          label="Hồ sơ có proxy"
          value={
            <>
              {proxied}
              <span className="text-base font-normal text-fg-muted">/{profiles.length}</span>
            </>
          }
          caption="còn lại kết nối trực tiếp"
        />
        <StatCard
          icon={<RefreshCw className="h-3.5 w-3.5" />}
          iconClass="bg-brand/10 border-brand/20 text-brand-fg"
          label="Nhà cung cấp"
          value={providerCount}
          caption="hồ sơ dùng KiotProxy hoặc TMProxy"
        />
        <StatCard
          icon={<KeyRound className="h-3.5 w-3.5" />}
          iconClass="bg-success/10 border-success/20 text-success"
          label="Đang dùng proxy"
          value={running}
          caption="hồ sơ đang chạy có IP thoát"
        />
      </div>

      <div className="flex flex-wrap items-center gap-2.5">
        <div
          className="flex items-center gap-0.5 rounded-lg border border-line bg-surface-2 p-0.5"
          role="group"
          aria-label="Lọc theo loại proxy"
        >
          {filters.map((f) => (
            <button
              key={f.key}
              type="button"
              aria-pressed={filter === f.key}
              onClick={() => setFilter(f.key)}
              className={`rounded-md px-2.5 py-1 text-xs transition-colors ${
                filter === f.key ? "bg-surface-3 font-medium text-fg" : "text-fg-muted hover:text-fg"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <span className="text-2xs text-fg-muted">
          {matching.length} trong {profiles.length} hồ sơ
        </span>
        <label className="relative ml-auto w-full sm:w-64">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted" />
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Tìm hồ sơ hoặc proxy…"
            aria-label="Tìm hồ sơ theo tên hoặc proxy"
            className="input w-full py-1.5 pl-8 text-xs"
          />
        </label>
      </div>

      {selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-brand/30 bg-brand-subtle px-3.5 py-2.5">
          <span className="text-xs font-medium text-fg">Đã chọn {selected.size} hồ sơ</span>
          <button type="button" onClick={() => openAssign()} className="btn-primary px-2.5 py-1 text-xs">
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

      <div className="card overflow-x-auto">
        <table className="w-full min-w-[48rem] text-left text-xs">
          <thead className="border-b border-line text-2xs uppercase tracking-wide text-fg-muted">
            <tr>
              <th className="w-9 px-3 py-2.5">
                <input
                  type="checkbox"
                  checked={allVisibleSelected}
                  onChange={toggleAllVisible}
                  aria-label="Chọn tất cả hồ sơ đang hiển thị"
                  className="h-3.5 w-3.5 cursor-pointer rounded border-line-strong"
                />
              </th>
              <th className="px-4 py-2.5 font-medium">Hồ sơ</th>
              <th className="px-4 py-2.5 font-medium">Nguồn</th>
              <th className="px-4 py-2.5 font-medium">Địa chỉ</th>
              <th className="px-4 py-2.5 font-medium">Khu vực</th>
              <th className="px-4 py-2.5 text-right font-medium">Thao tác</th>
            </tr>
          </thead>
          <tbody>
            {matching.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-fg-muted">
                  Không có hồ sơ khớp bộ lọc.
                </td>
              </tr>
            )}
            {matching.map((p) => {
              const provider = p.proxy_provider ?? "none";
              const rotating = provider === "kiotproxy" || provider === "tmproxy";
              const address = rotating
                ? (p.active_proxy?.host ??
                  (p.proxy_provider_key_set ? "lấy mới khi khởi chạy" : "chưa có API key"))
                : (proxyHost(p.proxy) ?? null);
              const region = rotating
                ? provider === "kiotproxy"
                  ? (REGION_LABEL[p.kiotproxy_region ?? "random"] ?? "Ngẫu nhiên")
                  : [
                      p.tmproxy_location_id ? `KV #${p.tmproxy_location_id}` : "KV ngẫu nhiên",
                      p.tmproxy_isp_id ? `ISP #${p.tmproxy_isp_id}` : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")
                : null;
              const label = profileProxyLabel(p);
              const shared = label ? (usage.get(label) ?? 1) : 0;
              const checked = selected.has(p.id);
              return (
                <tr
                  key={p.id}
                  onClick={() => toggle(p.id)}
                  className={`cursor-pointer border-b border-line/60 transition-colors last:border-0 ${
                    checked ? "bg-brand/5" : "hover:bg-surface-3/40"
                  }`}
                >
                  <td className="px-3 py-2.5">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggle(p.id)}
                      onClick={(e) => e.stopPropagation()}
                      aria-label={`Chọn ${p.name}`}
                      className="h-3.5 w-3.5 cursor-pointer rounded border-line-strong"
                    />
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="font-medium text-fg">{p.name}</div>
                    <div className="mt-0.5 text-2xs text-fg-muted">
                      {p.status === "running" ? "đang chạy" : "đã dừng"}
                    </div>
                  </td>
                  <td className="px-4 py-2.5">
                    <span className={rotating ? "badge-info" : "badge-neutral"}>
                      {PROVIDER_LABEL[provider] ?? provider}
                    </span>
                  </td>
                  <td className="max-w-[14rem] px-4 py-2.5 font-mono text-2xs text-fg-secondary">
                    {address ? (
                      <span className="inline-flex max-w-full items-center gap-2">
                        <span className="truncate">{address}</span>
                        {shared > 1 && (
                          <span
                            className="badge-neutral flex-shrink-0 px-1.5 py-0 font-sans"
                            title={`${shared} hồ sơ cùng dùng địa chỉ này`}
                          >
                            ×{shared}
                          </span>
                        )}
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 font-sans text-fg-muted">
                        <Link2Off className="h-3 w-3" />
                        trực tiếp
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-fg-muted">{region ?? "—"}</td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onEdit(p.id);
                      }}
                      className="btn-secondary px-2.5 py-1 text-xs"
                    >
                      <SlidersHorizontal className="h-3.5 w-3.5" />
                      Cấu hình
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="text-2xs text-fg-muted">
        Proxy thủ công dùng chung được cho nhiều hồ sơ, kể cả khi chạy cùng lúc. Một API key
        KiotProxy/TMProxy chỉ cấp một proxy tại một thời điểm. Manager chỉ hiển thị host, không
        hiển thị tên đăng nhập hay mật khẩu.
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
