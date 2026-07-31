import { useEffect, useRef, useState } from "react";
import {
  ClipboardCopy,
  Code2,
  Maximize2,
  Minimize2,
  MonitorOff,
  RefreshCw,
  WifiOff,
} from "lucide-react";
import { useViewerSession, type ViewerState } from "../hooks/useViewerSession";

interface ProfileViewerProps {
  profileId: string;
  cdpUrl: string | null;
  clipboardSync: boolean;
  /** Terminal only: the browser session really ended (not a transient drop). */
  onSessionEnded: () => void;
}

const STATUS_META: Record<ViewerState, { dot: string; label: string }> = {
  idle: { dot: "bg-yellow-400 animate-pulse", label: "Connecting..." },
  connecting: { dot: "bg-yellow-400 animate-pulse", label: "Connecting..." },
  connected: { dot: "bg-emerald-400", label: "Connected" },
  reconnecting: { dot: "bg-yellow-400 animate-pulse", label: "Reconnecting..." },
  "session-ended": { dot: "bg-red-400", label: "Session ended" },
  fatal: { dot: "bg-red-400", label: "Connection failed" },
};

export function ProfileViewer({ profileId, cdpUrl, clipboardSync: initialClipboardSync, onSessionEnded }: ProfileViewerProps) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [clipboardSync, setClipboardSync] = useState(initialClipboardSync);
  const [cdpCopied, setCdpCopied] = useState(false);

  const session = useViewerSession({ profileId, clipboardSync });

  // 1s ticker so the "next retry in Ns" countdown stays fresh
  const [, setTick] = useState(0);
  useEffect(() => {
    if (session.state !== "reconnecting" || session.nextRetryAt === null) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [session.state, session.nextRetryAt]);

  const retryInSeconds =
    session.nextRetryAt !== null
      ? Math.max(0, Math.ceil((session.nextRetryAt - Date.now()) / 1000))
      : null;

  const toggleFullscreen = () => {
    if (!wrapperRef.current) return;
    if (!document.fullscreenElement) {
      wrapperRef.current.requestFullscreen();
      setFullscreen(true);
    } else {
      document.exitFullscreen();
      setFullscreen(false);
    }
  };

  useEffect(() => {
    const handleFsChange = () => {
      setFullscreen(!!document.fullscreenElement);
    };
    document.addEventListener("fullscreenchange", handleFsChange);
    return () => document.removeEventListener("fullscreenchange", handleFsChange);
  }, []);

  // The iframe consumes wheel events over the canvas itself; this guards the
  // edges/toolbar so the page behind doesn't scroll.
  useEffect(() => {
    const surface = surfaceRef.current;
    if (!surface) return;

    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
    };

    surface.addEventListener("wheel", handleWheel, { passive: false });
    return () => surface.removeEventListener("wheel", handleWheel);
  }, []);

  const meta = STATUS_META[session.state];

  return (
    <div ref={wrapperRef} className="relative h-full flex flex-col bg-surface-0">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-surface-1 border-b border-border">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${meta.dot}`} />
          <span className="text-xs text-gray-400">
            {meta.label}
            {session.state === "reconnecting" && session.attempt > 0 && (
              <span className="text-gray-500"> (attempt {session.attempt})</span>
            )}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {cdpUrl && (
            <button
              onClick={() => {
                const base = `${window.location.protocol}//${window.location.host}${cdpUrl}`;
                navigator.clipboard?.writeText(base).then(() => {
                  setCdpCopied(true);
                  setTimeout(() => setCdpCopied(false), 2000);
                }).catch((err) => console.warn("[cdp] copy failed:", err));
              }}
              className={`p-1 ${cdpCopied ? "text-emerald-400" : "text-gray-500 hover:text-gray-300"}`}
              title={cdpCopied ? "Copied!" : "Copy CDP endpoint URL"}
            >
              <Code2 className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            onClick={() => setClipboardSync(!clipboardSync)}
            className={`p-1 ${clipboardSync ? "text-accent" : "text-gray-500 hover:text-gray-300"}`}
            title={
              (clipboardSync ? "Disable clipboard sync" : "Enable clipboard sync") +
              " (applies on next connect)"
            }
          >
            <ClipboardCopy className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={toggleFullscreen}
            className="text-gray-500 hover:text-gray-300 p-1"
            title={fullscreen ? "Exit fullscreen" : "Fullscreen"}
          >
            {fullscreen ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
          </button>
        </div>
      </div>

      {/* Display surface: native KasmVNC client in an iframe */}
      <div
        ref={surfaceRef}
        className="relative flex-1 bg-black overflow-hidden"
        style={{ minHeight: 0 }}
      >
        {session.iframeSrc && (
          <iframe
            ref={session.iframeRef}
            src={session.iframeSrc}
            onLoad={session.handleIframeLoad}
            className="h-full w-full border-0"
            allow="clipboard-read; clipboard-write; fullscreen"
            title="Browser session"
          />
        )}

        {/* Reconnecting: dim the last frame, keep the iframe mounted */}
        {session.state === "reconnecting" && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-black/60 px-4 text-center">
            {session.offline ? (
              <>
                <WifiOff className="h-6 w-6 text-yellow-400" />
                <p className="text-sm text-gray-200">
                  Your network appears offline — will reconnect when it returns
                </p>
              </>
            ) : (
              <>
                <RefreshCw className="h-6 w-6 text-yellow-400 animate-spin" />
                <p className="text-sm text-gray-200">Connection lost — reconnecting</p>
                <p className="text-xs text-gray-500">
                  Attempt {session.attempt}
                  {retryInSeconds !== null && ` · next retry in ${retryInSeconds}s`}
                </p>
                {session.degraded && (
                  <div className="mt-2 flex flex-col items-center gap-2">
                    <p className="text-xs text-gray-400">Still trying…</p>
                    <button onClick={session.reconnectNow} className="btn-secondary">
                      Reconnect now
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* Terminal states */}
        {(session.state === "session-ended" || session.state === "fatal") && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-surface-0/95 px-4 text-center">
            <MonitorOff className="h-8 w-8 text-gray-500" />
            <p className="text-sm text-gray-200">
              {session.endReason ?? "Browser session ended"}
            </p>
            <div className="flex items-center gap-2">
              <button onClick={session.reconnectNow} className="btn-secondary">
                Try again
              </button>
              <button onClick={onSessionEnded} className="btn-primary">
                Back to profile
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
