"""Pydantic models for profile CRUD operations."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .runtime import HostOS, RuntimeMode, ViewerMode


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
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
    allow_3p_cookies: bool = True
    set_google_default: bool = True
    capture_preview: bool = True
    restore_session: bool = True
    notes: str | None = None
    tags: list[TagCreate] | None = None


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
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
    allow_3p_cookies: bool | None = None
    set_google_default: bool | None = None
    capture_preview: bool | None = None
    restore_session: bool | None = None
    notes: str | None = Field(default=None)
    tags: list[TagCreate] | None = None

    @field_validator("gpu_family", mode="before")
    @classmethod
    def reject_null_gpu_family(cls, value: object) -> object:
        if value is None:
            raise ValueError("gpu_family cannot be null")
        return value


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
    binary_version: str
    license_tier: str = "keyless"
    profiles_total: int
    host_os: HostOS
    runtime_mode: RuntimeMode
    viewer_mode: ViewerMode
    windows_fonts_present: int | None = None
    windows_fonts_required: int | None = None
    windows_fonts_complete: bool | None = None
    proxy_auth_inline_supported: bool


class UpdateCheckResponse(BaseModel):
    current: str
    latest: str | None = None
    update_available: bool = False
    release_url: str | None = None


class SettingsResponse(BaseModel):
    license_key_set: bool
    license_key_masked: str | None = None
    release_channel: str = "stable"


class SettingsUpdate(BaseModel):
    # None = leave unchanged; "" = clear the license key (back to keyless).
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


class AutomationCookie(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=16_384)
    url: str | None = Field(default=None, max_length=2_048)
    domain: str | None = Field(default=None, max_length=255)
    path: str = Field(default="/", max_length=2_048)
    expires: float | None = None
    httpOnly: bool = False
    secure: bool = True
    sameSite: Literal["Strict", "Lax", "None"] | None = None


class AutomationBootstrapRequest(BaseModel):
    cookies: list[AutomationCookie] = Field(default_factory=list, max_length=100)
    allowed_origins: list[str] = Field(min_length=1, max_length=16)
    start_url: str = Field(max_length=2_048)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=4_096)
    totp_code: str | None = Field(default=None, pattern=r"^\d{6}$")


class AutomationFileInputRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4_096)


class ViewerGrantCreate(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    expires_in: int = Field(default=300, ge=30, le=14_400)


class ViewerGrantResponse(BaseModel):
    grant_id: str
    profile_id: str
    token: str
    expires_at: datetime


class ScopedProfileResponse(BaseModel):
    id: str
    name: str
    status: Literal["running", "stopped"]
    viewer_mode: ViewerMode
    clipboard_sync: bool = True


class LoginRequest(BaseModel):
    token: str
