import { useState, useCallback, useEffect, useRef } from "react";
import { ChevronRight, Eye, LayoutGrid, Loader2, Lock, PanelLeftClose, PanelLeft, Power, Settings, Shield, SlidersHorizontal, WifiOff, X } from "lucide-react";
import { useProfiles } from "./hooks/useProfiles";
import { api, ApiError, setOnUnauthorized, type ProfileCreateData, type SystemStatus, type UpdateInfo, type LaunchDenial } from "./lib/api";
import { ProfileList } from "./components/ProfileList";
import { ProfileTable } from "./components/ProfileTable";
import { StatusBar } from "./components/StatusBar";
import { ProfileForm } from "./components/ProfileForm";
import { ProfileViewer } from "./components/ProfileViewer";
import { NativeWindowStatus } from "./components/NativeWindowStatus";
import { LaunchButton } from "./components/LaunchButton";
import { StatusIndicator } from "./components/StatusIndicator";
import { SystemStatusBadge } from "./components/SystemStatusBadge";
import { UpdateBanner } from "./components/UpdateBanner";
import { LaunchErrorBanner } from "./components/LaunchErrorBanner";
import { SettingsPanel } from "./components/SettingsPanel";
import { LoginPage } from "./components/LoginPage";
import { useConfirm } from "./components/ui/confirm";
import { useToast } from "./components/ui/toast";

type AuthState = "checking" | "required" | "ok" | "error";
type View = "empty" | "create" | "edit" | "view";

export default function App() {
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [authRequired, setAuthRequired] = useState(false);

  useEffect(() => {
    setOnUnauthorized(() => setAuthState("required"));

    api.authStatus()
      .then(({ auth_required, authenticated }) => {
        setAuthRequired(auth_required);
        if (!auth_required || authenticated) {
          setAuthState("ok");
        } else {
          setAuthState("required");
        }
      })
      .catch((err) => {
        console.warn("[auth] status check failed:", err);
        setAuthState("error");
      });

    return () => setOnUnauthorized(null);
  }, []);

  if (authState === "checking") {
    return (
      <div className="h-screen flex items-center justify-center bg-surface-0 px-4">
        <div className="flex flex-col items-center gap-3">
          <div className="relative flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-brand/20 to-brand-deep/10 border border-brand/30 shadow-glow">
            <Shield className="h-6 w-6 text-brand" />
          </div>
          <div className="flex items-center gap-2 text-fg-secondary text-sm font-medium">
            <Loader2 className="h-4 w-4 animate-spin text-brand" />
            <span>Đang tải...</span>
          </div>
        </div>
      </div>
    );
  }

  if (authState === "error") {
    return (
      <div className="h-screen flex items-center justify-center bg-surface-0 px-4">
        <div className="card max-w-sm w-full p-6 text-center">
          <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-danger/10 border border-danger/25 text-danger">
            <WifiOff className="h-5 w-5" />
          </div>
          <p className="text-fg font-semibold text-base mb-1">Không thể kết nối tới máy chủ</p>
          <p className="text-fg-muted text-xs mb-4">
            Kiểm tra lại tiến trình CloakBrowser Manager và thử kết nối lại.
          </p>
          <button
            onClick={() => {
              setAuthState("checking");
              api.authStatus()
                .then(({ auth_required, authenticated }) => {
                  setAuthRequired(auth_required);
                  setAuthState(!auth_required || authenticated ? "ok" : "required");
                })
                .catch(() => setAuthState("error"));
            }}
            className="btn-primary w-full"
          >
            Thử lại
          </button>
        </div>
      </div>
    );
  }

  if (authState === "required") {
    return <LoginPage onSuccess={() => setAuthState("ok")} />;
  }

  return (
    <AppContent
      authRequired={authRequired}
      onLogout={async () => {
        await api.logout();
        setAuthState("required");
      }}
    />
  );
}

interface AppContentProps {
  authRequired: boolean;
  onLogout: () => void;
}

