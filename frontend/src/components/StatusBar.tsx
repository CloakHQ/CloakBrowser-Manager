import { AlertTriangle, Cpu, MonitorSmartphone, Server } from "lucide-react";
import type { SystemStatus } from "../lib/api";
import { TIER_LABEL } from "../lib/labels";

const TIER_CLASS: Record<string, string> = {
  pro: "badge-success",
  free: "badge-info",
  none: "badge-warning",
};

/**
 * Always-visible bottom bar. Its job is to make the licensing model legible at a
 * glance: one key for the whole app, and how many sessions that key is using.
 * Enforcement stays in the binary/backend — this only reports.
 */
export function StatusBar({ status }: { status: SystemStatus | null }) {
  const tier = status?.license_tier || "none";
  const plan = status?.license_plan ?? null;
  const running = status?.running_count ?? 0;
  const total = status?.profiles_total ?? 0;

  return (
    <div className="statusbar" role="status" aria-live="polite">
      <div className="flex items-center gap-2">
        <span className={TIER_CLASS[tier] ?? "badge-neutral"}>
          <span className="h-1.5 w-1.5 rounded-full bg-current opacity-80" />
          {TIER_LABEL[tier] ?? tier}
        </span>
        {plan && (
          <span className="hidden whitespace-nowrap text-fg-muted sm:inline text-[11px]">
            gói: <span className="text-fg-secondary font-medium">{plan === "free" ? "miễn phí" : plan}</span>
          </span>
        )}
      </div>

      {status && (
        <>
          <span className="hidden h-3 w-px bg-line/80 sm:block" />
          <span
            className="inline-flex items-center gap-1.5 whitespace-nowrap text-fg-secondary text-[11px]"
            title="Đang chạy / tổng số hồ sơ"
          >
            <MonitorSmartphone className="h-3.5 w-3.5 text-brand-fg" />
            <span className="font-medium text-fg">{running}</span>
            <span className="text-fg-muted">/{total} đang chạy</span>
          </span>
          <span className="hidden h-3 w-px bg-line/80 md:block" />
          <span
            className="hidden items-center gap-1.5 whitespace-nowrap font-mono text-fg-secondary text-[11px] md:inline-flex"
            title="Bản Chromium đang dùng"
          >
            <Cpu className="h-3.5 w-3.5 text-info" />
            {status.binary_version}
          </span>
          <span className="hidden h-3 w-px bg-line/80 lg:block" />
          <span
            className="hidden items-center gap-1.5 whitespace-nowrap text-fg-muted text-[11px] lg:inline-flex"
            title="Chế độ chạy và trình xem"
          >
            <Server className="h-3.5 w-3.5 text-fg-muted" />
            <span className="font-medium text-fg-secondary">{status.runtime_mode}</span>
            <span>·</span>
            <span>{status.viewer_mode}</span>
            <span>·</span>
            <span>{status.host_os}</span>
          </span>
        </>
      )}

      {status?.windows_fonts_complete === false && (
        <span
          className="inline-flex items-center gap-1 rounded-full bg-warning/10 border border-warning/25 px-2 py-0.5 text-warning text-[10px] font-medium"
          title={`Thiếu font Windows persona: tìm thấy ${status.windows_fonts_present ?? 0}/${status.windows_fonts_required ?? 0}`}
        >
          <AlertTriangle className="h-3 w-3" />
          thiếu font
        </span>
      )}

      <span className="ml-auto hidden truncate text-fg-muted/80 text-[10px] xl:inline font-mono">
        CloakBrowser Manager
      </span>
    </div>
  );
}
