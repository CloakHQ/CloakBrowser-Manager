import { useEffect, useState } from "react";
import { api, type SystemStatus } from "../lib/api";

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
export function SystemStatusBadge() {
  const [status, setStatus] = useState<SystemStatus | null>(null);

  // Tier/version is fixed per instance — fetch once on mount.
  useEffect(() => {
    api.getStatus().then(setStatus).catch(() => setStatus(null));
  }, []);

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
    </span>
  );
}
