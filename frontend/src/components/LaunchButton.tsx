import { Play, Square, Loader2 } from "lucide-react";
import { useState } from "react";
import type { ProfileLifecycle } from "../lib/api";

interface LaunchButtonProps {
  status: ProfileLifecycle;
  onLaunch: () => Promise<void>;
  onStop: () => Promise<void>;
}

export function LaunchButton({ status, onLaunch, onStop }: LaunchButtonProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClick = async () => {
    setLoading(true);
    setError(null);
    try {
      if (status === "running") {
        await onStop();
      } else {
        await onLaunch();
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Action failed";
      setError(msg);
      console.error("Action failed:", err);
    } finally {
      setLoading(false);
    }
  };

  // "starting" = the backend is already bringing this profile up (container
  // restart / auto-launch queue). Launching again would just 409.
  if (loading || status === "starting") {
    return (
      <button disabled className="btn-secondary opacity-60 cursor-not-allowed flex items-center gap-1.5">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        <span>{status === "running" ? "Stopping..." : "Launching..."}</span>
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
