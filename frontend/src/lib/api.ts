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

export type ProxyProvider = "none" | "kiotproxy" | "tmproxy";

export interface Profile {
  id: string;
  name: string;
  fingerprint_seed: number;
  proxy: string | null;
  proxy_provider: ProxyProvider;
  proxy_provider_key_set: boolean;
  proxy_provider_key_masked: string | null;
  kiotproxy_region: "bac" | "trung" | "nam" | "random";
  tmproxy_location_id: number;
  tmproxy_isp_id: number;
  active_proxy: ProxyProviderInfo | null;
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
  allow_3p_cookies: boolean;
  set_google_default: boolean;
  capture_preview: boolean;
  restore_session: boolean;
  transparent_proxy: boolean;
  portable_cookies: boolean;
  // Pinned pool key, or null to lease whichever key is free at launch.
  license_key_id: string | null;
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
  fingerprint_seed?: number | null;
  proxy?: string | null;
  proxy_provider?: ProxyProvider;
  proxy_provider_key?: string | null;
  kiotproxy_region?: "bac" | "trung" | "nam" | "random";
  tmproxy_location_id?: number;
  tmproxy_isp_id?: number;
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
  allow_3p_cookies?: boolean;
  set_google_default?: boolean;
  capture_preview?: boolean;
  restore_session?: boolean;
  transparent_proxy?: boolean;
  portable_cookies?: boolean;
  license_key_id?: string | null;
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

export interface ProxyProviderInfo {
  provider: string;
  host: string | null;
  real_ip: string | null;
  location: string | null;
  isp: string | null;
  expires_at: number | string | null;
  ttl: number | null;
  ttc: number | null;
  next_request_at: number | null;
}

export interface TmProxyOption {
  id: number;
  name: string | null;
}

export interface SystemStatus {
  running_count: number;
  binary_version: string;
  license_tier: string; // "pro" | "free" | "none"
  license_plan?: string | null; // server-reported plan, informational only
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

export interface ManagerSettings {
  license_key_set: boolean;
  license_key_masked: string | null;
  release_channel: string; // "stable" | "preview"
  key_pool_size: number;
}
export interface LicenseKeyEntry {
  id: string;
  key_masked: string;
  label: string | null;
  // "valid" | "invalid" | "unreachable" | "unknown"
  state: string;
  plan: string | null;
  expires: string | null;
  added_at: string;
  checked_at: string | null;
  // The profile currently running on this key, or null.
  leased_to: string | null;
  // ISO UTC until which the license server still counts this key's last
  // session. Null means the key can be leased right away.
  available_at: string | null;
}
export interface LicenseKeyCheckResult {
  id: string;
  key_masked: string;
  state: string;
  plan: string | null;
  expires: string | null;
}

export interface SettingsUpdate {
  license_key?: string | null; // omit = unchanged; "" = clear
  release_channel?: string | null;
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
      throw new ApiError(401, "Chưa được xác thực");
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
    throw new ApiError(res.status, detail || "Yêu cầu thất bại");
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

  kiotproxyNew: (key: string, region: "bac" | "trung" | "nam" | "random") =>
    request<ProxyProviderInfo>("/api/proxy-providers/kiotproxy/new", {
      method: "POST",
      body: JSON.stringify({ key, region }),
    }),
  kiotproxyCurrent: (key: string) =>
    request<ProxyProviderInfo>("/api/proxy-providers/kiotproxy/current", {
      method: "POST",
      body: JSON.stringify({ key }),
    }),
  /** The provider proxy this profile's *stored* key holds — no key input needed. */
  profileCurrentProxy: (profileId: string) =>
    request<ProxyProviderInfo>(`/api/profiles/${profileId}/proxy/current`, {
      method: "POST",
    }),
  kiotproxyOut: (key: string) =>
    request<{ ok: boolean }>("/api/proxy-providers/kiotproxy/out", {
      method: "POST",
      body: JSON.stringify({ key }),
    }),
  tmproxyNew: (apiKey: string, locationId = 0, ispId = 0) =>
    request<ProxyProviderInfo>("/api/proxy-providers/tmproxy/new", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey, location_id: locationId, isp_id: ispId }),
    }),
  tmproxyCurrent: (apiKey: string) =>
    request<ProxyProviderInfo>("/api/proxy-providers/tmproxy/current", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey }),
    }),
  tmproxyLocations: () =>
    request<TmProxyOption[]>("/api/proxy-providers/tmproxy/locations"),
  tmproxyIsps: () => request<TmProxyOption[]>("/api/proxy-providers/tmproxy/isps"),

  updateProfile: (id: string, data: Partial<ProfileCreateData>) =>
    request<Profile>(`/api/profiles/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  deleteProfile: (id: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}`, { method: "DELETE" }),

  resetProfile: (id: string) =>
    request<Profile>(`/api/profiles/${id}/reset`, { method: "POST" }),

  duplicateProfile: (id: string, includeBrowserState = false) =>
    request<Profile>(`/api/profiles/${id}/duplicate`, {
      method: "POST",
      body: JSON.stringify({ include_browser_state: includeBrowserState }),
    }),

  listProfileFiles: (id: string) =>
    request<ProfileFile[]>(`/api/profiles/${id}/files`),

  uploadProfileFile: async (id: string, file: File): Promise<ProfileFile> => {
    // The file is the body itself, streamed as-is. Its name travels in a header, URL-encoded,
    // because header values cannot carry arbitrary Unicode.
    const res = await fetch(`/api/profiles/${id}/files`, {
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
        throw new ApiError(401, "Chưa được xác thực");
      }
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      const detail = typeof body.detail === "string" ? body.detail : "Yêu cầu thất bại";
      throw new ApiError(res.status, detail);
    }
    return res.json();
  },

  deleteProfileFile: (id: string, fileId: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}/files/${fileId}`, { method: "DELETE" }),

  profileFileUrl: (id: string, fileId: string) => `/api/profiles/${id}/files/${fileId}`,

  launchProfile: (id: string) =>
    request<LaunchResult>(`/api/profiles/${id}/launch`, { method: "POST" }),

  stopProfile: (id: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}/stop`, { method: "POST" }),

  getStatus: () => request<SystemStatus>("/api/status"),

  checkUpdate: () => request<UpdateInfo>("/api/update-check"),

  openExternal: (url: string) =>
    request<{ ok: boolean }>("/api/open-external", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  shutdown: () =>
    request<{ ok: boolean; message?: string }>("/api/shutdown", { method: "POST" }),

  getSettings: () => request<ManagerSettings>("/api/settings"),
  listKeys: () => request<LicenseKeyEntry[]>("/api/keys"),
  addKeys: (keys: string, label?: string) =>
    request<LicenseKeyEntry[]>("/api/keys", {
      method: "POST",
      body: JSON.stringify({ keys, label: label ?? null }),
    }),
  deleteKey: (id: string) =>
    request<{ ok: boolean }>(`/api/keys/${id}`, { method: "DELETE" }),
  // No ids = re-check the whole pool. The backend field is a list, so send
  // [] (not null) for "all" or Pydantic rejects the body with a 422.
  checkKeys: (ids?: string[]) =>
    request<LicenseKeyCheckResult[]>("/api/keys/check", {
      method: "POST",
      body: JSON.stringify({ ids: ids ?? [] }),
    }),

  updateSettings: (data: SettingsUpdate) =>
    request<SystemStatus>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  setClipboard: (id: string, text: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}/clipboard`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),

  getClipboard: (id: string) =>
    request<{ text: string }>(`/api/profiles/${id}/clipboard`),
};
