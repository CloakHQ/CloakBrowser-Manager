/**
 * API client for CloakBrowser Manager backend.
 */

/** Backend lifecycle state.
 *  running  = a live RunningProfile exists; xvnc_alive/browser_alive carry the
 *             real probe results.
 *  starting = launch in flight or queued behind auto-launch (container
 *             restart / auto-launch queue) — transient, never terminal.
 *  stopping = teardown in progress; the browser is being closed and
 *             launch/stop are refused with 409 until it completes. The manager
 *             has already dropped the profile out of `running`, so
 *             /viewer-token 404s and /api/viewer-auth 403s: transient for the
 *             profile, but NOT recoverable for an open viewer session.
 *  stopped  = nothing held for this profile.
 *  ProfileStatus.xvnc_alive/browser_alive are null for "stopping" exactly as
 *  they are for "stopped" — there is no RunningProfile left to probe.
 *
 *  Declared as a runtime tuple, not a bare union, so the set is assertable in a
 *  test: tsconfig excludes *.test.ts from `tsc`, so a type-only pin would never
 *  actually be checked. Adding a value here is a compile error in every
 *  Record<ProfileLifecycle, …> consumer (App's VIEW_ON_SELECT,
 *  LaunchButton's BUSY_LABEL, StatusIndicator's DOT_CLASS) — which is the
 *  point: the previous three-value union was compile-checked in exactly one of
 *  them, so "stopping" would have fallen through to an enabled Launch button
 *  and a viewer opened on a profile whose /viewer-token 404s. */
export const PROFILE_LIFECYCLES = [
  "running",
  "starting",
  "stopping",
  "stopped",
] as const;
export type ProfileLifecycle = (typeof PROFILE_LIFECYCLES)[number];

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
  /** Per-profile CloakBrowser license key override. Blank inherits whatever
   *  the container's CLOAKBROWSER_LICENSE_KEY is (or free tier if it has
   *  none either) — it does not force free tier over a container-wide key. */
  license_key: string | null;
  /** Ids (Extension.id) of the extensions this profile loads, from the set
   *  GET /api/extensions returns. */
  enabled_extensions: string[];
  /** Seconds of no VNC-viewer/CDP activity before this profile auto-stops.
   *  null defers to SystemStatus.default_idle_timeout_seconds; 0 disables
   *  idle timeout for this profile. */
  idle_timeout_seconds: number | null;
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
  license_key?: string | null;
  enabled_extensions?: string[] | null;
  idle_timeout_seconds?: number | null;
  launch_args?: string[];
  notes?: string | null;
  tags?: { tag: string; color: string | null }[];
}

export interface LaunchResult {
  profile_id: string;
  status: string;
  /** null for a headless profile: it allocates no display and no Xvnc. */
  vnc_ws_port: number | null;
  display: string | null;
  cdp_url: string | null;
}

export interface SystemStatus {
  running_count: number;
  binary_version: string;
  profiles_total: number;
  /** CloakBrowser Chromium downloads on first launch — these three describe
   * that download so the UI can show a progress banner. state is
   * "downloading" | "extracting" | null (idle/done). */
  binary_downloading: boolean;
  binary_download_percent: number | null;
  binary_download_state: string | null;
  /** PROFILE_IDLE_TIMEOUT_SECONDS as actually resolved — what a blank
   *  per-profile idle timeout field means. */
  default_idle_timeout_seconds: number;
}

/** An unpacked extension found under EXTENSIONS_DIR at container startup —
 *  see backend/extensions.py. The set never changes without a restart. */
export interface Extension {
  id: string;
  name: string;
  description: string | null;
  version: string | null;
}

export interface ViewerToken {
  token: string;
  viewer_url: string;
  expires_in: number;
  /**
   * `kasmvnc_mode_preference` for the client, or null/absent to let it choose.
   * Only sent when the server is configured for an NVENC codec, which the
   * bundled client cannot auto-select. See buildViewerUrl.
   */
  stream_mode?: string | null;
}

export interface ProfileStatus {
  status: ProfileLifecycle;
  xvnc_alive: boolean | null;
  browser_alive: boolean | null;
}

