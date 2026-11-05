import { Copy, MoreHorizontal, SlidersHorizontal, Trash2 } from "lucide-react";
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

export function profileProxyLabel(p: Profile): string | null {
  const provider = String(p.proxy_provider ?? "");
  if (p.active_proxy && provider && provider !== "none" && provider !== "manual") return provider;
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
    <div className="card p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-fg-secondary">{label}</span>
        <span className={`flex h-7 w-7 items-center justify-center rounded-lg border ${iconClass}`}>{icon}</span>
      </div>
      <div className="mt-2 text-2xl font-semibold tracking-tight text-fg">{value}</div>
      <div className="mt-0.5 truncate text-2xs text-fg-muted">{caption}</div>
    </div>
  );
}

interface RowMenuProps {
  profile: Profile;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}

export function RowMenu({ profile, onEdit, onDuplicate, onDelete }: RowMenuProps) {
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
          className="absolute right-0 top-full z-popover mt-1 w-48 rounded-lg border border-line bg-surface-2 p-1 shadow-popover"
        >
          {item("Mở cấu hình", <SlidersHorizontal className="h-3.5 w-3.5" />, onEdit)}
          {item("Sao chép hồ sơ", <Copy className="h-3.5 w-3.5" />, onDuplicate)}
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
