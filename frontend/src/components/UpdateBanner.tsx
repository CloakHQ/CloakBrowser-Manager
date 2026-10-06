import { ArrowUpCircle, X } from "lucide-react";
import { api, type UpdateInfo } from "../lib/api";

/**
 * Thin dismissible bar shown at the top of the app when a newer Manager
 * release exists on GitHub. Clicking opens the release page in the system
 * browser; the dismiss is in-memory only (resets on relaunch).
 */
export function UpdateBanner({
  info,
  onDismiss,
}: {
  info: UpdateInfo;
  onDismiss: () => void;
}) {
  const openRelease = () => {
    if (!info.release_url) return;
    // Open in the host's default browser. The backend runs locally, so this
    // works from the native webview window (where window.open is a no-op).
    api.openExternal(info.release_url).catch(() => {});
  };

  return (
    <div className="flex items-center justify-center gap-2 px-4 py-1.5 text-xs bg-cyan-500/10 border-b border-cyan-500/30 text-cyan-300">
      <ArrowUpCircle className="h-3.5 w-3.5 flex-shrink-0" />
      <span>
        CloakBrowser Manager{" "}
        <span className="font-mono font-medium">{info.latest}</span> is available.
      </span>
      <button
        onClick={openRelease}
        className="font-medium underline underline-offset-2 hover:text-cyan-200"
      >
        Download
      </button>
      <button
        onClick={onDismiss}
        className="ml-1 p-0.5 text-cyan-400/70 hover:text-cyan-200"
        title="Dismiss"
        aria-label="Dismiss update notification"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