export interface DownloadFile {
  name: string;
  isDirectory: boolean;
  /** cubone/react-file-manager's own convention: "/<filename>", relative to
   *  the profile's Downloads dir (which is flat — no subfolders). */
  path: string;
  size: number | null;
  updatedAt: string;
}

export interface ResourceUsage {
  /** Un-normalized per-core percent (top's convention): three fully-busy
   *  renderer processes can legitimately sum past 100. null (not 0) means
   *  the browser process is already gone — distinct from genuinely idle. */
  cpu_percent: number | null;
  memory_mb: number | null;
  process_count: number;
}

export interface CookieWarmupStatus {
  state: "idle" | "running" | "done" | "error" | "cancelled";
  sites_total: number;
  sites_visited: number;
  current_site: string | null;
  elapsed_seconds: number | null;
  remaining_seconds: number | null;
  error: string | null;
}

export interface ViewerAttached {
  /**
   * Whether a viewer WebSocket is attached, or null if the probe could not
   * answer. null is NOT "no viewer" — a stats-endpoint hiccup must never be
   * mistaken for a dead socket.
   */
  viewer_attached: boolean | null;
  /** attached endpoints (-AlwaysShared can exceed 1), null if indeterminate. */
  clients: number | null;
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

  duplicateProfile: (id: string) =>
    request<Profile>(`/api/profiles/${id}/duplicate`, { method: "POST" }),

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

  listExtensions: () => request<Extension[]>("/api/extensions"),

  rescanExtensions: () =>
    request<Extension[]>("/api/extensions/rescan", { method: "POST" }),

  uploadExtension: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    // headers: {} overrides request()'s default JSON content-type — fetch
    // sets its own multipart boundary from the FormData body, and setting
    // Content-Type by hand here would omit that boundary and break parsing.
    return request<Extension[]>(
      "/api/extensions/upload",
      { method: "POST", headers: {}, body: formData },
      MUTATION_TIMEOUT_MS,
    );
  },

  installExtensionFromUrl: (url: string) =>
    request<Extension[]>(
      "/api/extensions/install-from-url",
      { method: "POST", body: JSON.stringify({ url }) },
      MUTATION_TIMEOUT_MS,
    ),

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

  profileResources: (id: string) =>
    request<ResourceUsage>(`/api/profiles/${id}/resources`),

  listDownloads: (id: string) =>
    request<DownloadFile[]>(`/api/profiles/${id}/downloads`),

  deleteDownload: (id: string, path: string) =>
    request<{ ok: boolean }>(`/api/profiles/${id}/downloads${path}`, { method: "DELETE" }),

  viewerAttached: (id: string) =>
    request<ViewerAttached>(`/api/profiles/${id}/viewer-attached`),

  startCookieWarmup: (id: string) =>
    request<CookieWarmupStatus>(`/api/profiles/${id}/cookie-warmup/start`, { method: "POST" }),

  cookieWarmupStatus: (id: string) =>
    request<CookieWarmupStatus>(`/api/profiles/${id}/cookie-warmup/status`),

  stopCookieWarmup: (id: string) =>
    request<CookieWarmupStatus>(`/api/profiles/${id}/cookie-warmup/stop`, { method: "POST" }),
};

// Not a JSON fetch — a URL for window.open()/an <a href>, so the browser's
// own download flow handles the FileResponse(..., filename=...) the server
// sends (Content-Disposition: attachment). `path` already carries its own
// leading "/" (cubone/react-file-manager's convention), hence no separator
// here. A plain function, not a member of `api`, so it isn't swept into
// api.test.ts's "every api method bounds its fetch" exhaustiveness check —
// it never calls fetch at all.
export function downloadFileUrl(id: string, path: string): string {
  return `/api/profiles/${id}/downloads${path}`;
}

// Same reasoning as downloadFileUrl above: a URL for window.open(), not a
// JSON fetch, so it's a standalone function rather than an `api` member.
export function downloadsZipUrl(id: string): string {
  return `/api/profiles/${id}/downloads-zip`;
}

export { ApiError };
