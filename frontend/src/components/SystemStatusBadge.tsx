import { AlertTriangle } from "lucide-react";
import type { SystemStatus } from "../lib/api";

/** Small always-visible badge showing the persistent browser cache state. */
export function SystemStatusBadge({ status }: { status: SystemStatus | null }) {
  if (!status) return null;

  const count = status.installed_binary_count;
  return (
    <span
      className="flex items-center gap-1.5 text-xs text-gray-500"
      title={`Browser cache: ${status.binary_cache_dir}`}
    >
      <span className={count > 0 ? "text-emerald-400" : "text-gray-500"}>
        {count} browser {count === 1 ? "version" : "versions"}
      </span>
      {status.windows_fonts_complete === false && (
        <span className="flex items-center gap-1 text-amber-400" title={`Windows persona fonts incomplete: ${status.windows_fonts_present ?? 0}/${status.windows_fonts_required ?? 0} found`}>
          <AlertTriangle className="h-3.5 w-3.5" /> Fonts incomplete
        </span>
      )}
    </span>
  );
}
