import { AlertTriangle, KeyRound, X } from "lucide-react";
import { api, type LaunchDenial } from "../lib/api";

/**
 * Dismissible top bar shown when a launch is refused for a license reason.
 * Two distinct cases, because the right next step differs:
 *   - seat_limit: the key works, but the plan's concurrent sessions are used up.
 *   - license:    the key itself is invalid, expired or missing.
 * The upgrade link opens in the host browser via openExternal (window.open is a
 * no-op in the native webview). Dismiss is in-memory only; nothing retries.
 */
export function LaunchErrorBanner({
  error,
  onDismiss,
}: {
  error: LaunchDenial;
  onDismiss: () => void;
}) {
  const openUpgrade = () => {
    if (!error.upgrade_url) return;
    api.openExternal(error.upgrade_url).catch(() => {});
  };

  const seat = error.reason === "seat_limit";
  const Icon = seat ? AlertTriangle : KeyRound;
  const title = seat ? "Đã đạt giới hạn phiên đồng thời" : "Có vấn đề với key bản quyền";
  const tone = seat
    ? "bg-warning/10 border-warning/30 text-warning"
    : "bg-danger/10 border-danger/30 text-danger";

  return (
    <div className={`flex items-center justify-center gap-2.5 border-b px-4 py-2 text-xs backdrop-blur-sm ${tone}`}>
      <Icon className="h-3.5 w-3.5 flex-shrink-0" />
      <span className="font-semibold">{title}:</span>
      <span className="opacity-95">{error.message}</span>
      {error.upgrade_url && (
        <button
          onClick={openUpgrade}
          className="rounded-md bg-current/15 px-2 py-0.5 font-medium underline underline-offset-2 hover:opacity-80"
        >
          Nâng cấp gói
        </button>
      )}
      <button
        onClick={onDismiss}
        className="ml-1 rounded p-0.5 opacity-70 hover:opacity-100 transition-opacity"
        title="Bỏ qua"
        aria-label="Bỏ qua lỗi khởi chạy"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
