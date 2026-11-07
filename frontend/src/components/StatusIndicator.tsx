import { PROFILE_STATUS_LABEL } from "../lib/labels";

interface StatusIndicatorProps {
  status: "running" | "stopped" | "initializing";
  size?: "sm" | "md";
}

export function StatusIndicator({ status, size = "sm" }: StatusIndicatorProps) {
  const sizeClass = size === "sm" ? "h-2 w-2" : "h-2.5 w-2.5";
  const isRunning = status === "running";
  const isInitializing = status === "initializing";
  const pulse = isRunning || isInitializing;
  const color = isRunning
    ? "bg-success shadow-[0_0_8px_rgba(16,185,129,0.6)]"
    : isInitializing
      ? "bg-warning shadow-[0_0_8px_rgba(245,158,11,0.6)]"
      : "bg-fg-muted/60";
  const pingColor = isRunning ? "bg-success" : "bg-warning";
  const title = isInitializing ? "Thiết lập lần đầu..." : PROFILE_STATUS_LABEL[status];

  return (
    <span className="relative inline-flex flex-shrink-0" title={title}>
      {pulse && (
        <span
          className={`absolute inline-flex ${sizeClass} rounded-full ${pingColor} opacity-75 animate-ping`}
        />
      )}
      <span className={`relative inline-flex ${sizeClass} rounded-full ${color}`} />
    </span>
  );
}
