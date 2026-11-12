"""Pydantic models for profile CRUD operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from .runtime import HostOS, RuntimeMode, ViewerMode
from .binary_cache import normalize_version

# Chromium Memory Saver: "off", or how eagerly background tabs are discarded
# (6 h / 4 h / 2 h hidden in Chromium 145).
MemorySaver = Literal["off", "conservative", "medium", "aggressive"]


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    license_key: str | None = None
    release_channel: Literal["stable", "preview"] = "stable"
    browser_version: str | None = None
    fingerprint_seed: int | None = None
    proxy: str | None = None
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
    extension_ids: list[str] = Field(default_factory=list)
    allow_3p_cookies: bool = True
    set_google_default: bool = True
    capture_preview: bool = True
    restore_session: bool = True
    memory_saver: MemorySaver = "off"
    transparent_proxy: bool = False
    portable_cookies: bool = False
    notes: str | None = None
    tags: list[TagCreate] | None = None
    @field_validator("license_key", mode="before")
    @classmethod
    def normalize_optional_license_key(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        return cleaned or None

    @field_validator("browser_version", mode="before")
    @classmethod
    def validate_browser_version(cls, value: object) -> object:
        return normalize_version(value)



class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    license_key: str | None = Field(default=None)
    release_channel: Literal["stable", "preview"] | None = None
    browser_version: str | None = Field(default=None)
    fingerprint_seed: int | None = None
    proxy: str | None = Field(default=None)
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
    extension_ids: list[str] | None = None
    allow_3p_cookies: bool | None = None
    set_google_default: bool | None = None
    capture_preview: bool | None = None
    restore_session: bool | None = None
    memory_saver: MemorySaver | None = None
    transparent_proxy: bool | None = None
    portable_cookies: bool | None = None
    notes: str | None = Field(default=None)
    tags: list[TagCreate] | None = None
    @field_validator("license_key", mode="before")
    @classmethod
    def normalize_optional_license_update(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        return cleaned or None

    @field_validator("browser_version", mode="before")
    @classmethod
    def validate_browser_version_update(cls, value: object) -> object:
        return normalize_version(value)

    @field_validator("release_channel", mode="before")
    @classmethod
    def reject_null_release_channel(cls, value: object) -> object:
        if value is None:
            raise ValueError("release_channel cannot be null")
        return value


    @field_validator("gpu_family", "memory_saver", mode="before")
    @classmethod
    def reject_null_choice(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null")
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
    license_key_set: bool = False
    license_key_masked: str | None = None
    release_channel: Literal["stable", "preview"] = "stable"
    browser_version: str | None = None
    fingerprint_seed: int
    proxy: str | None = None
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
    extension_ids: list[str] = Field(default_factory=list)
    allow_3p_cookies: bool = True
    set_google_default: bool = True
    capture_preview: bool = True
    restore_session: bool = True
    memory_saver: MemorySaver = "off"
    transparent_proxy: bool = False
    portable_cookies: bool = False
    notes: str | None = None
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
    installed_binary_count: int
    binary_cache_dir: str
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


class RuntimeConfigResponse(BaseModel):
    sidebar_width: str


class BrowserBinaryResponse(BaseModel):
    version: str
    tier: Literal["licensed", "keyless"]
    path: str
    size_bytes: int
    profile_count: int = 0
    running_count: int = 0
    in_use: bool = False


class BrowserBinaryListResponse(BaseModel):
    cache_dir: str
    binaries: list[BrowserBinaryResponse] = Field(default_factory=list)


class BrowserDownloadResponse(BaseModel):
    version: str | None = None
    tier: Literal["licensed", "keyless"]
    binary_path: str


class BrowserCleanupResponse(BaseModel):
    removed: list[BrowserBinaryResponse] = Field(default_factory=list)
    reclaimed_bytes: int = 0


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


class LoginRequest(BaseModel):
    token: str


class ExtensionResponse(BaseModel):
    """One entry of the global extension library."""

    id: str
    name: str
    description: str | None = None
    version: str
    previous_version: str | None = None
    manifest_version: int
    source_kind: Literal["webstore", "crx", "zip", "upload"]
    source_url: str | None = None
    webstore_id: str | None = None
    # "/api/extensions/{id}/icon" when the current version carries an icon.
    icon_url: str | None = None
    profile_count: int = 0
    created_at: str
    updated_at: str


class ExtensionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=2048)


class ExtensionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # None re-downloads the stored source (an upload has none: pick a file instead).
    source: str | None = Field(default=None, max_length=2048)


class ExtensionUpdateResponse(BaseModel):
    extension: ExtensionResponse
    updated: bool
