"""Pydantic models for profile CRUD operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .runtime import HostOS, RuntimeMode, ViewerMode


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    fingerprint_seed: int | None = None
    proxy: str | None = None
    # "none" = use the proxy field above; otherwise the proxy is fetched from
    # the provider on every launch. The provider key is a secret and is never
    # echoed back by the API; see proxy_provider_key_masked on the response.
    proxy_provider: Literal["none", "kiotproxy", "tmproxy"] = "none"
    proxy_provider_key: str | None = None
    kiotproxy_region: Literal["bac", "trung", "nam", "random"] = "random"
    tmproxy_location_id: int = 0
    tmproxy_isp_id: int = 0
    timezone: str | None = None
    locale: str | None = None
    screen_width: int = 1920
    screen_height: int = 1080
    gpu_family: Literal["auto", "nvidia", "intel"] = "auto"
    humanize: bool = False
    human_preset: Literal["default", "careful"] = "default"
    geoip: bool = True
    clipboard_sync: bool = True
    auto_launch: bool = False
    color_scheme: Literal["light", "dark", "no-preference"] | None = None
    launch_args: list[str] = Field(default_factory=list)
    extension_paths: list[str] = Field(default_factory=list)
    allow_3p_cookies: bool = True
    set_google_default: bool = True
    capture_preview: bool = True
    restore_session: bool = True
    transparent_proxy: bool = False
    portable_cookies: bool = False
    notes: str | None = None
    # Pin this profile to one pooled key (its id, never the key itself). None
    # lets the launch lease whichever pooled key is free.
    license_key_id: str | None = None
    tags: list[TagCreate] | None = None


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    fingerprint_seed: int | None = None
    proxy: str | None = Field(default=None)
    proxy_provider: Literal["none", "kiotproxy", "tmproxy"] | None = None
    # Omitted = unchanged; null/"" = clear the provider key.
    proxy_provider_key: str | None = Field(default=None)
    kiotproxy_region: Literal["bac", "trung", "nam", "random"] | None = None
    tmproxy_location_id: int | None = None
    tmproxy_isp_id: int | None = None
    timezone: str | None = Field(default=None)
    locale: str | None = Field(default=None)
    screen_width: int | None = None
    screen_height: int | None = None
    gpu_family: Literal["auto", "nvidia", "intel"] | None = None
    humanize: bool | None = None
    human_preset: Literal["default", "careful"] | None = None
    geoip: bool | None = None
    clipboard_sync: bool | None = None
    auto_launch: bool | None = None
    color_scheme: Literal["light", "dark", "no-preference"] | None = Field(default=None)
    launch_args: list[str] | None = None
    extension_paths: list[str] | None = None
    allow_3p_cookies: bool | None = None
    set_google_default: bool | None = None
    capture_preview: bool | None = None
    restore_session: bool | None = None
    transparent_proxy: bool | None = None
    portable_cookies: bool | None = None
    notes: str | None = Field(default=None)
    # Omitted = unchanged; null or "" = clear the pin (lease a free pooled key again).
    license_key_id: str | None = Field(default=None)
    tags: list[TagCreate] | None = None

    @field_validator("gpu_family", mode="before")
    @classmethod
    def reject_null_gpu_family(cls, value: object) -> object:
        if value is None:
            raise ValueError("gpu_family cannot be null")
        return value


class ArtifactResponse(BaseModel):
    id: str
    profile_id: str
    name: str
    size: int
    kind: str
    state: str
    content_type: str | None = None
    created_at: str
    # The name this file carries in the container's file-chooser view; stable for its lifetime.
    picker_name: str
    # Where the file is visible to the browser process, for CDP calls that take a path.
    container_path: str


class ProfileDuplicateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Also copy the source's user_data_dir (cookies, logged-in sessions, history,
    # local storage) so the clone launches as the same identity AND the same
    # session. Off by default: the clone starts with fresh, empty browser state.
    include_browser_state: bool = False


class TagCreate(BaseModel):
    tag: str
    color: str | None = None


class TagResponse(BaseModel):
    tag: str
    color: str | None = None


class ReorderRequest(BaseModel):
    ordered_ids: list[str]


class ProfileResponse(BaseModel):
    id: str
    name: str
    fingerprint_seed: int
    proxy: str | None = None
    proxy_provider: str = "none"
    proxy_provider_key_set: bool = False
    proxy_provider_key_masked: str | None = None
    kiotproxy_region: str = "random"
    tmproxy_location_id: int = 0
    tmproxy_isp_id: int = 0
    # Sanitised metadata of the proxy a running browser was launched with.
    active_proxy: dict | None = None
    timezone: str | None = None
    locale: str | None = None
    screen_width: int = 1920
    screen_height: int = 1080
    gpu_family: Literal["auto", "nvidia", "intel"] = "auto"
    humanize: bool = False
    human_preset: str = "default"
    geoip: bool = True
    clipboard_sync: bool = True
    auto_launch: bool = False

    @field_validator("clipboard_sync", mode="before")
    @classmethod
    def coerce_clipboard_sync(cls, v: object) -> bool:
        return True if v is None else bool(v)

    color_scheme: str | None = None
    launch_args: list[str] = Field(default_factory=list)
    extension_paths: list[str] = Field(default_factory=list)
    allow_3p_cookies: bool = True
    set_google_default: bool = True
    capture_preview: bool = True
    restore_session: bool = True
    transparent_proxy: bool = False
    portable_cookies: bool = False
    notes: str | None = None
    # The pooled key this profile is pinned to, if any. The key itself is never
    # returned; see GET /api/keys for the pool.
    license_key_id: str | None = None
    user_data_dir: str
    created_at: str
    updated_at: str
    sort_order: int = 0
    tags: list[TagResponse] = Field(default_factory=list)
    status: str = "stopped"
    runtime_mode: RuntimeMode = "docker"
    viewer_mode: ViewerMode = "vnc"
    vnc_ws_port: int | None = None
    cdp_url: str | None = None
    # Set when the profile's last launch closed on a license denial (out of
    # seats / bad key). {message, reason, upgrade_url?}. Cleared on next launch.
    last_error: dict[str, str] | None = None


class LaunchResponse(BaseModel):
    profile_id: str
    status: str = "running"
    runtime_mode: RuntimeMode
    viewer_mode: ViewerMode
    vnc_ws_port: int | None = None
    display: str | None = None
    cdp_url: str | None = None


class StatusResponse(BaseModel):
    running_count: int
    binary_version: str
    license_tier: str = "none"
    # Plan reported by the license server ("free", "solo", "team", ...). Purely
    # informational: the binary remains the authority on seat enforcement.
    license_plan: str | None = None
    profiles_total: int
    host_os: HostOS
    runtime_mode: RuntimeMode
    viewer_mode: ViewerMode
    windows_fonts_present: int | None = None
    windows_fonts_required: int | None = None
    windows_fonts_complete: bool | None = None


class UpdateCheckResponse(BaseModel):
    current: str
    latest: str | None = None
    update_available: bool = False
    release_url: str | None = None


class SettingsResponse(BaseModel):
    license_key_set: bool
    license_key_masked: str | None = None
    release_channel: str = "stable"
    # Size of the shared key pool (see /api/keys). Informational: a key in the
    # pool is used before the app-wide key.
    key_pool_size: int = 0


class LicenseKeyEntry(BaseModel):
    """One pooled key. The raw key never leaves the server."""

    id: str
    key_masked: str | None = None
    label: str | None = None
    state: str = "unknown"
    plan: str | None = None
    expires: str | None = None
    added_at: str
    checked_at: str | None = None
    # Profile currently holding this key, if any. A key is leased to exactly one
    # running browser, so a non-null value means it cannot be handed out again.
    leased_to: str | None = None
    # ISO UTC until which the license server still counts this key's abandoned
    # session. Null means the key may be leased right away.
    available_at: str | None = None


class LicenseKeyAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # One key per line. Blank lines and lines starting with '#' are ignored.
    keys: str = Field(min_length=1, max_length=100_000)
    label: str | None = None


class LicenseKeyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Empty/omitted = re-check every key in the pool.
    ids: list[str] = Field(default_factory=list)


class LicenseKeyCheckResult(BaseModel):
    id: str
    key_masked: str | None = None
    state: str
    plan: str | None = None
    expires: str | None = None


class SettingsUpdate(BaseModel):
    # None = leave unchanged; "" = clear the license key.
    license_key: str | None = None
    release_channel: str | None = None


class ProfileStatusResponse(BaseModel):
    status: str
    runtime_mode: RuntimeMode
    viewer_mode: ViewerMode
    vnc_ws_port: int | None = None
    display: str | None = None
    cdp_url: str | None = None


class ClipboardRequest(BaseModel):
    text: str = Field(max_length=1_048_576)


class ProxyTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proxy: str = Field(min_length=1, max_length=512)


class ProxyTestResponse(BaseModel):
    ok: bool
    ip: str | None = None
    country: str | None = None
    city: str | None = None
    timezone: str | None = None
    latency_ms: int | None = None
    error: str | None = None


class KiotProxyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=512)


class KiotProxyNewRequest(KiotProxyRequest):
    region: Literal["bac", "trung", "nam", "random"] = "random"


class TmProxyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=512)


class TmProxyNewRequest(TmProxyRequest):
    location_id: int = 0
    isp_id: int = 0


class ProxyProviderInfo(BaseModel):
    """Normalised proxy metadata. Credentials are omitted from this shape."""

    provider: str
    host: str | None = None
    real_ip: str | None = None
    location: str | None = None
    isp: str | None = None
    expires_at: int | str | None = None
    ttl: int | None = None
    ttc: int | None = None
    next_request_at: int | None = None


class TmProxyOption(BaseModel):
    id: int
    name: str | None = None


class LoginRequest(BaseModel):
    token: str
