import { useMemo, useState } from "react";
import { Globe, KeyRound, Link2Off, RefreshCw, SlidersHorizontal } from "lucide-react";
import type { Profile } from "../lib/api";
import { PROVIDER_LABEL, StatCard, profileProxyLabel, proxyHost } from "./ProfileTableParts";

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
}

/** Where each profile's traffic exits: manual proxy, rotating provider, or direct. */
export function ProxyPage({ profiles, onEdit }: ProxyPageProps) {
  const [filter, setFilter] = useState<Filter>("all");

  const rows = useMemo(
    () =>
      profiles.filter((p) => {
        const hasProxy = Boolean(profileProxyLabel(p));
        if (filter === "proxied") return hasProxy;
        if (filter === "direct") return !hasProxy;
        return true;
      }),
    [profiles, filter],
  );

  const proxied = profiles.filter((p) => Boolean(profileProxyLabel(p))).length;
  const providerCount = profiles.filter(
    (p) => p.proxy_provider === "kiotproxy" || p.proxy_provider === "tmproxy",
  ).length;
  const running = profiles.filter((p) => p.status === "running" && p.active_proxy).length;

  if (profiles.length === 0) {
    return (
      <div className="empty-state">
        <div className="mb-2 flex h-14 w-14 items-center justify-center rounded-2xl border border-info/30 bg-info/10">
          <Globe className="h-7 w-7 text-info" />
        </div>
        <p className="text-base font-semibold text-fg">Chưa có hồ sơ để cấu hình proxy</p>
        <p className="max-w-md text-sm text-fg-secondary">
          Proxy được đặt riêng cho từng hồ sơ, trong tab Mạng &amp; proxy của hồ sơ đó.
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
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-fg">Proxy</h1>
        <p className="mt-0.5 text-xs text-fg-muted">
          Mỗi hồ sơ có proxy riêng; nhà cung cấp xoay proxy mới mỗi lần khởi chạy.
        </p>
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
          iconClass="bg-brand/10 border-brand/20 text-brand"
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
        <div className="flex items-center gap-0.5 rounded-lg border border-line bg-surface-2 p-0.5" role="group" aria-label="Lọc theo loại proxy">
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
          {rows.length} trong {profiles.length} hồ sơ
        </span>
      </div>

      <div className="card overflow-x-auto">
        <table className="w-full min-w-[44rem] text-left text-xs">
          <thead className="border-b border-line text-2xs uppercase tracking-wide text-fg-muted">
            <tr>
              <th className="px-4 py-2.5 font-medium">Hồ sơ</th>
              <th className="px-4 py-2.5 font-medium">Nguồn</th>
              <th className="px-4 py-2.5 font-medium">Địa chỉ</th>
              <th className="px-4 py-2.5 font-medium">Khu vực</th>
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
              const provider = p.proxy_provider ?? "none";
              const rotating = provider === "kiotproxy" || provider === "tmproxy";
              const address = rotating
                ? (p.active_proxy?.host ?? (p.proxy_provider_key_set ? "lấy mới khi khởi chạy" : "chưa có API key"))
                : (proxyHost(p.proxy) ?? null);
              const region = rotating
                ? provider === "kiotproxy"
                  ? REGION_LABEL[p.kiotproxy_region ?? "random"] ?? "Ngẫu nhiên"
                  : [p.tmproxy_location_id ? `KV #${p.tmproxy_location_id}` : "KV ngẫu nhiên",
                     p.tmproxy_isp_id ? `ISP #${p.tmproxy_isp_id}` : null]
                      .filter(Boolean)
                      .join(" · ")
                : null;
              return (
                <tr key={p.id} className="border-b border-line/60 last:border-0 hover:bg-surface-3/40">
                  <td className="px-4 py-2.5">
                    <div className="font-medium text-fg">{p.name}</div>
                    <div className="mt-0.5 text-2xs text-fg-muted">
                      {p.status === "running" ? "đang chạy" : "đã dừng"}
                    </div>
                  </td>
                  <td className="px-4 py-2.5">
                    <span className={rotating ? "badge-info" : address ? "badge-neutral" : "badge-neutral"}>
                      {PROVIDER_LABEL[provider] ?? provider}
                    </span>
                  </td>
                  <td className="max-w-[14rem] truncate px-4 py-2.5 font-mono text-2xs text-fg-secondary">
                    {address ?? (
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
                      onClick={() => onEdit(p.id)}
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
        Proxy được nhập tay hoặc lấy từ nhà cung cấp trong tab{" "}
        <span className="font-medium text-fg-secondary">Mạng &amp; proxy</span> của từng hồ sơ.
        Manager chỉ hiển thị host, không hiển thị tên đăng nhập hay mật khẩu.
      </p>
    </div>
  );
}