function AppContent({ authRequired, onLogout }: AppContentProps) {
  const { profiles, loading, error, actionError, clearActionError, create, update, remove, reorder, launch, stop, reset, duplicate } = useProfiles();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [view, setView] = useState<View>("empty");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null);
  const [updateDismissed, setUpdateDismissed] = useState(false);
  const [launchError, setLaunchError] = useState<LaunchDenial | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [stopped, setStopped] = useState(false);
  const confirm = useConfirm();
  const toast = useToast();

  const handleQuit = useCallback(async () => {
    const ok = await confirm({
      title: "Thoát CloakBrowser Manager (Custom)?",
      description: "Máy chủ sẽ dừng và tất cả hồ sơ đang chạy sẽ bị đóng.",
      confirmLabel: "Thoát",
      tone: "danger",
    });
    if (!ok) return;
    setStopped(true);
    try {
      await api.shutdown();
    } catch {
      // The server exits mid-request, so a network error here is expected.
    }
  }, [confirm]);

  // System status (tier, running count, binary) changes on launch/stop and on
  // key edits, so it is refreshed after those and on a slow interval rather
  // than fetched once.
  const refreshStatus = useCallback(() => {
    api.getStatus().then(setSystemStatus).catch(() => setSystemStatus(null));
  }, []);

  useEffect(() => {
    refreshStatus();
    api.checkUpdate().then(setUpdateInfo).catch(() => setUpdateInfo(null));
    const interval = setInterval(refreshStatus, 15000);
    return () => clearInterval(interval);
  }, [refreshStatus]);

  const selected = profiles.find((p) => p.id === selectedId) ?? null;

  // A viewed profile that stops (window closed, crash, stopped elsewhere) would
  // leave the viewer pane blank; fall back to its config view instead.
  useEffect(() => {
    if (view === "view" && selected?.status === "stopped") {
      setView("edit");
      refreshStatus();
    }
  }, [view, selected?.status, refreshStatus]);

  // Switching profiles clears the banner (like X); a post-handshake denial
  // (profile.last_error, arriving via the poll) shows only while its own
  // profile stays selected, never carried across a switch.
  const prevSelectedId = useRef(selectedId);
  const selectedLastErrorMsg = selected?.last_error?.message ?? null;
  useEffect(() => {
    const switched = prevSelectedId.current !== selectedId;
    prevSelectedId.current = selectedId;
    if (switched) {
      setLaunchError(null);
      return;
    }
    if (selected?.last_error) setLaunchError(selected.last_error);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, selectedLastErrorMsg]);

  const handleSelect = useCallback((id: string) => {
    setSelectedId(id);
    const profile = profiles.find((p) => p.id === id);
    setView(profile?.status === "running" ? "view" : "edit");
  }, [profiles]);

  const handleNew = useCallback(() => {
    setSelectedId(null);
    setView("create");
  }, []);

  const handleCreate = useCallback(async (data: ProfileCreateData) => {
    const profile = await create(data);
    if (profile) {
      setSelectedId(profile.id);
      setView("edit");
    }
  }, [create]);

  const handleUpdate = useCallback(async (data: ProfileCreateData) => {
    if (!selectedId) return;
    await update(selectedId, data);
  }, [selectedId, update]);

  const handleDelete = useCallback(async () => {
    if (!selectedId) return;
    await remove(selectedId);
    setSelectedId(null);
    setView("empty");
  }, [selectedId, remove]);

  // A license denial (out of seats, bad/expired key) → 402/403 with a
  // structured detail; surface it in the top banner.
  const reportLaunchError = useCallback((err: unknown) => {
    if (err instanceof ApiError && (err.status === 402 || err.status === 403)) {
      setLaunchError({
        message: err.message,
        reason: (err.reason as LaunchDenial["reason"]) ?? "license",
        upgrade_url: err.upgradeUrl,
      });
    } else {
      setLaunchError({
        message: err instanceof Error ? err.message : "Không thể khởi chạy hồ sơ",
        reason: "license",
      });
    }
  }, []);

  // Shared launch path: a license denial lands in the top banner, and the
  // error is rethrown so row buttons know the launch did not happen.
  const launchById = useCallback(async (id: string, openViewer: boolean) => {
    setLaunchError(null);
    try {
      const result = await launch(id);
      if (result && openViewer) setView("view");
      refreshStatus();
    } catch (err) {
      reportLaunchError(err);
      throw err;
    }
  }, [launch, refreshStatus, reportLaunchError]);

  const handleLaunch = useCallback(async () => {
    if (!selectedId) return;
    try {
      await launchById(selectedId, true);
    } catch {
      // Already shown in the launch banner.
    }
  }, [selectedId, launchById]);

  const handleEditById = useCallback((id: string) => {
    setSelectedId(id);
    setView("edit");
  }, []);

  const handleRowLaunch = useCallback(async (id: string) => {
    const name = profiles.find((p) => p.id === id)?.name ?? "";
    await launchById(id, false);
    toast.success(`Đã khởi chạy "${name}"`);
  }, [profiles, launchById, toast]);

  const handleRowStop = useCallback(async (id: string) => {
    const name = profiles.find((p) => p.id === id)?.name ?? "";
    try {
      await stop(id);
      toast.info(`Đã dừng "${name}"`);
    } finally {
      refreshStatus();
    }
  }, [profiles, stop, refreshStatus, toast]);

  const handleRowDuplicate = useCallback(async (id: string) => {
    try {
      const copy = await duplicate(id, false);
      toast.show({
        tone: "success",
        title: `Đã sao chép thành "${copy.name}"`,
        action: {
          label: "Mở cấu hình",
          onClick: () => {
            setSelectedId(copy.id);
            setView("edit");
          },
        },
      });
    } catch {
      // useProfiles records the failure in the error banner.
    }
  }, [duplicate, toast]);

  const handleRowDelete = useCallback(async (id: string) => {
    const profile = profiles.find((p) => p.id === id);
    if (!profile) return;
    const ok = await confirm({
      title: `Xóa hồ sơ "${profile.name}"?`,
      description: "Cookie, phiên đăng nhập, lịch sử và tệp của hồ sơ sẽ bị xóa vĩnh viễn.",
      confirmLabel: "Xóa hồ sơ",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await remove(id);
      if (selectedId === id) setSelectedId(null);
      toast.success(`Đã xóa hồ sơ "${profile.name}"`);
    } catch {
      // useProfiles records the failure in the error banner.
    }
  }, [profiles, confirm, remove, selectedId, toast]);

  const handleStop = useCallback(async () => {
    if (!selectedId) return;
    try {
      // A failed stop rethrows (LaunchButton reports it) and keeps the view.
      await stop(selectedId);
      setView("edit");
    } finally {
      refreshStatus();
    }
  }, [selectedId, stop, refreshStatus]);

  const handleReset = useCallback(async () => {
    if (!selectedId) return;
    await reset(selectedId);
    // Stays in the edit view — the profile is wiped but not launched.
  }, [selectedId, reset]);

  const handleDuplicate = useCallback(async (includeBrowserState: boolean) => {
    if (!selectedId) return;
    const profile = await duplicate(selectedId, includeBrowserState);
    if (profile) {
      setSelectedId(profile.id);
      setView("edit");
    }
  }, [selectedId, duplicate]);

  const handleClipboardSyncChange = useCallback(async (enabled: boolean) => {
    if (!selectedId) throw new Error("Chưa chọn hồ sơ");
    const updated = await update(selectedId, { clipboard_sync: enabled });
    if (!updated) throw new Error("Không thể lưu tùy chọn clipboard");
  }, [selectedId, update]);

  const handleVncDisconnect = useCallback(() => {
    setView("edit");
  }, []);

  if (loading) {
    return (
      <div className="h-screen flex items-center justify-center bg-surface-0 px-4">
        <div className="flex flex-col items-center gap-3">
          <div className="relative flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-brand/20 to-brand-deep/10 border border-brand/30 shadow-glow">
            <Shield className="h-6 w-6 text-brand" />
          </div>
          <div className="flex items-center gap-2 text-fg-secondary text-sm font-medium">
            <Loader2 className="h-4 w-4 animate-spin text-brand" />
            <span>Đang tải...</span>
          </div>
        </div>
      </div>
    );
  }

  if (stopped) {
    return (
      <div className="h-screen flex flex-col items-center justify-center bg-surface-0 text-center px-6">
        <div className="card max-w-md w-full p-8 flex flex-col items-center">
          <div className="h-12 w-12 rounded-2xl bg-surface-3 border border-line flex items-center justify-center mb-4">
            <Power className="h-6 w-6 text-fg-muted" />
          </div>
          <h1 className="text-lg font-semibold text-fg mb-1.5">CloakBrowser Manager (Custom) đã dừng</h1>
          <p className="text-sm text-fg-secondary">Máy chủ không còn chạy. Bạn có thể đóng tab này.</p>
          <p className="text-xs text-fg-muted mt-4 pt-4 border-t border-line w-full">
            Mở lại ứng dụng để khởi động lại.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col bg-surface-0 text-fg">
      {settingsOpen && (
        <SettingsPanel
          onClose={() => setSettingsOpen(false)}
          onSaved={setSystemStatus}
        />
      )}
      {updateInfo?.update_available && !updateDismissed && (
        <UpdateBanner info={updateInfo} onDismiss={() => setUpdateDismissed(true)} />
      )}
      {launchError && (
        <LaunchErrorBanner error={launchError} onDismiss={() => setLaunchError(null)} />
      )}
      <div className="flex-1 flex min-h-0">
      {/* Sidebar */}
      {sidebarOpen && (
        <div className="w-sidebar border-r border-line bg-surface-1/95 backdrop-blur-md flex-shrink-0">
          <ProfileList
            profiles={profiles}
            selectedId={selectedId}
            onSelect={handleSelect}
            onNew={handleNew}
            onReorder={reorder}
          />
        </div>
      )}

      {/* Main panel */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top bar */}
        <div className="flex h-topbar items-center justify-between px-4 border-b border-line bg-surface-1/90 backdrop-blur-md">
          <div className="flex items-center gap-2 min-w-0">
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="rail-item"
              title={sidebarOpen ? "Ẩn thanh bên" : "Hiện thanh bên"}
            >
              {sidebarOpen ? <PanelLeftClose className="h-4 w-4" /> : <PanelLeft className="h-4 w-4" />}
            </button>
            <div className="h-4 w-px bg-line mx-0.5" />
            <button
              type="button"
              onClick={() => {
                setSelectedId(null);
                setView("empty");
              }}
              className={`inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium transition-colors ${
                !selected && view === "empty"
                  ? "bg-surface-2 text-fg"
                  : "text-fg-secondary hover:bg-surface-2 hover:text-fg"
              }`}
              title="Tổng quan hồ sơ"
            >
              <LayoutGrid className="h-3.5 w-3.5 text-brand" />
              <span>Hồ sơ</span>
            </button>

            {view === "create" && !selected && (
              <>
                <ChevronRight className="h-3.5 w-3.5 text-fg-muted flex-shrink-0" />
                <span className="text-sm font-medium text-fg truncate">Tạo hồ sơ mới</span>
              </>
            )}

            {selected && (
              <>
                <ChevronRight className="h-3.5 w-3.5 text-fg-muted flex-shrink-0" />
                <div className="flex items-center gap-2 min-w-0">
                  <StatusIndicator status={selected.status} size="md" />
                  <span className="text-sm font-semibold text-fg truncate max-w-[220px]">
                    {selected.name}
                  </span>
                </div>
                {selected.status === "running" && (
                  <div className="ml-2 flex items-center rounded-lg border border-line bg-surface-0/80 p-0.5">
                    <button
                      type="button"
                      onClick={() => setView("view")}
                      className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-2xs font-medium transition-colors ${
                        view === "view"
                          ? "bg-brand-subtle text-brand"
                          : "text-fg-muted hover:text-fg"
                      }`}
                    >
                      <Eye className="h-3 w-3" />
                      <span>Trình xem</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => setView("edit")}
                      className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-2xs font-medium transition-colors ${
                        view === "edit"
                          ? "bg-brand-subtle text-brand"
                          : "text-fg-muted hover:text-fg"
                      }`}
                    >
                      <SlidersHorizontal className="h-3 w-3" />
                      <span>Cấu hình</span>
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            <SystemStatusBadge status={systemStatus} />
            {selected && (
              <LaunchButton
                key={selected.id}
                status={selected.status}
                onLaunch={handleLaunch}
                onStop={handleStop}
              />
            )}
            <div className="h-4 w-px bg-line mx-0.5" />
            <button
              onClick={() => setSettingsOpen(true)}
              className="rail-item"
              title="Cài đặt"
            >
              <Settings className="h-4 w-4" />
            </button>
            {authRequired && (
              <button
                onClick={onLogout}
                className="rail-item"
                title="Đăng xuất"
              >
                <Lock className="h-3.5 w-3.5" />
              </button>
            )}
            <button
              onClick={handleQuit}
              className="rail-item hover:!bg-danger/15 hover:!text-danger"
              title="Thoát Manager (dừng máy chủ)"
            >
              <Power className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* Error banner */}
        {/* An action error stays until dismissed; a poll error clears itself on
            the next good poll, so it gets no close button. */}
        {error && (
          <div
            role="alert"
            className="px-4 py-2.5 bg-danger/10 border-b border-danger/30 text-danger text-xs font-medium flex items-center gap-2"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-danger flex-shrink-0" />
            <span className="min-w-0 flex-1">
              {actionError ? error : `Mất kết nối tới máy chủ: ${error}`}
            </span>
            {actionError && (
              <button
                type="button"
                onClick={clearActionError}
                className="opacity-70 hover:opacity-100"
                aria-label="Đóng thông báo lỗi"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        )}

        {/* Content */}
        <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain">
          {view === "empty" &&
            ((systemStatus?.license_tier ?? "none") === "none" ? (
              <div className="flex h-full items-center justify-center px-6">
                <div className="max-w-sm text-center">
                  <p className="text-sm font-medium text-fg">Chưa có key bản quyền</p>
                  <p className="mt-1.5 text-sm text-fg-muted">
                    Hồ sơ cần key để khởi chạy. Key miễn phí chỉ cần một email và
                    dùng được bản dựng mới nhất.
                  </p>
                  <div className="mt-5 flex flex-col items-center gap-2">
                    <button onClick={() => setSettingsOpen(true)} className="btn-primary w-56">
                      Nhập key trong Cài đặt
                    </button>
                    <a
                      href="https://cloakbrowser.dev/free"
                      target="_blank"
                      rel="noreferrer"
                      className="btn-secondary w-56"
                    >
                      Lấy key miễn phí
                    </a>
                    <a
                      href="https://cloakbrowser.dev/#pricing"
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs text-fg-muted hover:text-fg-secondary"
                    >
                      Xem các gói Pro &rarr;
                    </a>
                  </div>
                </div>
              </div>
            ) : (
              <ProfileTable
                profiles={profiles}
                selectedId={selectedId}
                systemStatus={systemStatus}
                onSelect={handleSelect}
                onEdit={handleEditById}
                onNew={handleNew}
                onOpenSettings={() => setSettingsOpen(true)}
                onLaunch={handleRowLaunch}
                onStop={handleRowStop}
                onDuplicate={handleRowDuplicate}
                onDelete={handleRowDelete}
              />
            ))}

          {view === "create" && (
            <ProfileForm
              profile={null}
              hostOs={systemStatus?.host_os ?? null}
              viewerMode={systemStatus?.viewer_mode ?? null}
              onSave={handleCreate}
              onCancel={() => setView("empty")}
            />
          )}

          {view === "edit" && selected && (
            <ProfileForm
              profile={selected}
              hostOs={systemStatus?.host_os ?? null}
              viewerMode={systemStatus?.viewer_mode ?? null}
              onSave={handleUpdate}
              onDelete={handleDelete}
              onReset={handleReset}
              onDuplicate={handleDuplicate}
              onCancel={() => {
                setSelectedId(null);
                setView("empty");
              }}
            />
          )}

          {view === "view" && selected && selected.status === "running" && (
            selected.viewer_mode === "vnc" ? (
              <ProfileViewer
                key={selected.id}
                profileId={selected.id}
                profileName={selected.name}
                cdpUrl={selected.cdp_url}
                clipboardSync={selected.clipboard_sync}
                onClipboardSyncChange={handleClipboardSyncChange}
                onDisconnect={handleVncDisconnect}
              />
            ) : (
              <NativeWindowStatus
                key={selected.id}
                profileId={selected.id}
                profileName={selected.name}
                cdpUrl={selected.cdp_url}
                capturePreview={selected.capture_preview}
              />
            )
          )}
        </div>
      </div>
      </div>
      <StatusBar status={systemStatus} />
    </div>
  );
}
