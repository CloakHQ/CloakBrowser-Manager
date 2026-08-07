"""Pydantic models for profile CRUD operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# The lifecycle contract, machine-checked on the way out. Kept as a Literal so
# adding a value is a type error at every producer instead of a string that
# silently reaches a frontend that has never heard of it — the mirror of
# frontend/src/lib/api.ts's PROFILE_LIFECYCLES tuple, which these four values
# must stay identical to.
#   running  = a live RunningProfile exists
#   starting = a launch is in flight or queued behind auto-launch
#   stopping = teardown in flight (or wedged); the browser may still be alive
#              and launch/stop/delete all refuse with 409
#   stopped  = nothing is held for this profile
ProfileLifecycleT = Literal["running", "starting", "stopping", "stopped"]


class ProfileCreate(BaseModel):
    name: str
    fingerprint_seed: int | None = None  # random if not set
    proxy: str | None = None  # "http://user:pass@host:port" or null
    timezone: str | None = None  # "America/New_York"
    locale: str | None = None  # "en-US"
    platform: Literal["windows", "macos", "linux"] = "windows"
    user_agent: str | None = None
    screen_width: int = 1920
    screen_height: int = 1080
    gpu_vendor: str | None = None
    gpu_renderer: str | None = None
    hardware_concurrency: int | None = None
    humanize: bool = False
    human_preset: Literal["default", "careful"] = "default"
    headless: bool = False
    geoip: bool = False
    clipboard_sync: bool = True
    auto_launch: bool = False
    # Relaunch this profile automatically if its Chromium dies unexpectedly
    # (crash, OOM kill) — never for a launch the user or the idle-timeout
    # reaper stopped on purpose. See browser_manager.py's _on_browser_closed
    # for how those two are told apart, and reap_dead_browsers for how a
    # driver-killed crash with no "close" event is caught at all. Bounded by
    # AUTO_RESTART_MAX_ATTEMPTS within AUTO_RESTART_WINDOW_S so a profile
    # that crashes on launch cannot loop forever.
    auto_restart: bool = False
    color_scheme: Literal["light", "dark", "no-preference"] | None = None
    # Per-profile CloakBrowser license key override. Empty/unset defers to
    # whatever CLOAKBROWSER_LICENSE_KEY the container has (or free tier if
    # the container has none either) — it does NOT force free tier when a
    # container-wide key is set. See binary_status.py / browser_manager.py.
    license_key: str | None = None
    # Ids (extensions.py's directory-name ids) of the extensions this profile
    # loads, from the set discovered at container startup. None (the
    # default — distinct from an explicit []) means "not specified": the API
    # layer fills it in with every currently-available extension id, so a
    # freshly created profile starts with every extension enabled rather
    # than none. See main.py's create_profile.
    enabled_extensions: list[str] | None = None
    # Seconds of no VNC-viewer/CDP activity before this profile is stopped
    # automatically (releasing its CloakBrowser license claim). None (the
    # default) defers to PROFILE_IDLE_TIMEOUT_SECONDS (60 min if unset); 0
    # disables idle timeout for this profile. See browser_manager.py's
    # reap_idle_profiles.
    idle_timeout_seconds: int | None = None
    launch_args: list[str] = Field(default_factory=list)
    notes: str | None = None
    tags: list[TagCreate] | None = None


class ProfileUpdate(BaseModel):
    name: str | None = None
    fingerprint_seed: int | None = None
    proxy: str | None = Field(default=None)
    timezone: str | None = Field(default=None)
    locale: str | None = Field(default=None)
    platform: Literal["windows", "macos", "linux"] | None = None
    user_agent: str | None = Field(default=None)
    screen_width: int | None = None
    screen_height: int | None = None
    gpu_vendor: str | None = Field(default=None)
    gpu_renderer: str | None = Field(default=None)
    hardware_concurrency: int | None = Field(default=None)
    humanize: bool | None = None
    human_preset: Literal["default", "careful"] | None = None
    headless: bool | None = None
    geoip: bool | None = None
    clipboard_sync: bool | None = None
    auto_launch: bool | None = None
    auto_restart: bool | None = None
    color_scheme: Literal["light", "dark", "no-preference"] | None = Field(default=None)
    license_key: str | None = Field(default=None)
    enabled_extensions: list[str] | None = None
    idle_timeout_seconds: int | None = Field(default=None)
    launch_args: list[str] | None = None
    notes: str | None = Field(default=None)
    tags: list[TagCreate] | None = None


class TagCreate(BaseModel):
    tag: str
    color: str | None = None  # hex color


class TagResponse(BaseModel):
    tag: str
    color: str | None = None


class ProfileResponse(BaseModel):
    id: str
    name: str
    fingerprint_seed: int
    proxy: str | None = None
    timezone: str | None = None
    locale: str | None = None
    platform: str = "windows"
    user_agent: str | None = None
    screen_width: int = 1920
    screen_height: int = 1080
    gpu_vendor: str | None = None
    gpu_renderer: str | None = None
    hardware_concurrency: int | None = None
    humanize: bool = False
    human_preset: str = "default"
    headless: bool = False
    geoip: bool = False
    clipboard_sync: bool = True
    auto_launch: bool = False
    auto_restart: bool = False
    # True when this profile's crash-restart budget is currently exhausted
    # (AUTO_RESTART_MAX_ATTEMPTS auto-restarts already used within
    # AUTO_RESTART_WINDOW_S) — see BrowserManager.auto_restart_budget_state.
    # Always False when auto_restart itself is off; there is no budget to
    # exhaust. A manual launch that comes up cleanly resets it immediately,
    # so this is never a permanent state short of the profile continuing to
    # crash on every attempt.
    auto_restart_exhausted: bool = False

    @field_validator("clipboard_sync", mode="before")
    @classmethod
    def coerce_clipboard_sync(cls, v: object) -> bool:
        return v if v is not None else True

    color_scheme: str | None = None
    license_key: str | None = None
    enabled_extensions: list[str] = []
    idle_timeout_seconds: int | None = None
    launch_args: list[str] = []
    notes: str | None = None
    user_data_dir: str
    created_at: str
    updated_at: str
    tags: list[TagResponse] = []
    status: ProfileLifecycleT = "stopped"
    vnc_ws_port: int | None = None
    cdp_url: str | None = None


class LaunchResponse(BaseModel):
    profile_id: str
    status: str = "running"
    # Null for a headless profile: it allocates no display and no Xvnc, so
    # there is nothing to report. These were non-nullable, which turned every
    # headless launch into a 500 ResponseValidationError AFTER the browser had
    # already started — the caller saw "Internal Server Error" for a launch
    # that in fact succeeded, and the next attempt then answered 409.
    vnc_ws_port: int | None = None
    display: str | None = None
    cdp_url: str | None = None


class StatusResponse(BaseModel):
    running_count: int
    binary_version: str
    profiles_total: int
    # CloakBrowser Chromium downloads on first launch (see binary_status.py);
    # these let the UI show a progress banner instead of a launch that just
    # looks hung. binary_download_state is "downloading" | "extracting" | None.
    binary_downloading: bool = False
    binary_download_percent: int | None = None
    binary_download_state: str | None = None
    # PROFILE_IDLE_TIMEOUT_SECONDS as actually resolved, so the profile form
    # can show what "leave blank" means instead of a hardcoded guess.
    default_idle_timeout_seconds: int = 3600


class ProfileStatusResponse(BaseModel):
    status: ProfileLifecycleT
    vnc_ws_port: int | None = None
    display: str | None = None
    cdp_url: str | None = None
    xvnc_alive: bool | None = None  # null when stopped
    browser_alive: bool | None = None  # null when stopped


class ViewerTokenResponse(BaseModel):
    token: str
    viewer_url: str
    expires_in: int
    # `kasmvnc_mode_preference` for the viewer URL, or None to let the client
    # choose. Only set when an NVENC codec is configured — the shipped client
    # cannot auto-select those. See vnc_manager.viewer_stream_mode_preference.
    stream_mode: str | None = None


class ExtensionResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    version: str | None = None


class ClipboardRequest(BaseModel):
    text: str = Field(max_length=1_048_576)  # 1MB max


class LoginRequest(BaseModel):
    token: str


class ExtensionInstallFromUrlRequest(BaseModel):
    # A chromewebstore.google.com/detail/<name>/<id> URL, or a bare 32-char
    # extension id — extensions.extract_extension_id() accepts either.
    url: str = Field(min_length=1, max_length=2048)


class ResourceUsageResponse(BaseModel):
    # None for all three when the profile's browser process is gone (a
    # crash between the caller's own liveness check and this one) — distinct
    # from 0, which would misreport a genuinely idle-but-alive profile.
    cpu_percent: float | None = None
    memory_mb: float | None = None
    process_count: int = 0
    # Seconds until browser_manager.reap_idle_profiles() auto-stops this
    # profile for inactivity, computed fresh from the same
    # idle_timeout_seconds/last_active fields that reaper itself checks.
    # None when idle timeout is disabled for this profile (<= 0), never
    # negative — clamped to 0 rather than going below it while the reaper's
    # own 5s sweep interval catches up.
    idle_remaining_seconds: int | None = None


class SystemCheckResponse(BaseModel):
    gpu_mode: Literal["swiftshader", "nvidia", "igpu"]
    binary_version: str
    license_configured: bool
    kasmvnc_version: str
    disk_total_bytes: int
    disk_used_bytes: int
    disk_free_bytes: int
    disk_percent_used: float


class TabInfo(BaseModel):
    # Position in context.pages() at the moment this response was built — the
    # id used to target the close endpoint. Not a stable identity: a tab
    # opening/closing between a list and a close call can shift it, same
    # trade-off downloads-by-filename already makes elsewhere in this API.
    index: int
    title: str
    url: str
    favicon: str | None = None


class ProfileTabsResponse(BaseModel):
    tabs: list[TabInfo]


class CookieWarmupStatusResponse(BaseModel):
    state: Literal["idle", "running", "done", "error", "cancelled"] = "idle"
    sites_total: int = 0
    sites_visited: int = 0
    current_site: str | None = None
    # Wall-clock seconds since /start was called and (while running) roughly
    # how much of the fixed WARMUP_DURATION_SECONDS budget is left — computed
    # from cookie_warmup.WarmupStatus's monotonic timestamps at response time
    # rather than stored on it, so the module itself stays clock-free.
    elapsed_seconds: float | None = None
    remaining_seconds: float | None = None
    error: str | None = None
