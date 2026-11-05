import { useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, ClipboardCopy, Loader2, Maximize2, Minimize2, X } from "lucide-react";
import { api } from "../lib/api";
import { CdpEndpointButton } from "./CdpEndpointButton";
import { ProfileFilesButton, useProfileFiles } from "./ProfileFiles";

interface ProfileViewerProps {
  profileId: string;
  cdpUrl: string | null;
  clipboardSync: boolean;
  profileName: string;
  onClipboardSyncChange: (enabled: boolean) => Promise<void>;
  onDisconnect: () => void;
}

// X11 keysym for V key (Ctrl is already held in VNC by the time we intercept)
const XK_v = 0x0076;

export function ProfileViewer({
  profileId,
  cdpUrl,
  clipboardSync: initialClipboardSync,
  profileName,
  onClipboardSyncChange,
  onDisconnect,
}: ProfileViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  // The canvas plus the drop overlay and transfer feedback; fullscreening the bare canvas
  // would hide every sign that a file is being uploaded.
  const stageRef = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<any>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [clipboardSync, setClipboardSync] = useState(initialClipboardSync);
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const files = useProfileFiles(profileId);

  // A drop onto the canvas is caught HERE, in the Manager's own page — the VNC protocol
  // carries no files. The bytes go up over HTTP and land in this profile's file store.
  const hasFiles = (e: React.DragEvent) => e.dataTransfer?.types?.includes("Files");
  const onDragEnter = (e: React.DragEvent) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth.current += 1;
    setDragging(true);
  };
  const onDragOver = (e: React.DragEvent) => {
    if (hasFiles(e)) e.preventDefault();
  };
  const onDragLeave = (e: React.DragEvent) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragging(false);
  };
  const onDrop = async (e: React.DragEvent) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    // Every dropped file, not just the first: silently keeping one and discarding the rest
    // looks identical to success.
    const dropped = Array.from(e.dataTransfer.files ?? []);
    if (dropped.length) await files.uploadFiles(dropped);
  };

  useEffect(() => setClipboardSync(initialClipboardSync), [initialClipboardSync]);

  useEffect(() => {
    let rfb: any = null;
    let cancelled = false;

    async function connect() {
      try {
        // Import noVNC dynamically
        const { default: RFB } = await import("@novnc/novnc/core/rfb.js");

        if (cancelled) return;

        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/api/profiles/${profileId}/vnc`;

        rfb = new RFB(containerRef.current!, wsUrl, {
          wsProtocols: ["binary"],
        });
        rfbRef.current = rfb;

        rfb.scaleViewport = true;
        rfb.resizeSession = false;
        rfb.showDotCursor = true;

        rfb.addEventListener("connect", () => {
          if (!cancelled) setConnected(true);
        });

        rfb.addEventListener("disconnect", () => {
          if (!cancelled) {
            setConnected(false);
            onDisconnect();
          }
        });

        rfb.addEventListener("securityfailure", (e: any) => {
          setError(`Lỗi bảo mật: ${e.detail.reason}`);
        });
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Kết nối thất bại");
        }
      }
    }

    connect();

    return () => {
      cancelled = true;
      if (rfb) {
        try {
          rfb.disconnect();
        } catch (err) {
          console.debug("[vnc] disconnect cleanup failed:", err);
        }
      }
      rfbRef.current = null;
    };
  }, [profileId, onDisconnect]);

  // Host→VNC: intercept Ctrl+V/Cmd+V at keydown (capture phase)
  // Must fire BEFORE noVNC's canvas listener to prevent the race condition
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !clipboardSync || !connected) return;

    const handleKeyDown = async (e: KeyboardEvent) => {
      console.log("[clipboard] keydown:", e.key, "ctrl:", e.ctrlKey, "meta:", e.metaKey, "clipboardSync:", true);

      const isPaste =
        e.key === "v" && (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey;
      if (!isPaste) return;

      console.log("[clipboard] intercepted Ctrl+V");

      // Block noVNC from sending the keystroke before clipboard is updated
      e.stopPropagation();
      e.preventDefault();

      const rfb = rfbRef.current;
      if (!rfb) {
        console.log("[clipboard] no rfb ref, aborting");
        return;
      }

      try {
        const text = await navigator.clipboard.readText();
        console.log("[clipboard] host clipboard text:", text?.substring(0, 50), "len:", text?.length);
        if (text) {
          console.log("[clipboard] calling setClipboard API...");
          await api.setClipboard(profileId, text);
          console.log("[clipboard] setClipboard API success");
        }
      } catch (err) {
        console.warn("[clipboard] error:", err);
        setClipboardSync(false);
        return;
      }

      // Send full Ctrl+V sequence to VNC. We can't rely on Ctrl still being
      // held because the user may have released it during the async API call.
      console.log("[clipboard] sending Ctrl+V to VNC");
      rfb.sendKey(0xffe3, "ControlLeft", true);   // Ctrl press
      rfb.sendKey(XK_v, "KeyV", true);             // V press
      rfb.sendKey(XK_v, "KeyV", false);            // V release
      rfb.sendKey(0xffe3, "ControlLeft", false);   // Ctrl release
    };

    // capture: true ensures we fire before noVNC's canvas listener
    container.addEventListener("keydown", handleKeyDown, true);
    return () => container.removeEventListener("keydown", handleKeyDown, true);
  }, [profileId, clipboardSync, connected]);

  // VNC→Host: listen for noVNC "clipboard" event (fired when proxy converts
  // KasmVNC BinaryClipboard type 180 → standard ServerCutText type 3)
  useEffect(() => {
    const rfb = rfbRef.current;
    console.log("[clipboard] VNC→Host effect: rfb=", !!rfb, "sync=", clipboardSync, "connected=", connected);
    if (!rfb || !clipboardSync || !connected) return;

    const handleClipboard = (e: any) => {
      const text = e.detail?.text;
      console.log("[clipboard] VNC→Host event fired, text:", text?.substring(0, 50), "len:", text?.length);
      if (text) {
        navigator.clipboard.writeText(text).then(() => {
          console.log("[clipboard] writeText success");
        }).catch((err) => {
          console.warn("[clipboard] writeText failed:", err);
        });
      }
    };

    console.log("[clipboard] registering clipboard event listener on rfb");
    rfb.addEventListener("clipboard", handleClipboard);
    return () => {
      console.log("[clipboard] removing clipboard event listener");
      rfb.removeEventListener("clipboard", handleClipboard);
    };
  }, [clipboardSync, connected]);

  // VNC→Host polling: Chrome doesn't write to X11 clipboard under KasmVNC,
  // so type 180 events won't fire for Chrome copies. Poll via Playwright CDP.
  useEffect(() => {
    if (!clipboardSync || !connected) return;

    let cancelled = false;
    let lastText = "";

    const poll = async () => {
      if (cancelled) return;
      try {
        const { text } = await api.getClipboard(profileId);
        if (text && text !== lastText) {
          lastText = text;
          console.log("[clipboard] poll: new VNC clipboard:", text.substring(0, 50), "len:", text.length);
          await navigator.clipboard.writeText(text).catch((err) =>
            console.warn("[clipboard] poll writeText failed:", err)
          );
        }
      } catch (err) {
        console.warn("[clipboard] poll error, stopping:", err);
        cancelled = true;
        return;
      }
      if (!cancelled) {
        setTimeout(poll, 2000);
      }
    };

    // Start polling after a short delay
    const timer = setTimeout(poll, 2000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [profileId, clipboardSync, connected]);

  const toggleClipboardSync = async () => {
    const previous = clipboardSync;
    const next = !previous;
    setClipboardSync(next);
    try {
      await onClipboardSyncChange(next);
    } catch (err) {
      console.warn("[clipboard] failed to persist preference:", err);
      setClipboardSync(previous);
    }
  };

  const toggleFullscreen = () => {
    if (!stageRef.current) return;
    if (!document.fullscreenElement) {
      stageRef.current.requestFullscreen();
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

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
    };

    container.addEventListener("wheel", handleWheel, { passive: false });
    return () => container.removeEventListener("wheel", handleWheel);
  }, []);

  if (error) {
    return (
      <div className="flex items-center justify-center h-full p-6">
        <div className="card max-w-sm w-full p-6 text-center">
          <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-xl bg-danger/10 border border-danger/25 text-danger">
            <AlertCircle className="h-5 w-5" />
          </div>
          <p className="text-fg font-semibold text-sm mb-1">Kết nối thất bại</p>
          <p className="text-fg-muted text-xs">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-full flex flex-col">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-4 py-2 bg-surface-1/95 border-b border-line">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${connected ? "bg-success shadow-[0_0_8px_rgba(16,185,129,0.6)]" : "bg-warning animate-pulse"}`} />
          <span className="text-xs font-medium text-fg-secondary">
            {connected ? "Đã kết nối" : "Đang kết nối..."}
          </span>
        </div>
        <div className="flex items-center gap-1 rounded-lg border border-line bg-surface-0/60 p-0.5">
          <CdpEndpointButton cdpUrl={cdpUrl} />
          <ProfileFilesButton
            profileId={profileId}
            profileName={profileName}
            files={files.files}
            uploading={files.uploading}
            error={files.error}
            onUpload={files.uploadFiles}
            onRemove={files.remove}
            onRefresh={files.refresh}
            onClearError={files.clearError}
          />
          <button
            onClick={toggleClipboardSync}
            className={`inline-flex items-center justify-center rounded-md p-1.5 transition-colors ${
              clipboardSync
                ? "bg-brand-subtle text-brand"
                : "text-fg-muted hover:bg-surface-3 hover:text-fg"
            }`}
            title={clipboardSync ? "Tắt đồng bộ clipboard" : "Bật đồng bộ clipboard"}
          >
            <ClipboardCopy className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={toggleFullscreen}
            className="inline-flex items-center justify-center rounded-md p-1.5 text-fg-muted hover:bg-surface-3 hover:text-fg transition-colors"
            title={fullscreen ? "Thoát toàn màn hình" : "Toàn màn hình"}
          >
            {fullscreen ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
          </button>
        </div>
      </div>
      {/* VNC canvas container, doubling as a drop target for file uploads */}
      <div
        ref={stageRef}
        className="relative flex-1 bg-black"
        style={{ minHeight: 0 }}
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        <div ref={containerRef} className="absolute inset-0 bg-black overflow-hidden" />
        {/* Transfer feedback lives here, not in the panel: a drop must not look like it
            reached the web page just because the panel happens to be closed. */}
        {files.transfers.length > 0 && (
          <div className="absolute bottom-3 left-3 z-20 w-80 space-y-1.5">
            {files.transfers.map((transfer) => (
              <div
                key={transfer.id}
                role={transfer.state === "failed" ? "alert" : "status"}
                className="flex items-start gap-2 rounded-md border border-line bg-surface-2/95 px-3 py-2 shadow-lg"
              >
                {transfer.state === "uploading" ? (
                  <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin text-brand" />
                ) : transfer.state === "done" ? (
                  <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-success" />
                ) : (
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm text-fg" title={transfer.name}>
                    {transfer.name}
                  </div>
                  <div className="text-xs text-fg-secondary">
                    {transfer.state === "uploading"
                      ? "Đang tải lên hồ sơ này..."
                      : transfer.state === "done"
                        ? `Đã vào tệp của hồ sơ — chọn từ Uploads — ${profileName} trong hộp thoại tệp của trang`
                        : transfer.error}
                  </div>
                  {transfer.state === "failed" && (
                    <button
                      type="button"
                      onClick={() => void files.retryTransfer(transfer.id)}
                      className="mt-1 text-xs text-brand hover:underline"
                    >
                      Thử lại
                    </button>
                  )}
                </div>
                {transfer.state !== "uploading" && (
                  <button
                    type="button"
                    onClick={() => files.dismissTransfer(transfer.id)}
                    className="p-0.5 text-fg-muted hover:text-fg"
                    aria-label={`Bỏ qua ${transfer.name}`}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {dragging && (
          <div className="pointer-events-none absolute inset-0 z-10 m-2 flex items-center justify-center rounded-md border-2 border-dashed border-brand bg-black/70">
            <div className="text-center">
              <p className="text-sm font-medium text-fg">Thả để tải lên hồ sơ này</p>
              <p className="mt-1 text-xs text-fg-secondary">
                Sau đó chọn từ Uploads — {profileName} trong hộp thoại tệp của trang
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
