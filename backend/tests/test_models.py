"""Tests for Pydantic models — validation, defaults, constraints."""

import pytest
from pydantic import ValidationError

from backend.models import (
    ClipboardRequest,
    LaunchResponse,
    ProfileCreate,
    ProfileResponse,
    ProfileStatusResponse,
    ProfileUpdate,
    StatusResponse,
    TagCreate,
    TagResponse,
)


# ── ProfileCreate ────────────────────────────────────────────────────────────


def test_profile_create_minimal():
    p = ProfileCreate(name="Test")
    assert p.name == "Test"
    assert p.fingerprint_seed is None
    assert p.screen_width == 1920
    assert p.screen_height == 1080
    assert p.gpu_family == "auto"
    assert p.humanize is False
    assert p.geoip is True
    assert p.human_preset == "default"
    assert p.extension_paths == []
    assert p.allow_3p_cookies is True


def test_profile_create_all_fields():
    p = ProfileCreate(
        name="Full",
        fingerprint_seed=42,
        proxy="http://host:8080",
        timezone="America/New_York",
        locale="en-US",
        screen_width=2560,
        screen_height=1440,
        gpu_family="nvidia",
        humanize=True,
        human_preset="careful",
        geoip=True,
        color_scheme="dark",
        extension_paths=["/tmp/extension"],
        allow_3p_cookies=True,
        notes="test note",
        tags=[TagCreate(tag="work", color="#ff0000")],
    )
    assert p.gpu_family == "nvidia"
    assert p.human_preset == "careful"
    assert p.color_scheme == "dark"
    assert len(p.tags) == 1


def test_profile_create_launch_args_default():
    p = ProfileCreate(name="Test")
    assert p.launch_args == []


def test_profile_create_with_launch_args():
    p = ProfileCreate(name="Test", launch_args=["--load-extension=/tmp/ext"])
    assert p.launch_args == ["--load-extension=/tmp/ext"]


def test_profile_update_launch_args():
    p = ProfileUpdate(launch_args=["--flag"])
    dumped = p.model_dump(exclude_unset=True)
    assert dumped == {"launch_args": ["--flag"]}


def test_profile_create_invalid_gpu_family():
    with pytest.raises(ValidationError):
        ProfileCreate(name="Bad", gpu_family="amd")


def test_profile_create_invalid_human_preset():
    with pytest.raises(ValidationError):
        ProfileCreate(name="Bad", human_preset="fast")


def test_profile_create_invalid_color_scheme():
    with pytest.raises(ValidationError):
        ProfileCreate(name="Bad", color_scheme="auto")


# ── ProfileUpdate ────────────────────────────────────────────────────────────


def test_profile_update_all_optional():
    p = ProfileUpdate()
    assert p.name is None
    assert p.gpu_family is None


def test_profile_update_exclude_unset():
    p = ProfileUpdate(name="New Name")
    dumped = p.model_dump(exclude_unset=True)
    assert dumped == {"name": "New Name"}


def test_profile_update_invalid_gpu_family():
    with pytest.raises(ValidationError):
        ProfileUpdate(gpu_family="amd")
    with pytest.raises(ValidationError):
        ProfileUpdate(gpu_family=None)


def test_removed_profile_fields_are_rejected():
    with pytest.raises(ValidationError):
        ProfileCreate(name="Legacy", user_agent="Legacy UA")


# ── TagCreate ────────────────────────────────────────────────────────────────


def test_tag_create_minimal():
    t = TagCreate(tag="work")
    assert t.tag == "work"
    assert t.color is None


def test_tag_create_with_color():
    t = TagCreate(tag="personal", color="#00ff00")
    assert t.color == "#00ff00"


# ── ClipboardRequest ─────────────────────────────────────────────────────────


def test_clipboard_request_valid():
    c = ClipboardRequest(text="hello world")
    assert c.text == "hello world"


def test_clipboard_request_max_length():
    with pytest.raises(ValidationError):
        ClipboardRequest(text="x" * 1_048_577)


def test_clipboard_request_at_limit():
    c = ClipboardRequest(text="x" * 1_048_576)
    assert len(c.text) == 1_048_576


# ── LaunchResponse ──────────────────────────────────────────────────────────


def test_launch_response_with_cdp_url():
    r = LaunchResponse(
        profile_id="abc", runtime_mode="docker", viewer_mode="vnc",
        vnc_ws_port=6100, display=":100",
        cdp_url="/api/profiles/abc/cdp",
    )
    assert r.cdp_url == "/api/profiles/abc/cdp"


def test_launch_response_cdp_url_default_none():
    r = LaunchResponse(
        profile_id="abc", runtime_mode="docker", viewer_mode="vnc",
        vnc_ws_port=6100, display=":100",
    )
    assert r.cdp_url is None


# ── ProfileStatusResponse ──────────────────────────────────────────────────


def test_profile_status_response_cdp_url():
    r = ProfileStatusResponse(
        status="running", runtime_mode="docker", viewer_mode="vnc",
        vnc_ws_port=6100, display=":100",
        cdp_url="/api/profiles/abc/cdp",
    )
    assert r.cdp_url == "/api/profiles/abc/cdp"


def test_profile_status_response_cdp_url_stopped():
    r = ProfileStatusResponse(
        status="stopped", runtime_mode="docker", viewer_mode="vnc"
    )
    assert r.cdp_url is None


# ── ProfileResponse ────────────────────────────────────────────────────────


def test_profile_response_cdp_url():
    r = ProfileResponse(
        id="abc", name="Test", fingerprint_seed=12345,
        user_data_dir="/data/profiles/abc",
        created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
        status="running", cdp_url="/api/profiles/abc/cdp",
    )
    assert r.cdp_url == "/api/profiles/abc/cdp"


def test_profile_response_cdp_url_default_none():
    r = ProfileResponse(
        id="abc", name="Test", fingerprint_seed=12345,
        user_data_dir="/data/profiles/abc",
        created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
    )
    assert r.cdp_url is None
