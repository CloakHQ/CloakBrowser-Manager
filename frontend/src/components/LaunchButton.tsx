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
        ? "Thiết lập lần đầu..."
        : status === "running"
          ? "Đang dừng..."
          : "Đang khởi chạy...";
    return (
      <button disabled className="btn-secondary h-8 text-xs opacity-70 cursor-not-allowed flex items-center gap-1.5">
        <Loader2 className="h-3.5 w-3.5 animate-spin text-brand" />
        <span>{label}</span>
      </button>
    );
  }

  if (status === "running") {
    return (
      <button onClick={handleClick} className="btn-danger h-8 text-xs flex items-center gap-1.5">
        <Square className="h-3.5 w-3.5 fill-current" />
        <span>Dừng</span>
      </button>
    );
  }

  return (
    <button onClick={handleClick} className="btn-primary h-8 text-xs flex items-center gap-1.5">
      <Play className="h-3.5 w-3.5 fill-current" />
      <span>Khởi chạy</span>
    </button>
  );
}
