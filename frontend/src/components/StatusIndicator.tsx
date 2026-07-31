import type { ProfileLifecycle } from "../lib/api";

interface StatusIndicatorProps {
  status: ProfileLifecycle;
  size?: "sm" | "md";
}

const DOT_CLASS: Record<ProfileLifecycle, string> = {
  running: "bg-emerald-400",
  starting: "bg-yellow-400",
  stopped: "bg-gray-500",
};

export function StatusIndicator({ status, size = "sm" }: StatusIndicatorProps) {
  const sizeClass = size === "sm" ? "h-2 w-2" : "h-2.5 w-2.5";
  const pinging = status === "running" || status === "starting";
  const dot = DOT_CLASS[status] ?? DOT_CLASS.stopped;

  return (
    <span className="relative inline-flex">
      {pinging && (
        <span
          className={`absolute inline-flex ${sizeClass} rounded-full ${dot} opacity-75 animate-ping`}
        />
      )}
      <span className={`relative inline-flex ${sizeClass} rounded-full ${dot}`} />
    </span>
  );
}
