import { AlertTriangle } from "lucide-react";
import type { SystemStatus } from "../lib/api";

const TIER_LABEL: Record<string, string> = {
  pro: "Pro",
  free: "Free key",
  keyless: "Free (keyless)",
};

const TIER_COLOR: Record<string, string> = {
  pro: "text-emerald-400",
  free: "text-sky-400",
  keyless: "text-amber-400",
};

/** Small always-visible badge showing which binary/tier the Manager is running. */
export function SystemStatusBadge({ status }: { status: SystemStatus | null }) {
  if (!status) return null;

  const tier = status.license_tier || "keyless";
  const title =
    tier === "keyless"
      ? "No license key set. Running the free keyless build. Add a key in .env for the latest Pro build."
      : `${TIER_LABEL[tier] ?? tier} license — running ${status.binary_version}`;

  return (
    <span
      className="flex items-center gap-1.5 text-xs text-gray-500"
      title={title}
    >
      <span className={TIER_COLOR[tier] ?? "text-gray-400"}>
        {TIER_LABEL[tier] ?? tier}
      </span>
      <span className="text-gray-600">·</span>
      <span className="font-mono">{status.binary_version}</span>
      {status.windows_fonts_complete === false && (
        <span className="flex items-center gap-1 text-amber-400" title={`Windows persona fonts incomplete: ${status.windows_fonts_present ?? 0}/${status.windows_fonts_required ?? 0} found`}>
          <AlertTriangle className="h-3.5 w-3.5" /> Fonts incomplete
        </span>
      )}
    </span>
  );
}
