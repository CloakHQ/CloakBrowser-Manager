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
    <div className="flex items-center justify-center gap-2.5 px-4 py-2 text-xs bg-info/10 border-b border-info/25 text-info backdrop-blur-sm">
      <ArrowUpCircle className="h-3.5 w-3.5 flex-shrink-0" />
      <span>
        CloakBrowser Manager (Custom){" "}
        <span className="font-mono font-semibold">{info.latest}</span> đã có bản mới.
      </span>
      <button
        onClick={openRelease}
        className="rounded-md bg-info/15 border border-info/30 px-2 py-0.5 font-medium hover:bg-info/25 transition-colors"
      >
        Tải xuống
      </button>
      <button
        onClick={onDismiss}
        className="ml-1 rounded p-0.5 opacity-70 hover:opacity-100 transition-opacity"
        title="Bỏ qua"
        aria-label="Bỏ qua thông báo cập nhật"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
