import { Play, Square, Loader2 } from "lucide-react";
import { useState } from "react";
import type { ProfileLifecycle } from "../lib/api";

interface LaunchButtonProps {
  status: ProfileLifecycle;
  onLaunch: () => Promise<void>;
  onStop: () => Promise<void>;
}

/**
 * Lifecycle states that offer no action at all, and the label to show instead.
 * This is a Record over the full union rather than an if-chain precisely so a
 * new ProfileLifecycle value cannot fall through to the enabled "Launch" at the
 * bottom of the component — which for a busy profile is a guaranteed 409.
 *   starting = the backend is already bringing this profile up (container
 *              restart / auto-launch queue); launching again just 409s.
 *   stopping = teardown in flight; launch AND stop are both refused with 409
 *              until it completes, so a Stop button here is a guaranteed error.
 */
const BUSY_LABEL: Record<ProfileLifecycle, string | null> = {
  running: null,
  starting: "Launching...",
  stopping: "Shutting down...",
  stopped: null,
};

export function LaunchButton({ status, onLaunch, onStop }: LaunchButtonProps) {
  /** Which action WE invoked, if any — never re-derived from the polled status. */
  const [pending, setPending] = useState<"launch" | "stop" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleClick = async () => {
    const action = status === "running" ? "stop" : "launch";
    setPending(action);
    setError(null);
    try {
      if (action === "stop") {
        await onStop();
      } else {
        await onLaunch();
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Action failed";
      setError(msg);
      console.error("Action failed:", err);
    } finally {
      setPending(null);
    }
  };

  // Our own in-flight action outranks the polled status, which is up to 3s
  // stale. It must be remembered rather than re-derived: the poll flips
  // running -> stopping while the stop POST is still on the wire, so deriving
  // the label from `status` labelled a teardown "Launching..." — the same
  // dot/button contradiction the `stopping` state exists to remove.
  const busyLabel = pending
    ? pending === "stop" ? "Stopping..." : "Launching..."
    : BUSY_LABEL[status];

  if (busyLabel !== null) {
    return (
      <button disabled className="btn-secondary opacity-60 cursor-not-allowed flex items-center gap-1.5">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        <span>{busyLabel}</span>
      </button>
    );
  }

  if (status === "running") {
    return (
      <button onClick={handleClick} className="btn-danger flex items-center gap-1.5">
        <Square className="h-3.5 w-3.5" />
        <span>Stop</span>
      </button>
    );
  }

  return (
    <div>
      <button onClick={handleClick} className="btn-primary flex items-center gap-1.5">
        <Play className="h-3.5 w-3.5" />
        <span>Launch</span>
      </button>
      {error && <p className="text-red-400 text-xs mt-1">{error}</p>}
    </div>
  );
}
