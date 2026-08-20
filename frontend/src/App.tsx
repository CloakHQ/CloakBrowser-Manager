import { useState, useCallback, useEffect, useRef } from "react";
import { Lock, PanelLeftClose, PanelLeft, Settings, Power } from "lucide-react";
import { useProfiles } from "./hooks/useProfiles";
import { api, ApiError, setOnUnauthorized, type ProfileCreateData, type SystemStatus, type UpdateInfo, type LaunchDenial } from "./lib/api";
import { ProfileList } from "./components/ProfileList";
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
      <div className="h-screen flex items-center justify-center">
        <div className="text-gray-500 text-sm">Loading...</div>
      </div>
    );
  }

  if (authState === "error") {
    return (
      <div className="h-screen flex items-center justify-center bg-surface-0">
        <div className="text-center">
          <p className="text-red-400 text-sm mb-2">Unable to reach the server</p>
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
            className="text-xs text-gray-400 hover:text-gray-200 underline"
          >
            Retry
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
  const { profiles, loading, error, create, update, remove, reorder, launch, stop, reset, duplicate } = useProfiles();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [view, setView] = useState<View>("empty");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null);
  const [updateDismissed, setUpdateDismissed] = useState(false);
  const [launchError, setLaunchError] = useState<LaunchDenial | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [stopped, setStopped] = useState(false);

  const handleQuit = useCallback(async () => {
    if (!window.confirm("Quit CloakBrowser Manager? This stops the server and closes all running profiles.")) {
      return;
    }
    setStopped(true);
    try {
      await api.shutdown();
    } catch {
      // The server exits mid-request, so a network error here is expected.
    }
  }, []);

  useEffect(() => {
    api.getStatus().then(setSystemStatus).catch(() => setSystemStatus(null));
    api.checkUpdate().then(setUpdateInfo).catch(() => setUpdateInfo(null));
  }, []);

  const selected = profiles.find((p) => p.id === selectedId) ?? null;

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

  const handleLaunch = useCallback(async () => {
    if (!selectedId) return;
    setLaunchError(null);
    try {
      const result = await launch(selectedId);
      if (result) setView("view");
    } catch (err) {
      // A license denial (out of seats, bad/expired key) → 402/403 with a
      // structured detail; surface it in the top banner.
      if (err instanceof ApiError && (err.status === 402 || err.status === 403)) {
        setLaunchError({
          message: err.message,
          reason: (err.reason as LaunchDenial["reason"]) ?? "license",
          upgrade_url: err.upgradeUrl,
        });
      } else {
        setLaunchError({
          message: err instanceof Error ? err.message : "Failed to launch profile",
          reason: "license",
        });
      }
    }
  }, [selectedId, launch]);

  const handleStop = useCallback(async () => {
    if (!selectedId) return;
    await stop(selectedId);
    setView("edit");
  }, [selectedId, stop]);

  const handleReset = useCallback(async () => {
    if (!selectedId) return;
    await reset(selectedId);
    // Stays in the edit view — the profile is wiped but not launched.
  }, [selectedId, reset]);

  const handleDuplicate = useCallback(async () => {
    if (!selectedId) return;
    const profile = await duplicate(selectedId);
    if (profile) {
      setSelectedId(profile.id);
      setView("edit");
    }
  }, [selectedId, duplicate]);

  const handleClipboardSyncChange = useCallback(async (enabled: boolean) => {
    if (!selectedId) throw new Error("No profile selected");
    const updated = await update(selectedId, { clipboard_sync: enabled });
    if (!updated) throw new Error("Failed to save clipboard preference");
  }, [selectedId, update]);

  const handleVncDisconnect = useCallback(() => {
    setView("edit");
  }, []);

  if (loading) {
    return (
      <div className="h-screen flex items-center justify-center">
        <div className="text-gray-500 text-sm">Loading...</div>
      </div>
    );
  }

  if (stopped) {
    return (
      <div className="h-screen flex flex-col items-center justify-center bg-surface-0 text-center px-6">
        <Power className="h-10 w-10 text-gray-600 mb-4" />
        <h1 className="text-lg font-medium mb-1">CloakBrowser Manager has stopped</h1>
        <p className="text-sm text-gray-500">The server is no longer running. You can close this tab.</p>
        <p className="text-xs text-gray-600 mt-4">Relaunch the app to start it again.</p>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col">
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
        <div className="w-64 border-r border-border bg-surface-1 flex-shrink-0">
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
        <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface-1">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="text-gray-500 hover:text-gray-300 p-1"
              title={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
            >
              {sidebarOpen ? <PanelLeftClose className="h-4 w-4" /> : <PanelLeft className="h-4 w-4" />}
            </button>
            {selected && (
              <div className="flex items-center gap-2">
                <StatusIndicator status={selected.status} size="md" />
                <span className="text-sm font-medium">{selected.name}</span>
              </div>
            )}
          </div>
          <div className="flex items-center gap-3">
            <SystemStatusBadge status={systemStatus} />
            <button
              onClick={() => setSettingsOpen(true)}
              className="text-gray-500 hover:text-gray-300 p-1"
              title="Settings"
            >
              <Settings className="h-4 w-4" />
            </button>
            <button
              onClick={handleQuit}
              className="text-gray-500 hover:text-red-400 p-1"
              title="Quit Manager (stops the server)"
            >
              <Power className="h-4 w-4" />
            </button>
            {selected && (
              <LaunchButton
                key={selected.id}
                status={selected.status}
                onLaunch={handleLaunch}
                onStop={handleStop}
              />
            )}
            {authRequired && (
              <button
                onClick={onLogout}
                className="text-gray-500 hover:text-gray-300 p-1"
                title="Log out"
              >
                <Lock className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>

        {/* Error banner */}
        {error && (
          <div className="px-4 py-2 bg-red-600/15 border-b border-red-600/30 text-red-400 text-sm">
            {error}
          </div>
        )}

        {/* Content */}
        <div className="flex-1 overflow-y-auto overscroll-contain">
          {view === "empty" &&
            ((systemStatus?.license_tier ?? "keyless") === "keyless" ? (
              <div className="flex items-center justify-center h-full px-6">
                <div className="max-w-sm text-center">
                  <p className="text-sm font-medium text-gray-200">No license key set</p>
                  <p className="mt-1.5 text-sm text-gray-500">
                    You're running the free keyless build. Add a key to run the
                    latest Pro build.
                  </p>
                  <div className="mt-5 flex flex-col items-center gap-2">
                    <button
                      onClick={() => setSettingsOpen(true)}
                      className="btn-primary w-56"
                    >
                      Enter a key in Settings
                    </button>
                    <a
                      href="https://cloakbrowser.dev/free"
                      target="_blank"
                      rel="noreferrer"
                      className="btn-secondary w-56"
                    >
                      Get a free key
                    </a>
                    <a
                      href="https://cloakbrowser.dev/#pricing"
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs text-gray-500 hover:text-gray-300"
                    >
                      See Pro plans &rarr;
                    </a>
                  </div>
                  <p className="mt-6 text-xs text-gray-600">
                    Or select a profile or create a new one to continue keyless.
                  </p>
                </div>
              </div>
            ) : (
              <div className="flex items-center justify-center h-full">
                <div className="text-center">
                  <p className="text-gray-500 text-sm">Select a profile or create a new one</p>
                </div>
              </div>
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
    </div>
  );
}
