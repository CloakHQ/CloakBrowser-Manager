import { AlertTriangle } from "lucide-react";
import type { SystemStatus } from "../lib/api";

const TIER_LABEL: Record<string, string> = {
  pro: "Pro",
  free: "Free key",
  none: "No key",
};

const TIER_COLOR: Record<string, string> = {
  pro: "text-emerald-400",
  free: "text-sky-400",
  none: "text-amber-400",
};

/** Small always-visible badge showing which binary/tier the Manager is running. */
export function SystemStatusBadge({ status }: { status: SystemStatus | null }) {
  if (!status) return null;

  const tier = status.license_tier || "none";
  const title =
    tier === "none"
      ? "No license key set. Open Settings to add one (a free key works) before launching a profile."
      : `${TIER_LABEL[tier] ?? tier} license — running ${status.binary_version}`;

  return (
    <span
      className="flex items-center gap-1.5 text-xs text-gray-500"
      title={title}
    >
      <span className={TIER_COLOR[tier] ?? "text-gray-400"}>
        {TIER_LABEL[tier] ?? tier}
      </span>
      {status.binary_version && (
        <>
          <span className="text-gray-600">·</span>
          <span className="font-mono">{status.binary_version}</span>
        </>
      )}
      {status.windows_fonts_complete === false && (
        <span className="flex items-center gap-1 text-amber-400" title={`Windows persona fonts incomplete: ${status.windows_fonts_present ?? 0}/${status.windows_fonts_required ?? 0} found`}>
          <AlertTriangle className="h-3.5 w-3.5" /> Fonts incomplete
        </span>
      )}
    </span>
  );
}
