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
      <span className={TIER_CLASS[tier] ?? "badge-neutral"}>
        {TIER_LABEL[tier] ?? tier}
      </span>
      {plan && <span className="text-fg-secondary">gói: {plan === "free" ? "miễn phí" : plan}</span>}

      {status && (
        <>
          <span className="h-3 w-px bg-line" />
          <span className="inline-flex items-center gap-1.5 text-fg-secondary" title="Đang chạy / tổng số hồ sơ">
            <MonitorSmartphone className="h-3 w-3 text-brand" />
            {running} đang chạy / {total} hồ sơ
          </span>
          <span className="h-3 w-px bg-line" />
          <span className="inline-flex items-center gap-1.5 font-mono text-fg-secondary" title="Bản Chromium đang dùng">
            <Cpu className="h-3 w-3 text-info" />
            {status.binary_version}
          </span>
          <span className="h-3 w-px bg-line" />
          <span className="inline-flex items-center gap-1.5" title="Chế độ chạy và trình xem">
            <Server className="h-3 w-3 text-fg-muted" />
            {status.runtime_mode} · {status.viewer_mode} · {status.host_os}
          </span>
        </>
      )}

      {status?.windows_fonts_complete === false && (
        <span
          className="inline-flex items-center gap-1 rounded-full bg-warning/10 border border-warning/25 px-2 py-0.5 text-warning"
          title={`Thiếu font Windows persona: tìm thấy ${status.windows_fonts_present ?? 0}/${status.windows_fonts_required ?? 0}`}
        >
          <AlertTriangle className="h-3 w-3" />
          thiếu font
        </span>
      )}

      <span className="ml-auto truncate text-fg-muted">
        CloakBrowser Manager (Custom) · bản fork không chính thức
      </span>
    </div>
  );
}
