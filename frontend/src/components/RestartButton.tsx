import { RotateCw, Loader2 } from "lucide-react";

interface RestartButtonProps {
  busy: boolean;
  onClick: () => void;
}

/** Purely presentational — App.tsx owns the stop-then-launch sequencing and
 *  the single `restarting` flag that also hides LaunchButton for the
 *  duration, so this never has to reconcile its own busy state against the
 *  3s-stale polled profile status the way LaunchButton itself does. */
export function RestartButton({ busy, onClick }: RestartButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      className="btn-secondary flex items-center gap-1.5"
      title="Stop then relaunch this profile"
    >
      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5" />}
      <span>{busy ? "Restarting..." : "Restart"}</span>
    </button>
  );
}
