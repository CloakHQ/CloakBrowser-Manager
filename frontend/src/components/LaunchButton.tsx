import { Play, Square, Loader2 } from "lucide-react";
import { useState } from "react";

interface LaunchButtonProps {
  status: "running" | "stopped" | "initializing";
  onLaunch: () => Promise<void>;
  onStop: () => Promise<void>;
}

export function LaunchButton({ status, onLaunch, onStop }: LaunchButtonProps) {
  const [loading, setLoading] = useState(false);

  const handleClick = async () => {
    setLoading(true);
    try {
      if (status === "running") {
        await onStop();
      } else {
        await onLaunch();
      }
    } catch (err) {
      // Launch/license errors are surfaced by the top LaunchErrorBanner (set in
      // App.handleLaunch); nothing to render here.
      console.error("Action failed:", err);
    } finally {
      setLoading(false);
    }
  };

  if (loading || status === "initializing") {
    const label =
      status === "initializing"
        ? "First-time setup..."
        : status === "running"
          ? "Stopping..."
          : "Launching...";
    return (
      <button disabled className="btn-secondary opacity-60 cursor-not-allowed flex items-center gap-1.5">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        <span>{label}</span>
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
    <button onClick={handleClick} className="btn-primary flex items-center gap-1.5">
      <Play className="h-3.5 w-3.5" />
      <span>Launch</span>
    </button>
  );
}
