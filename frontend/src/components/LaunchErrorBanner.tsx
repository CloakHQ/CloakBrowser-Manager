import { AlertTriangle, X } from "lucide-react";
import { api, type LaunchDenial } from "../lib/api";

/**
 * Dismissible top bar shown when a launch is refused for a license reason
 * (out of seats, invalid/expired key). Mirrors UpdateBanner's styling; the
 * upgrade link opens in the host browser via openExternal (window.open is a
 * no-op in the native webview). Dismiss is in-memory only.
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

  return (
    <div className="flex items-center justify-center gap-2 px-4 py-1.5 text-xs bg-red-500/10 border-b border-red-500/30 text-red-300">
      <AlertTriangle className="h-3.5 w-3.5 flex-shrink-0" />
      <span>{error.message}</span>
      {error.upgrade_url && (
        <button
          onClick={openUpgrade}
          className="font-medium underline underline-offset-2 hover:text-red-200"
        >
          Upgrade your plan
        </button>
      )}
      <button
        onClick={onDismiss}
        className="ml-1 p-0.5 text-red-400/70 hover:text-red-200"
        title="Dismiss"
        aria-label="Dismiss launch error"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
