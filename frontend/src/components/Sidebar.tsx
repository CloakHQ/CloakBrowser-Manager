import { Globe, KeyRound, LayoutGrid, LogOut, Power, Server, Settings, Shield } from "lucide-react";
import type { SystemStatus } from "../lib/api";
import { TIER_LABEL } from "../lib/labels";

export type Page = "overview" | "proxy" | "providers" | "license" | "settings";

export const PAGE_TITLE: Record<Page, string> = {
  overview: "Tổng quan",
  proxy: "Proxy",
  providers: "Nhà cung cấp",
  license: "Bản quyền",
  settings: "Cài đặt",
};

const NAV: { key: Page; label: string; icon: typeof LayoutGrid }[] = [
  { key: "overview", label: "Tổng quan", icon: LayoutGrid },
  { key: "proxy", label: "Proxy", icon: Globe },
  { key: "providers", label: "Nhà cung cấp", icon: Server },
  { key: "license", label: "Bản quyền", icon: KeyRound },
  { key: "settings", label: "Cài đặt", icon: Settings },
];

interface SidebarProps {
  page: Page;
  onNavigate: (page: Page) => void;
  status: SystemStatus | null;
  runningCount: number;
  profileCount: number;
  version: string | null;
  authRequired: boolean;
  onLogout: () => void;
  onQuit: () => void;
}

/**
 * Persistent left rail: brand, the four sections, and the plan card. The plan
 * card is the one place the licensing model is always visible — one key per
 * running profile — so it belongs in the shell rather than the status bar.
 */
export function Sidebar({
  page,
  onNavigate,
  status,
  runningCount,
  profileCount,
  version,
  authRequired,
  onLogout,
  onQuit,
}: SidebarProps) {
  const tier = status?.license_tier || "none";
  const plan = status?.license_plan ?? null;
  const planLabel = tier === "none" ? "Chưa có key" : TIER_LABEL[tier] ?? tier;
  const seats = status ? "1 phiên / key" : null;

  return (
    <div className="flex h-full flex-col border-r border-line bg-surface-1">
      {/* Brand */}
      <div className="flex items-center gap-2.5 border-b border-line p-3.5">
        <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-brand to-brand-deep text-white shadow-glow">
          <Shield className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <h1 className="truncate text-sm font-semibold tracking-tight text-fg">CloakBrowser Manager</h1>
            <span className="badge-neutral flex-shrink-0">Tùy chỉnh</span>
          </div>
          <p className="truncate text-2xs text-fg-muted">Quản lý danh tính trình duyệt</p>
        </div>
      </div>

      {/* Navigation */}
      <nav aria-label="Điều hướng" className="flex-1 overflow-y-auto p-2.5">
        <p className="section-title px-2 pb-2 pt-1">Điều hướng</p>
        <ul className="space-y-0.5">
          {NAV.map(({ key, label, icon: Icon }) => (
            <li key={key}>
              <button
                type="button"
                onClick={() => onNavigate(key)}
                aria-current={page === key ? "page" : undefined}
                className="sidebar-item flex items-center gap-2.5 py-2"
                data-selected={page === key}
              >
                <Icon className={`h-4 w-4 flex-shrink-0 ${page === key ? "text-brand-fg" : "text-fg-muted"}`} />
                <span className={`text-sm ${page === key ? "font-medium text-fg" : "text-fg-secondary"}`}>
                  {label}
                </span>
                {key === "license" && tier === "none" && (
                  <span className="badge-warning ml-auto">cần key</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      {/* Plan card */}
      <div className="border-t border-line p-3">
        <button
          type="button"
          onClick={() => onNavigate("license")}
          className="w-full rounded-lg border border-line bg-surface-0/60 p-3 text-left transition-colors hover:border-line-strong hover:bg-surface-2/60"
        >
          <div className="flex items-center justify-between">
            <span className="text-2xs font-medium uppercase tracking-wider text-fg-muted">Gói dịch vụ</span>
            <KeyRound className={`h-3.5 w-3.5 ${tier === "none" ? "text-warning" : "text-brand-fg"}`} />
          </div>
          <div className="mt-1 text-sm font-semibold text-fg">{planLabel}</div>
          <div className="mt-0.5 text-2xs text-fg-muted">
            {plan ? `gói: ${plan === "free" ? "miễn phí" : plan}` : seats ?? "thêm key để khởi chạy"}
          </div>
          <div className="mt-2 flex items-center gap-1.5 text-2xs text-fg-secondary">
            <span className={`h-1.5 w-1.5 rounded-full ${runningCount > 0 ? "bg-success" : "bg-fg-muted/50"}`} />
            {runningCount} đang chạy / {profileCount} hồ sơ
          </div>
        </button>

        <div className="mt-2.5 flex items-center gap-1">
          {authRequired && (
            <button onClick={onLogout} className="rail-item" title="Đăng xuất">
              <LogOut className="h-3.5 w-3.5" />
            </button>
          )}
          <button onClick={onQuit} className="rail-item hover:!bg-danger/15 hover:!text-danger" title="Thoát Manager (dừng máy chủ)">
            <Power className="h-3.5 w-3.5" />
          </button>
          <span className="ml-auto truncate font-mono text-2xs text-fg-muted" title={status?.binary_version}>
            {version ? `v${version}` : status?.binary_version ?? ""}
          </span>
        </div>
      </div>
    </div>
  );
}
