import { Activity, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type SystemCheck } from "../lib/api";

const GPU_LABELS: Record<SystemCheck["gpu_mode"], string> = {
  swiftshader: "Off (software rendering)",
  nvidia: "NVIDIA",
  igpu: "Integrated (Intel/AMD)",
};

function formatBytes(bytes: number): string {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exp = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** exp).toFixed(exp === 0 ? 0 : 1)} ${units[exp]}`;
}

interface RowProps {
  label: string;
  value: string;
  warn?: boolean;
}

function Row({ label, value, warn }: RowProps) {
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="text-gray-500">{label}</dt>
      <dd className={warn ? "text-amber-400 text-right" : "text-gray-200 text-right"}>{value}</dd>
    </div>
  );
}

export function SystemCheckPanel() {
  const [open, setOpen] = useState(false);
  const [check, setCheck] = useState<SystemCheck | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    api.systemCheck()
      .then((data) => {
        if (!cancelled) setCheck(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load");
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="text-gray-500 hover:text-gray-300 p-1"
        title="Container self-check"
      >
        <Activity className="h-4 w-4" />
      </button>
      {open && (
        <div className="absolute right-0 mt-2 w-72 bg-surface-2 border border-border rounded-lg shadow-lg p-4 z-50">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
              Container Self-Check
            </h3>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-gray-500 hover:text-gray-300"
              aria-label="Close"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>

          {error && <p className="text-red-400 text-xs">{error}</p>}
          {!error && !check && <p className="text-gray-500 text-xs">Loading...</p>}

          {check && (
            <dl className="space-y-2 text-xs">
              <Row label="GPU acceleration" value={GPU_LABELS[check.gpu_mode] ?? check.gpu_mode} />
              <Row label="Binary version" value={check.binary_version} />
              <Row
                label="License"
                value={check.license_configured ? "Configured" : "Not set (free tier)"}
                warn={!check.license_configured}
              />
              <Row label="KasmVNC" value={check.kasmvnc_version} />
              <Row
                label="Disk (/data)"
                value={`${formatBytes(check.disk_free_bytes)} free (${check.disk_percent_used}% used)`}
                warn={check.disk_percent_used >= 90}
              />
            </dl>
          )}
        </div>
      )}
    </div>
  );
}
