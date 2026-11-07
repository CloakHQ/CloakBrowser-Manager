import { ArrowDown, ArrowUp, Copy, MoreHorizontal, SlidersHorizontal, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Profile } from "../lib/api";

/** Show only host:port — proxy URLs can carry credentials that must not leak. */
export function proxyHost(proxy: string | null): string | null {
  if (!proxy) return null;
  try {
    const url = new URL(proxy.includes("://") ? proxy : `http://${proxy}`);
    return url.host;
  } catch {
    return proxy.replace(/^[^@]*@/, "");
  }
}

export const PROVIDER_LABEL: Record<string, string> = {
  none: "Tùy chỉnh",
  manual: "Tùy chỉnh",
  kiotproxy: "KiotProxy",
  tmproxy: "TMProxy",
};

/**
 * How a profile's traffic leaves. A rotating provider counts as proxied even
 * before the first launch — the address is only known once it has fetched one.
 */
export function profileProxyLabel(p: Profile): string | null {
  const provider = String(p.proxy_provider ?? "");
  const rotating = provider !== "" && provider !== "none" && provider !== "manual";
  if (rotating) return p.active_proxy?.host ?? PROVIDER_LABEL[provider] ?? provider;
  return proxyHost(p.proxy);
}

export interface ActivityItem {
  key: string;
  at: string;
  text: ReactNode;
  tone: "brand" | "success" | "muted" | "warning";
}

export const TONE_DOT: Record<ActivityItem["tone"], string> = {
  brand: "bg-brand",
  success: "bg-success",
  muted: "bg-fg-muted/60",
  warning: "bg-warning",
};

/**
 * Recent activity derived only from data the server actually reports
 * (created/updated timestamps) — no invented events.
 */
export function buildActivity(profiles: Profile[]): ActivityItem[] {
  const items: ActivityItem[] = [];
  for (const p of profiles) {
    const name = <span className="font-medium text-fg">{p.name}</span>;
    items.push({ key: `${p.id}:c`, at: p.created_at, text: <>Đã tạo hồ sơ {name}</>, tone: "brand" });
    const created = Date.parse(p.created_at);
    const updated = Date.parse(p.updated_at);
    if (!Number.isNaN(updated) && updated - created > 60_000) {
      items.push({
        key: `${p.id}:u`,
        at: p.updated_at,
        text: <>Đã cập nhật hồ sơ {name}</>,
        tone: p.last_error ? "warning" : "muted",
      });
    }
  }
  items.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  return items.slice(0, 6);
}

export function StatCard({
  icon,
  iconClass,
  label,
  value,
  caption,
}: {
  icon: ReactNode;
  iconClass: string;
  label: string;
  value: ReactNode;
  caption: ReactNode;
}) {
  return (
    <div className="group relative overflow-hidden rounded-2xl border border-line/80 bg-surface-1/90 p-4 shadow-card backdrop-blur-md transition-all duration-200 hover:border-line-strong hover:shadow-elevated">
      <div className="absolute -right-6 -top-6 h-20 w-20 rounded-full bg-brand/5 blur-xl pointer-events-none group-hover:bg-brand/10 transition-colors" />
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider text-fg-muted">{label}</span>
        <span className={`flex h-8 w-8 items-center justify-center rounded-xl border shadow-sm transition-transform group-hover:scale-105 ${iconClass}`}>
          {icon}
        </span>
      </div>
      <div className="mt-2.5 text-2xl font-bold tracking-tight text-fg">{value}</div>
      <div className="mt-1 truncate text-xs text-fg-muted">{caption}</div>
    </div>
  );
}

interface RowMenuProps {
  profile: Profile;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onMoveUp?: () => void;
  onMoveDown?: () => void;
  canMoveUp?: boolean;
  canMoveDown?: boolean;
  /** Open upward — the table clips overflow, so the last rows need it. */
  flipUp?: boolean;
}

export function RowMenu({
  profile,
  onEdit,
  onDuplicate,
  onDelete,
  onMoveUp,
  onMoveDown,
  canMoveUp = false,
  canMoveDown = false,
  flipUp = false,
}: RowMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const running = profile.status !== "stopped";

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    ref.current?.querySelector<HTMLButtonElement>("[role=menuitem]:not(:disabled)")?.focus();
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const item = (
    label: string,
    icon: ReactNode,
    action: () => void,
    opts: { danger?: boolean; disabled?: boolean; title?: string } = {},
  ) => (
    <button
      type="button"
      role="menuitem"
      disabled={opts.disabled}
      title={opts.title}
      onClick={() => {
        setOpen(false);
        action();
      }}
      className={`flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
        opts.danger ? "text-danger hover:bg-danger/10" : "text-fg-secondary hover:bg-surface-3 hover:text-fg"
      }`}
    >
      {icon}
      {label}
    </button>
  );

  return (
    <div ref={ref} className="relative" onClick={(e) => e.stopPropagation()}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="rail-item h-7 w-7"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Thao tác khác cho ${profile.name}`}
      >
        <MoreHorizontal className="h-4 w-4" />
      </button>
      {open && (
        <div
          role="menu"
          className={`absolute right-0 z-popover w-48 rounded-lg border border-line bg-surface-2 p-1 shadow-popover ${
            flipUp ? "bottom-full mb-1" : "top-full mt-1"
          }`}
        >
          {item("Mở cấu hình", <SlidersHorizontal className="h-3.5 w-3.5" />, onEdit)}
          {item("Sao chép hồ sơ", <Copy className="h-3.5 w-3.5" />, onDuplicate)}
          {(onMoveUp || onMoveDown) && (
            <>
              <div className="my-1 h-px bg-line" />
              {onMoveUp && (
                <button
                  type="button"
                  role="menuitem"
                  disabled={!canMoveUp}
                  onClick={() => {
                    setOpen(false);
                    onMoveUp();
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-fg-secondary transition-colors hover:bg-surface-3 hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <ArrowUp className="h-3.5 w-3.5" />
                  Di chuyển lên
                </button>
              )}
              {onMoveDown && (
                <button
                  type="button"
                  role="menuitem"
                  disabled={!canMoveDown}
                  onClick={() => {
                    setOpen(false);
                    onMoveDown();
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-fg-secondary transition-colors hover:bg-surface-3 hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <ArrowDown className="h-3.5 w-3.5" />
                  Di chuyển xuống
                </button>
              )}
            </>
          )}
          <div className="my-1 h-px bg-line" />
          {item("Xóa hồ sơ", <Trash2 className="h-3.5 w-3.5" />, onDelete, {
            danger: true,
            disabled: running,
            title: running ? "Dừng hồ sơ trước khi xóa" : undefined,
          })}
        </div>
      )}
    </div>
  );
}
