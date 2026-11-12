/**
 * API client for CloakBrowser Manager backend.
 */

export type HostOS = "windows" | "macos" | "linux";
export type RuntimeMode = "native" | "docker";
export type ViewerMode = "native-window" | "vnc";

export interface ProfileFile {
  id: string;
  profile_id: string;
  name: string;
  size: number;
  kind: string;
  state: string;
  content_type: string | null;
  created_at: string;
  /** Where the browser process sees the file, for CDP calls that take a path. */
  container_path: string;
}

// Chromium Memory Saver: off, or how long a tab stays hidden before it is
// discarded (conservative 6 h, medium 4 h, aggressive 2 h).
export type MemorySaver = "off" | "conservative" | "medium" | "aggressive";

export interface Profile {
  id: string;
  name: string;
  license_key_set: boolean;
  license_key_masked: string | null;
  release_channel: "stable" | "preview";
  browser_version: string | null;
  fingerprint_seed: number;
  proxy: string | null;
  timezone: string | null;
  locale: string | null;
  screen_width: number;
  screen_height: number;
  gpu_family: "auto" | "nvidia" | "intel";
  humanize: boolean;
  human_preset: string;
  geoip: boolean;
  clipboard_sync: boolean;
  auto_launch: boolean;
  color_scheme: string | null;
  launch_args: string[];
  extension_paths: string[];
  extension_ids: string[];
  allow_3p_cookies: boolean;
  set_google_default: boolean;
  capture_preview: boolean;
  restore_session: boolean;
  memory_saver: MemorySaver;
  transparent_proxy: boolean;
  portable_cookies: boolean;
  notes: string | null;
  user_data_dir: string;
  created_at: string;
  updated_at: string;
  sort_order: number;
  tags: { tag: string; color: string | null }[];
  status: "running" | "stopped" | "initializing";
  runtime_mode: RuntimeMode;
  viewer_mode: ViewerMode;
  vnc_ws_port: number | null;
  cdp_url: string | null;
  // Set when the last launch closed on a license denial (out of seats / bad
  // key). Cleared on the next launch. Shown under the Launch button.
  last_error: LaunchDenial | null;
}

export interface LaunchDenial {
  message: string;
  reason: "seat_limit" | "license";
  upgrade_url?: string;
}

export interface ProfileCreateData {
  name: string;
  license_key?: string | null;
  release_channel?: "stable" | "preview";
  browser_version?: string | null;
  fingerprint_seed?: number | null;
  proxy?: string | null;
  timezone?: string | null;
  locale?: string | null;
  screen_width?: number;
  screen_height?: number;
  gpu_family?: "auto" | "nvidia" | "intel";
  humanize?: boolean;
  human_preset?: string;
  geoip?: boolean;
  clipboard_sync?: boolean;
  auto_launch?: boolean;
  color_scheme?: string | null;
  launch_args?: string[];
  extension_paths?: string[];
  extension_ids?: string[];
  allow_3p_cookies?: boolean;
  set_google_default?: boolean;
  capture_preview?: boolean;
  restore_session?: boolean;
  memory_saver?: MemorySaver;
  transparent_proxy?: boolean;
  portable_cookies?: boolean;
  notes?: string | null;
  tags?: { tag: string; color: string | null }[];
}

export interface LaunchResult {
  profile_id: string;
  status: string;
  runtime_mode: RuntimeMode;
  viewer_mode: ViewerMode;
  vnc_ws_port: number | null;
  display: string | null;
  cdp_url: string | null;
}

export interface ProxyTestResult {
  ok: boolean;
  ip?: string | null;
  country?: string | null;
  city?: string | null;
  timezone?: string | null;
  latency_ms?: number | null;
  error?: string | null;
}

export interface SystemStatus {
  running_count: number;
  installed_binary_count: number;
  binary_cache_dir: string;
  profiles_total: number;
  host_os: HostOS;
  runtime_mode: RuntimeMode;
  viewer_mode: ViewerMode;
  windows_fonts_present: number | null;
  windows_fonts_required: number | null;
  windows_fonts_complete: boolean | null;
}

export interface UpdateInfo {
  current: string;
  latest: string | null;
  update_available: boolean;
  release_url: string | null;
}

export interface RuntimeConfig {
  sidebar_width: string;
}

export interface BrowserBinary {
  version: string;
  tier: "licensed" | "keyless";
  path: string;
  size_bytes: number;
  profile_count: number;
  running_count: number;
  in_use: boolean;
}

export interface BrowserBinaryList {
  cache_dir: string;
  binaries: BrowserBinary[];
}

export interface BrowserDownloadResult {
  version: string | null;
  tier: "licensed" | "keyless";
  binary_path: string;
}

export interface BrowserCleanupResult {
  removed: BrowserBinary[];
  reclaimed_bytes: number;
}

export type ExtensionSourceKind = "webstore" | "crx" | "zip" | "upload";

/** One entry of the Manager-wide extension library, shared by every profile. */
export interface LibraryExtension {
  id: string;
  name: string;
  description: string | null;
  version: string;
  previous_version: string | null;
  manifest_version: 2 | 3;
  source_kind: ExtensionSourceKind;
  source_url: string | null;
  webstore_id: string | null;
  /** "/api/extensions/{id}/icon" when the extension ships an icon. */
  icon_url: string | null;
  profile_count: number;
  created_at: string;
  updated_at: string;
}

export interface ExtensionUpdateResult {
  extension: LibraryExtension;
  updated: boolean;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    // Set when the backend returns a structured detail (e.g. a launch blocked
    // by a license/seat problem): "seat_limit" | "license", plus an upgrade URL.
    public reason?: string,
    public upgradeUrl?: string,
  ) {
    super(message);
  }
}

