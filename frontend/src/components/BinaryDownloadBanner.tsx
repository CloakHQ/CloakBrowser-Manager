import type { BinaryDownloadStatus } from "../hooks/useBinaryDownload";

// Exhaustive-ish: anything not listed (including null, mid-download before
// the first "progress" log line lands) reads as "Preparing" rather than a
// blank label.
const STATE_LABEL: Record<string, string> = {
  downloading: "Downloading",
  extracting: "Extracting",
};

export function BinaryDownloadBanner({ status }: { status: BinaryDownloadStatus }) {
  if (!status.downloading) return null;

  const label = (status.state && STATE_LABEL[status.state]) ?? "Preparing";
  const pct = status.percent ?? 0;

  return (
    <div className="px-4 py-2 bg-blue-600/15 border-b border-blue-600/30 text-blue-300 text-sm flex items-center gap-3">
      <span className="whitespace-nowrap">
        {label} the CloakBrowser Chromium binary ({pct}%) — profiles can't launch until this finishes.
      </span>
      <div className="flex-1 h-1.5 max-w-xs bg-blue-900/40 rounded overflow-hidden">
        <div className="h-full bg-blue-400 transition-[width]" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
