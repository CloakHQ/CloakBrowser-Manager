/**
 * API client for CloakBrowser Manager backend.
 */

/** Backend lifecycle state. "starting" = launch in flight (container
 *  restart / auto-launch queue) — transient, never terminal. */
export type ProfileLifecycle = "running" | "starting" | "stopped";

export interface Profile {
  id: string;
  name: string;
  fingerprint_seed: number;
  proxy: string | null;
  timezone: string | null;
  locale: string | null;
  platform: string;
  user_agent: string | null;
  screen_width: number;
  screen_height: number;
  gpu_vendor: string | null;
  gpu_renderer: string | null;
  hardware_concurrency: number | null;
  humanize: boolean;
  human_preset: string;
  headless: boolean;
  geoip: boolean;
  clipboard_sync: boolean;
  auto_launch: boolean;
  color_scheme: string | null;
  launch_args: string[];
  notes: string | null;
  user_data_dir: string;
  created_at: string;
  updated_at: string;
  tags: { tag: string; color: string | null }[];
  status: ProfileLifecycle;
  vnc_ws_port: number | null;
  cdp_url: string | null;
}

export interface ProfileCreateData {
  name: string;
  fingerprint_seed?: number | null;
  proxy?: string | null;
  timezone?: string | null;
  locale?: string | null;
  platform?: string;
  user_agent?: string | null;
  screen_width?: number;
  screen_height?: number;
  gpu_vendor?: string | null;
  gpu_renderer?: string | null;
  hardware_concurrency?: number | null;
  humanize?: boolean;
  human_preset?: string;
  headless?: boolean;
  geoip?: boolean;
  clipboard_sync?: boolean;
  auto_launch?: boolean;
  color_scheme?: string | null;
  launch_args?: string[];
  notes?: string | null;
  tags?: { tag: string; color: string | null }[];
}

export interface LaunchResult {
  profile_id: string;
  status: string;
  vnc_ws_port: number;
  display: string;
  cdp_url: string | null;
}

export interface SystemStatus {
  running_count: number;
  binary_version: string;
  profiles_total: number;
}

export interface ViewerToken {
  token: string;
  viewer_url: string;
  expires_in: number;
}

export interface ProfileStatus {
  status: ProfileLifecycle;
  xvnc_alive: boolean | null;
  browser_alive: boolean | null;
}

class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// Global 401 callback — set by App to trigger login page on auth failure
let _onUnauthorized: (() => void) | null = null;
export function setOnUnauthorized(cb: (() => void) | null) {
  _onUnauthorized = cb;
}

/**
 * No browser applies a default fetch timeout. A connection that opens but never
 * responds (a middlebox or tunnel dropping a half-open socket) hangs until the
 * OS retransmit cap — minutes. The viewer's reconnect machine awaits these
 * calls and serialises on them, so one stalled request stops automatic recovery
 * for that entire window. Bound them instead: an abort surfaces as a rejection,
 * which the state machine already handles by backing off and retrying.
 */
const REQUEST_TIMEOUT_MS = 15_000;
/**
 * Lifecycle mutations legitimately run long and must NOT share the short
 * budget: the backend allows a launch 60s (Xvnc readiness alone is 15s before
 * Chromium even starts), stop closes a context plus up to 10s of Xvnc
 * teardown, and delete rmtree's a profile directory that can be hundreds of
 * MB. Aborting those client-side reports a failure for an operation the server
 * completes anyway — and the next click then answers 409.
 */
const MUTATION_TIMEOUT_MS = 120_000;

function timeoutSignal(ms: number): AbortSignal | undefined {
  // AbortSignal.timeout is unavailable in some test/older environments.
  return typeof AbortSignal !== "undefined" && "timeout" in AbortSignal
    ? AbortSignal.timeout(ms)
    : undefined;
}

async function request<T>(
  path: string,
  options?: RequestInit,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    signal: timeoutSignal(timeoutMs),
    ...options,
  });
  if (!res.ok) {
    if (res.status === 401 && _onUnauthorized) {
      _onUnauthorized();
      throw new ApiError(401, "Unauthorized");
    }
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail || res.statusText);
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

  getProfile: (id: string) => request<Profile>(`/api/profiles/${id}`),

  createProfile: (data: ProfileCreateData) =>
    request<Profile>("/api/profiles", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  updateProfile: (id: string, data: Partial<ProfileCreateData>) =>
    request<Profile>(`/api/profiles/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  deleteProfile: (id: string) =>
    request<{ ok: boolean }>(
      `/api/profiles/${id}`, { method: "DELETE" }, MUTATION_TIMEOUT_MS,
    ),

  launchProfile: (id: string) =>
    request<LaunchResult>(
      `/api/profiles/${id}/launch`, { method: "POST" }, MUTATION_TIMEOUT_MS,
    ),

  stopProfile: (id: string) =>
    request<{ ok: boolean }>(
      `/api/profiles/${id}/stop`, { method: "POST" }, MUTATION_TIMEOUT_MS,
    ),

  getStatus: () => request<SystemStatus>("/api/status"),

  setClipboard: (id: string, text: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}/clipboard`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),

  getClipboard: (id: string) =>
    request<{ text: string }>(`/api/profiles/${id}/clipboard`),

  createViewerToken: (id: string) =>
    request<ViewerToken>(`/api/profiles/${id}/viewer-token`, { method: "POST" }),

  profileStatus: (id: string) =>
    request<ProfileStatus>(`/api/profiles/${id}/status`),
};

export { ApiError };
