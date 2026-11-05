import { Fingerprint, KeyRound, LayoutGrid, LogOut, Power, Server, Settings, Shield } from "lucide-react";
import type { SystemStatus } from "../lib/api";
import { TIER_LABEL } from "../lib/labels";

export type Page = "overview" | "profiles" | "providers" | "license" | "settings";

export const PAGE_TITLE: Record<Page, string> = {
  overview: "Tổng quan",
  profiles: "Hồ sơ",
  providers: "Nhà cung cấp",
  license: "Bản quyền",
  settings: "Cài đặt",
};

const NAV: { key: Page; label: string; icon: typeof LayoutGrid }[] = [
  { key: "overview", label: "Tổng quan", icon: LayoutGrid },
  { key: "profiles", label: "Hồ sơ", icon: Fingerprint },
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
    <div className="flex h-full flex-col border-r border-line/80 bg-surface-1 select-none">
      {/* Brand Header */}
      <div className="flex items-center gap-3 border-b border-line/75 px-4 py-3.5">
        <div className="relative flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-brand via-brand to-brand-deep text-white shadow-glow">
          <Shield className="h-5 w-5 drop-shadow-sm" />
          <span className="absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full border-2 border-surface-1 bg-success" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-sm font-bold tracking-tight text-fg">CloakBrowser</span>
            <span className="rounded-md border border-brand/30 bg-brand/10 px-1.5 py-0.2 text-[10px] font-semibold uppercase tracking-wider text-brand-fg">
              Custom
            </span>
          </div>
          <p className="truncate text-[11px] text-fg-muted">Quản lý danh tính trình duyệt</p>
        </div>
      </div>

      {/* Navigation */}
      <nav aria-label="Điều hướng" className="flex-1 overflow-y-auto px-2.5 py-3 space-y-1">
        <div className="px-2.5 pb-1.5 pt-0.5 text-[10px] font-semibold uppercase tracking-wider text-fg-muted/80">
          Menu chính
        </div>
        <ul className="space-y-1">
          {NAV.map(({ key, label, icon: Icon }) => {
            const isSelected = page === key;
            return (
              <li key={key}>
                <button
                  type="button"
                  onClick={() => onNavigate(key)}
                  aria-current={isSelected ? "page" : undefined}
                  className={`group flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-xs font-medium transition-all duration-150 ${
                    isSelected
                      ? "bg-brand/15 text-fg font-semibold shadow-sm border border-brand/30"
                      : "text-fg-secondary hover:bg-surface-2/90 hover:text-fg border border-transparent"
                  }`}
                  data-selected={isSelected}
                >
                  <div
                    className={`flex h-6 w-6 items-center justify-center rounded-lg transition-colors ${
                      isSelected
                        ? "text-brand-fg"
                        : "text-fg-muted group-hover:text-fg-secondary"
                    }`}
                  >
                    <Icon className="h-4 w-4" />
                  </div>
                  <span className="truncate">{label}</span>
                  {key === "profiles" && runningCount > 0 && (
                    <span className="ml-auto flex items-center gap-1 rounded-full bg-success/15 border border-success/30 px-2 py-0.5 text-[10px] font-semibold text-success">
                      <span className="h-1.5 w-1.5 rounded-full bg-success animate-pulse" />
                      {runningCount}
                    </span>
                  )}
                  {key === "license" && tier === "none" && (
                    <span className="badge-warning ml-auto text-[10px] py-0 px-1.5">
                      cần key
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      </nav>

      {/* Footer / Plan Widget */}
      <div className="border-t border-line/75 p-3">
        <button
          type="button"
          onClick={() => onNavigate("license")}
          className="group w-full rounded-xl border border-line/80 bg-surface-2/60 p-3 text-left transition-all duration-150 hover:border-brand/40 hover:bg-surface-2 hover:shadow-card"
        >
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-muted">
              Bản quyền
            </span>
            <KeyRound
              className={`h-3.5 w-3.5 transition-transform group-hover:scale-110 ${
                tier === "none" ? "text-warning" : "text-brand-fg"
              }`}
            />
          </div>
          <div className="mt-1 flex items-baseline justify-between gap-1">
            <span className="text-xs font-bold text-fg truncate">{planLabel}</span>
            <span className="text-[10px] font-medium text-fg-muted">
              {plan ? (plan === "free" ? "miễn phí" : plan) : (seats ?? "cần key")}
            </span>
          </div>
          <div className="mt-2.5 flex items-center justify-between text-[11px] text-fg-secondary border-t border-line/60 pt-2">
            <span className="flex items-center gap-1.5">
              <span
                className={`h-2 w-2 rounded-full ${
                  runningCount > 0 ? "bg-success shadow-[0_0_8px_rgba(16,185,129,0.5)]" : "bg-fg-muted/40"
                }`}
              />
              <span>{runningCount} đang chạy</span>
            </span>
            <span className="font-mono text-fg-muted text-[10px]">
              {profileCount} hồ sơ
            </span>
          </div>
        </button>

        <div className="mt-2.5 flex items-center justify-between px-1">
          <div className="flex items-center gap-1">
            {authRequired && (
              <button onClick={onLogout} className="rail-item h-7 w-7" title="Đăng xuất">
                <LogOut className="h-3.5 w-3.5" />
              </button>
            )}
            <button
              onClick={onQuit}
              className="rail-item h-7 w-7 text-fg-muted hover:!bg-danger/15 hover:!text-danger"
              title="Thoát Manager (dừng máy chủ)"
            >
              <Power className="h-3.5 w-3.5" />
            </button>
          </div>
          <span
            className="truncate font-mono text-[10px] text-fg-muted"
            title={status?.binary_version}
          >
            {version ? `v${version}` : status?.binary_version ?? ""}
          </span>
        </div>
      </div>
    </div>
  );
}
