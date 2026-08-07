import { Cookie } from "lucide-react";
import { useCookieWarmup } from "../hooks/useCookieWarmup";

interface CookieWarmupPanelProps {
  profileId: string;
  isRunning: boolean;
  /** Drops the descriptive paragraph and shrinks the status line, for use in
   *  App.tsx's top bar — see its own docstring for why that's where the
   *  actionable button has to live, not the profile edit form. */
  compact?: boolean;
}

function formatRemaining(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${minutes}m${secs.toString().padStart(2, "0")}s`;
}

function hostnameOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

export function CookieWarmupPanel({ profileId, isRunning, compact = false }: CookieWarmupPanelProps) {
  const { status, start, stop, busy } = useCookieWarmup(profileId);

  if (!isRunning) {
    if (compact) return null; // nothing actionable, and the top bar already hides this by status
    return (
      <p className="text-xs text-gray-500">
        Launch this profile first — cookie warmup drives its live browser session.
      </p>
    );
  }

  const state = status?.state ?? "idle";
  const isActive = state === "running";
  const buttonClass = compact
    ? "text-gray-500 hover:text-gray-300 p-1 flex items-center gap-1 disabled:opacity-50"
    : "btn-secondary text-xs flex items-center gap-1.5";
  const statusClass = compact ? "text-xs text-gray-500" : "text-xs text-gray-400";

  return (
    <div className={compact ? "flex items-center gap-2" : "space-y-2"}>
      {!compact && (
        <p className="text-xs text-gray-500">
          Visits {status?.sites_total ?? 20} common sites over about 10 minutes, so this profile
          does not start out as a completely blank slate of cookies and browsing history.
        </p>
      )}
      <div className={compact ? "flex items-center gap-2" : "flex items-center gap-3 flex-wrap"}>
        {isActive ? (
          <button
            type="button"
            onClick={stop}
            disabled={busy}
            className={buttonClass}
            title={compact ? "Stop cookie warmup" : undefined}
            aria-label={compact ? "Stop cookie warmup" : undefined}
          >
            {compact ? <Cookie className="h-3.5 w-3.5" /> : null}
            {busy ? "Stopping..." : "Stop"}
          </button>
        ) : (
          <button
            type="button"
            onClick={start}
            disabled={busy}
            className={buttonClass}
            title={compact ? "Warm up cookies — visit common sites for ~10 minutes" : undefined}
            aria-label={compact ? "Warm up cookies" : undefined}
          >
            <Cookie className="h-3.5 w-3.5" />
            {!compact && (busy ? "Starting..." : "Warm up cookies")}
          </button>
        )}

        {isActive && status && (
          <span className={statusClass}>
            {status.sites_visited}/{status.sites_total} sites
            {status.remaining_seconds != null && ` · ${formatRemaining(status.remaining_seconds)} remaining`}
            {!compact && status.current_site && ` · visiting ${hostnameOf(status.current_site)}`}
          </span>
        )}
        {state === "done" && (
          <span className={compact ? "text-xs text-green-500" : "text-xs text-green-400"}>
            {compact ? "Warmed up" : `Done — visited ${status?.sites_visited} sites`}
          </span>
        )}
        {state === "cancelled" && !compact && (
          <span className="text-xs text-gray-400">
            Stopped — visited {status?.sites_visited} sites
          </span>
        )}
        {state === "error" && (
          <span className={compact ? "text-xs text-red-500" : "text-xs text-red-400"}>
            {compact ? "Warmup failed" : `Failed: ${status?.error}`}
          </span>
        )}
      </div>
    </div>
  );
}
