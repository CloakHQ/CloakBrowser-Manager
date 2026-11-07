import { AlertTriangle } from "lucide-react";
import type { SystemStatus } from "../lib/api";
import { TIER_LABEL } from "../lib/labels";

const TIER_CLASS: Record<string, string> = {
  pro: "badge-success",
  free: "badge-info",
  none: "badge-warning",
};

/**
 * Compact pill in the top bar: which tier/plan the app is running and which
 * Chromium build it resolved. The full detail lives in the StatusBar.
 */
export function SystemStatusBadge({ status }: { status: SystemStatus | null }) {
  if (!status) return null;

  const tier = status.license_tier || "none";
  const plan = status.license_plan ?? null;
  const title =
    tier === "none"
      ? "Chưa đặt key bản quyền. Mở Cài đặt để thêm key trước khi khởi chạy hồ sơ."
      : `Giấy phép ${TIER_LABEL[tier] ?? tier}${plan ? ` (${plan === "free" ? "miễn phí" : plan})` : ""} — đang chạy ${status.binary_version}`;

  return (
    <span
      className="inline-flex items-center gap-2 rounded-xl border border-line/80 bg-surface-2/70 px-2.5 py-1 text-xs shadow-sm transition-all"
      title={title}
    >
      <span className={TIER_CLASS[tier] ?? "badge-neutral"}>
        <span className="h-1.5 w-1.5 rounded-full bg-current opacity-80" />
        {TIER_LABEL[tier] ?? tier}
      </span>
      <span className="font-mono text-[11px] text-fg-secondary">{status.binary_version}</span>
      {status.windows_fonts_complete === false && (
        <span
          className="flex items-center gap-1 rounded-md bg-warning/10 px-1.5 py-0.5 text-warning text-[10px] font-semibold"
          title={`Thiếu font Windows persona: tìm thấy ${status.windows_fonts_present ?? 0}/${status.windows_fonts_required ?? 0}`}
        >
          <AlertTriangle className="h-3 w-3" />
          Thiếu font
        </span>
      )}
    </span>
  );
}