// Global 401 callback — set by App to trigger login page on auth failure
let _onUnauthorized: (() => void) | null = null;
export function setOnUnauthorized(cb: (() => void) | null) {
  _onUnauthorized = cb;
}

async function request<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    if (res.status === 401 && _onUnauthorized) {
      _onUnauthorized();
      throw new ApiError(401, "Unauthorized");
    }
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = body.detail;
    // FastAPI detail is usually a string, but license/seat denials return an
    // object {message, reason, upgrade_url} so the UI can show a CTA.
    if (detail && typeof detail === "object") {
      throw new ApiError(
        res.status,
        detail.message || res.statusText,
        detail.reason,
        detail.upgrade_url,
      );
    }
    throw new ApiError(res.status, detail || res.statusText);
  }
  return res.json();
}

// The file is the body itself, streamed as-is. Its name travels in a header,
// URL-encoded, because header values cannot carry arbitrary Unicode.
async function uploadRequest<T>(path: string, file: File): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    body: file,
    headers: {
      "X-File-Name": encodeURIComponent(file.name),
      "Content-Type": file.type || "application/octet-stream",
    },
  });
  if (!res.ok) {
    if (res.status === 401 && _onUnauthorized) {
      _onUnauthorized();
      throw new ApiError(401, "Unauthorized");
    }
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = typeof body.detail === "string" ? body.detail : res.statusText;
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export const api = {
  authStatus: () =>
    request<{ auth_required: boolean; authenticated: boolean }>("/api/auth/status"),

  login: (token: string) =>
    request<{ ok: boolean }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  logout: () =>
    request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),

  listProfiles: () => request<Profile[]>("/api/profiles"),

  reorderProfiles: (ids: string[]) =>
    request<{ ok: boolean }>("/api/profiles/reorder", {
      method: "POST",
      body: JSON.stringify({ ordered_ids: ids }),
    }),

  getProfile: (id: string) => request<Profile>(`/api/profiles/${id}`),

  createProfile: (data: ProfileCreateData) =>
    request<Profile>("/api/profiles", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  testProxy: (proxy: string) =>
    request<ProxyTestResult>("/api/profiles/test-proxy", {
      method: "POST",
      body: JSON.stringify({ proxy }),
    }),

  updateProfile: (id: string, data: Partial<ProfileCreateData>) =>
    request<Profile>(`/api/profiles/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  deleteProfile: (id: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}`, { method: "DELETE" }),

  resetProfile: (id: string) =>
    request<Profile>(`/api/profiles/${id}/reset`, { method: "POST" }),

  clearProfileData: (id: string) =>
    request<Profile>(`/api/profiles/${id}/clear-data`, { method: "POST" }),

  duplicateProfile: (id: string, includeBrowserState = false) =>
    request<Profile>(`/api/profiles/${id}/duplicate`, {
      method: "POST",
      body: JSON.stringify({ include_browser_state: includeBrowserState }),
    }),

  listProfileFiles: (id: string) =>
    request<ProfileFile[]>(`/api/profiles/${id}/files`),

  uploadProfileFile: (id: string, file: File): Promise<ProfileFile> =>
    uploadRequest<ProfileFile>(`/api/profiles/${id}/files`, file),

  deleteProfileFile: (id: string, fileId: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}/files/${fileId}`, { method: "DELETE" }),

  profileFileUrl: (id: string, fileId: string) => `/api/profiles/${id}/files/${fileId}`,

  launchProfile: (id: string) =>
    request<LaunchResult>(`/api/profiles/${id}/launch`, { method: "POST" }),

  stopProfile: (id: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}/stop`, { method: "POST" }),

  getStatus: () => request<SystemStatus>("/api/status"),

  getConfig: () => request<RuntimeConfig>("/api/config"),

  checkUpdate: () => request<UpdateInfo>("/api/update-check"),

  openExternal: (url: string) =>
    request<{ ok: boolean }>("/api/open-external", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  shutdown: () =>
    request<{ ok: boolean; message?: string }>("/api/shutdown", { method: "POST" }),

  listBrowsers: () => request<BrowserBinaryList>("/api/browsers"),

  cleanupUnusedBrowsers: () =>
    request<BrowserCleanupResult>("/api/browsers/unused", { method: "DELETE" }),

  downloadProfileBrowser: (id: string) =>
    request<BrowserDownloadResult>(`/api/profiles/${id}/browser/download`, {
      method: "POST",
    }),

  setClipboard: (id: string, text: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}/clipboard`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),

  getClipboard: (id: string) =>
    request<{ text: string }>(`/api/profiles/${id}/clipboard`),

  // ── Extension library ──
  listExtensions: () => request<LibraryExtension[]>("/api/extensions"),

  addExtension: (source: string) =>
    request<LibraryExtension>("/api/extensions", {
      method: "POST",
      body: JSON.stringify({ source }),
    }),

  // Uploading with an extensionId installs a new version of that entry.
  uploadExtension: (file: File, extensionId?: string) =>
    uploadRequest<LibraryExtension>(
      extensionId
        ? `/api/extensions/upload?extension_id=${encodeURIComponent(extensionId)}`
        : "/api/extensions/upload",
      file,
    ),

  // source null re-downloads the stored webstore ID / URL.
  updateExtension: (id: string, source: string | null = null) =>
    request<ExtensionUpdateResult>(`/api/extensions/${id}/update`, {
      method: "POST",
      body: JSON.stringify({ source }),
    }),

  rollbackExtension: (id: string) =>
    request<LibraryExtension>(`/api/extensions/${id}/rollback`, { method: "POST" }),

  deleteExtension: (id: string) =>
    request<{ ok: boolean }>(`/api/extensions/${id}`, { method: "DELETE" }),

  extensionIconUrl: (id: string) => `/api/extensions/${id}/icon`,
};
