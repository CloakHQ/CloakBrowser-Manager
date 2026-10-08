"""Tests for FastAPI routes via TestClient."""

from __future__ import annotations

import asyncio
import json
import time
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

from backend import database as db
from backend import main
from backend.models import ProfileDuplicateRequest
from backend.browser_manager import BrowserManager, RunningProfile
from backend.runtime import RuntimeConfig


# ── Profile CRUD ─────────────────────────────────────────────────────────────


def test_list_profiles_empty(app_client: TestClient):
    resp = app_client.get("/api/profiles")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_profile(app_client: TestClient):
    resp = app_client.post("/api/profiles", json={"name": "Test"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test"
    assert data["status"] == "stopped"
    assert "id" in data
    assert len(data["id"]) == 36  # UUID


def test_create_profile_with_all_fields(app_client: TestClient):
    resp = app_client.post("/api/profiles", json={
        "name": "Full",
        "fingerprint_seed": 42,
        "proxy": "http://host:8080",
        "gpu_family": "nvidia",
        "screen_width": 2560,
        "screen_height": 1440,
        "humanize": True,
        "human_preset": "careful",
        "tags": [{"tag": "work", "color": "#ff0000"}],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["fingerprint_seed"] == 42
    assert data["gpu_family"] == "nvidia"
    assert len(data["tags"]) == 1


def test_create_profile_invalid_gpu_family(app_client: TestClient):
    resp = app_client.post("/api/profiles", json={"name": "Bad", "gpu_family": "amd"})
    assert resp.status_code == 422


def test_removed_profile_fields_and_null_gpu_family_are_rejected(app_client: TestClient):
    legacy = app_client.post("/api/profiles", json={"name": "Legacy", "headless": True})
    assert legacy.status_code == 422

    created = app_client.post("/api/profiles", json={"name": "GPU"})
    response = app_client.put(
        f"/api/profiles/{created.json()['id']}",
        json={"gpu_family": None},
    )
    assert response.status_code == 422


def test_get_profile(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "Get Me"})
    pid = create.json()["id"]
    resp = app_client.get(f"/api/profiles/{pid}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Get Me"


def test_get_profile_not_found(app_client: TestClient):
    resp = app_client.get("/api/profiles/nonexistent")
    assert resp.status_code == 404


def test_reorder_profiles(app_client: TestClient):
    a = app_client.post("/api/profiles", json={"name": "A"}).json()["id"]
    b = app_client.post("/api/profiles", json={"name": "B"}).json()["id"]
    c = app_client.post("/api/profiles", json={"name": "C"}).json()["id"]
    resp = app_client.post("/api/profiles/reorder", json={"ordered_ids": [a, b, c]})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    names = [p["name"] for p in app_client.get("/api/profiles").json()]
    assert names == ["A", "B", "C"]


def test_update_profile(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "Original"})
    pid = create.json()["id"]
    resp = app_client.put(f"/api/profiles/{pid}", json={"name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"


def test_update_profile_not_found(app_client: TestClient):
    resp = app_client.put("/api/profiles/nonexistent", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_profile(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "Delete Me"})
    pid = create.json()["id"]
    resp = app_client.delete(f"/api/profiles/{pid}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # Confirm gone
    assert app_client.get(f"/api/profiles/{pid}").status_code == 404


def test_delete_profile_not_found(app_client: TestClient):
    resp = app_client.delete("/api/profiles/nonexistent")
    assert resp.status_code == 404


def test_delete_profile_stops_running(app_client: TestClient):
    """Deleting a running profile should stop it first."""
    create = app_client.post("/api/profiles", json={"name": "Running"})
    pid = create.json()["id"]

    # Inject mock running profile
    mock_running = MagicMock(spec=RunningProfile)
    mock_running.display = 100
    mock_running.ws_port = 6100
    mock_running.cdp_port = 5100
    main.browser_mgr.running[pid] = mock_running
    main.browser_mgr.stop = AsyncMock(side_effect=lambda p: main.browser_mgr.running.pop(p, None))

    resp = app_client.delete(f"/api/profiles/{pid}")
    assert resp.status_code == 200
    main.browser_mgr.stop.assert_called_once_with(pid)


# ── Profile Status ───────────────────────────────────────────────────────────


def test_get_profile_status_stopped(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "Status"})
    pid = create.json()["id"]
    resp = app_client.get(f"/api/profiles/{pid}/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"


def test_get_profile_status_not_found(app_client: TestClient):
    resp = app_client.get("/api/profiles/nonexistent/status")
    assert resp.status_code == 404


def test_profile_response_reports_viewer_capability(app_client: TestClient):
    response = app_client.post("/api/profiles", json={"name": "Runtime"})
    assert response.status_code == 201
    assert response.json()["runtime_mode"] == "docker"
    assert response.json()["viewer_mode"] == "vnc"


# ── Launch / Stop ────────────────────────────────────────────────────────────


def test_launch_not_found(app_client: TestClient):
    resp = app_client.post("/api/profiles/nonexistent/launch")
    assert resp.status_code == 404


def test_launch_already_running(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "Running"})
    pid = create.json()["id"]
    # Inject into running dict
    main.browser_mgr.running[pid] = MagicMock(spec=RunningProfile)
    resp = app_client.post(f"/api/profiles/{pid}/launch")
    assert resp.status_code == 409
    # Cleanup
    main.browser_mgr.running.pop(pid, None)


def test_launch_invalid_proxy_400(app_client: TestClient):
    """ValueError from browser_mgr.launch should map to 400."""
    create = app_client.post("/api/profiles", json={"name": "BadProxy"})
    pid = create.json()["id"]
    main.browser_mgr.launch = AsyncMock(side_effect=ValueError("Invalid proxy scheme 'ftp'"))
    resp = app_client.post(f"/api/profiles/{pid}/launch")
    assert resp.status_code == 400
    assert "ftp" in resp.json()["detail"]


def test_launch_failure_500(app_client: TestClient):
    """Generic exception from browser_mgr.launch should map to 500."""
    create = app_client.post("/api/profiles", json={"name": "Crash"})
    pid = create.json()["id"]
    main.browser_mgr.launch = AsyncMock(side_effect=RuntimeError("Xvnc failed"))
    resp = app_client.post(f"/api/profiles/{pid}/launch")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to launch browser"


def test_native_launch_has_no_vnc_display(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    create = app_client.post("/api/profiles", json={"name": "Native"})
    pid = create.json()["id"]
    monkeypatch.setattr(
        main.browser_mgr,
        "runtime",
        RuntimeConfig("windows", "native", "native-window", Path("C:/data")),
    )
    running = RunningProfile(pid, MagicMock(), 53123)
    monkeypatch.setattr(main.browser_mgr, "launch", AsyncMock(return_value=running))

    response = app_client.post(f"/api/profiles/{pid}/launch")

    assert response.status_code == 200
    assert response.json()["viewer_mode"] == "native-window"
    assert response.json()["vnc_ws_port"] is None
    assert response.json()["display"] is None


def test_stop_not_running(app_client: TestClient):
    resp = app_client.post("/api/profiles/nonexistent/stop")
    assert resp.status_code == 404


# ── System Status ────────────────────────────────────────────────────────────


def test_system_status(app_client: TestClient):
    # Clear any leaked running profiles from prior tests
    main.browser_mgr.running.clear()

    # Create a profile so profiles_total > 0
    app_client.post("/api/profiles", json={"name": "Status Test"})
    resp = app_client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running_count"] == 0
    assert data["binary_version"] == "0.0.0-test"
    assert data["profiles_total"] >= 1


# ── Launch Args ─────────────────────────────────────────────────────────────


def test_profile_launch_args_default_empty(app_client: TestClient):
    resp = app_client.post("/api/profiles", json={"name": "NoArgs"})
    assert resp.status_code == 201
    assert resp.json()["launch_args"] == []


def test_profile_launch_args_create(app_client: TestClient):
    resp = app_client.post("/api/profiles", json={
        "name": "WithArgs",
        "launch_args": ["--load-extension=/data/ext", "--disable-features=Foo"],
    })
    assert resp.status_code == 201
    assert resp.json()["launch_args"] == ["--load-extension=/data/ext", "--disable-features=Foo"]


def test_profile_launch_args_update(app_client: TestClient):
    resp = app_client.post("/api/profiles", json={"name": "UpdateArgs"})
    pid = resp.json()["id"]
    resp = app_client.put(f"/api/profiles/{pid}", json={"launch_args": ["--new-flag"]})
    assert resp.status_code == 200
    assert resp.json()["launch_args"] == ["--new-flag"]


def test_profile_launch_args_get(app_client: TestClient):
    resp = app_client.post("/api/profiles", json={
        "name": "GetArgs",
        "launch_args": ["--flag"],
    })
    pid = resp.json()["id"]
    resp = app_client.get(f"/api/profiles/{pid}")
    assert resp.json()["launch_args"] == ["--flag"]


def test_profile_extensions_and_3p_cookie_setting(app_client: TestClient):
    resp = app_client.post("/api/profiles", json={
        "name": "Compatibility",
        "extension_paths": ["/data/extensions/one"],
        "allow_3p_cookies": True,
    })
    assert resp.status_code == 201
    assert resp.json()["extension_paths"] == ["/data/extensions/one"]
    assert resp.json()["allow_3p_cookies"] is True


# ── Clipboard Sync Setting ──────────────────────────────────────────────────


def test_profile_clipboard_sync_default_true(app_client: TestClient):
    """New profiles should have clipboard_sync=true by default."""
    resp = app_client.post("/api/profiles", json={"name": "Clipboard Test"})
    assert resp.status_code == 201
    assert resp.json()["clipboard_sync"] is True


def test_profile_clipboard_sync_update(app_client: TestClient):
    """clipboard_sync can be toggled per profile."""
    resp = app_client.post("/api/profiles", json={"name": "Clipboard Toggle"})
    pid = resp.json()["id"]
    resp = app_client.put(f"/api/profiles/{pid}", json={"clipboard_sync": False})
    assert resp.status_code == 200
    assert resp.json()["clipboard_sync"] is False
    resp = app_client.put(f"/api/profiles/{pid}", json={"clipboard_sync": True})
    assert resp.json()["clipboard_sync"] is True


# ── Clipboard ────────────────────────────────────────────────────────────────


def test_set_clipboard_not_running(app_client: TestClient):
    resp = app_client.post("/api/profiles/nonexistent/clipboard", json={"text": "hello"})
    assert resp.status_code == 404


def test_get_clipboard_not_running(app_client: TestClient):
    resp = app_client.get("/api/profiles/nonexistent/clipboard")
    assert resp.status_code == 404


def test_set_clipboard_success(app_client: TestClient):
    """Mock a running profile and patch xclip subprocess."""
    create = app_client.post("/api/profiles", json={"name": "Clip"})
    pid = create.json()["id"]

    # Inject mock running profile
    mock_running = MagicMock(spec=RunningProfile)
    mock_running.display = 100
    mock_running.cdp_port = 5100
    main.browser_mgr.running[pid] = mock_running

    # Mock asyncio.create_subprocess_exec to avoid actual xclip
    mock_proc = AsyncMock()
    mock_proc.returncode = None
    mock_proc.stdin = MagicMock()
    mock_proc.stdin.write = MagicMock()
    mock_proc.stdin.drain = AsyncMock()
    mock_proc.stdin.close = MagicMock()

    with patch("backend.main.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        resp = app_client.post(f"/api/profiles/{pid}/clipboard", json={"text": "test clipboard"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # Cleanup
    main.browser_mgr.running.pop(pid, None)


def test_get_clipboard_from_page(app_client: TestClient):
    """Mock running profile with a page that has clipboard text."""
    create = app_client.post("/api/profiles", json={"name": "ClipRead"})
    pid = create.json()["id"]

    # Mock page with clipboard text
    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(return_value="copied text")

    mock_context = MagicMock()
    mock_context.pages = [mock_page]

    mock_running = MagicMock(spec=RunningProfile)
    mock_running.display = 100
    mock_running.cdp_port = 5100
    mock_running.context = mock_context
    main.browser_mgr.running[pid] = mock_running

    resp = app_client.get(f"/api/profiles/{pid}/clipboard")
    assert resp.status_code == 200
    assert resp.json()["text"] == "copied text"

    # Cleanup
    main.browser_mgr.running.pop(pid, None)


# ── Response shape ───────────────────────────────────────────────────────────


def test_profile_response_has_status_field(app_client: TestClient):
    app_client.post("/api/profiles", json={"name": "Shape"})
    resp = app_client.get("/api/profiles")
    for profile in resp.json():
        assert "status" in profile
        assert profile["status"] in ("running", "stopped")


def test_profile_response_has_cdp_url_field(app_client: TestClient):
    """Stopped profiles should have cdp_url=null."""
    app_client.post("/api/profiles", json={"name": "CdpShape"})
    resp = app_client.get("/api/profiles")
    for profile in resp.json():
        assert "cdp_url" in profile
        if profile["status"] == "stopped":
            assert profile["cdp_url"] is None


def test_status_stopped_has_cdp_url_null(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "CdpStatus"})
    pid = create.json()["id"]
    resp = app_client.get(f"/api/profiles/{pid}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cdp_url"] is None


def test_running_profile_has_cdp_url(app_client: TestClient):
    """Running profile should have a cdp_url in list/get responses."""
    create = app_client.post("/api/profiles", json={"name": "CdpRunning"})
    pid = create.json()["id"]

    mock_running = MagicMock(spec=RunningProfile)
    mock_running.display = 100
    mock_running.ws_port = 6100
    mock_running.cdp_port = 5100
    mock_running.profile_id = pid
    main.browser_mgr.running[pid] = mock_running

    resp = app_client.get(f"/api/profiles/{pid}")
    data = resp.json()
    assert data["status"] == "running"
    assert data["cdp_url"] == f"/api/profiles/{pid}/cdp"

    # Cleanup
    main.browser_mgr.running.pop(pid, None)


# ── CDP Proxy ───────────────────────────────────────────────────────────────


def test_cdp_json_version_not_running(app_client: TestClient):
    resp = app_client.get("/api/profiles/nonexistent/cdp/json/version")
    assert resp.status_code == 404


def test_cdp_json_list_not_running(app_client: TestClient):
    resp = app_client.get("/api/profiles/nonexistent/cdp/json/list")
    assert resp.status_code == 404


def _mock_running_profile(pid: str) -> MagicMock:
    """Create a mock RunningProfile and register it in browser_mgr."""
    mock = MagicMock(spec=RunningProfile)
    mock.display = 100
    mock.ws_port = 6100
    mock.cdp_port = 5100
    mock.profile_id = pid
    main.browser_mgr.running[pid] = mock
    return mock


def test_cdp_json_version_rewrites_ws_url(app_client: TestClient):
    """GET /cdp/json/version rewrites webSocketDebuggerUrl through our proxy."""
    create = app_client.post("/api/profiles", json={"name": "CdpVer"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    chrome_response = MagicMock()
    chrome_response.json.return_value = {
        "webSocketDebuggerUrl": "ws://127.0.0.1:5100/devtools/browser/abc-123",
        "Browser": "Chrome/145.0.0.0",
    }
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=chrome_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = app_client.get(f"/api/profiles/{pid}/cdp/json/version")

    assert resp.status_code == 200
    data = resp.json()
    assert data["webSocketDebuggerUrl"] == f"ws://testserver/api/profiles/{pid}/cdp"
    assert data["Browser"] == "Chrome/145.0.0.0"
    main.browser_mgr.running.pop(pid, None)


def test_cdp_json_version_uses_wss_behind_https(app_client: TestClient):
    """X-Forwarded-Proto: https should produce wss:// URLs."""
    create = app_client.post("/api/profiles", json={"name": "CdpWss"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    chrome_response = MagicMock()
    chrome_response.json.return_value = {
        "webSocketDebuggerUrl": "ws://127.0.0.1:5100/devtools/browser/abc",
    }
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=chrome_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = app_client.get(
            f"/api/profiles/{pid}/cdp/json/version",
            headers={"X-Forwarded-Proto": "https"},
        )

    assert resp.status_code == 200
    assert resp.json()["webSocketDebuggerUrl"].startswith("wss://")
    main.browser_mgr.running.pop(pid, None)


def test_cdp_json_list_rewrites_page_urls(app_client: TestClient):
    """GET /cdp/json/list rewrites per-page webSocketDebuggerUrl."""
    create = app_client.post("/api/profiles", json={"name": "CdpList"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    chrome_response = MagicMock()
    chrome_response.json.return_value = [
        {
            "id": "page1",
            "webSocketDebuggerUrl": "ws://127.0.0.1:5100/devtools/page/DEADBEEF",
        },
        {
            "id": "page2",
            "title": "No WS URL",
        },
    ]
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=chrome_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = app_client.get(f"/api/profiles/{pid}/cdp/json/list")

    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["webSocketDebuggerUrl"] == (
        f"ws://testserver/api/profiles/{pid}/cdp/devtools/page/DEADBEEF"
    )
    assert "webSocketDebuggerUrl" not in data[1]
    main.browser_mgr.running.pop(pid, None)


def test_cdp_json_version_chrome_unreachable(app_client: TestClient):
    """502 when Chrome CDP endpoint is down."""
    create = app_client.post("/api/profiles", json={"name": "CdpDown"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=ConnectionError("refused"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = app_client.get(f"/api/profiles/{pid}/cdp/json/version")

    assert resp.status_code == 502
    main.browser_mgr.running.pop(pid, None)


# ── WebSocket Origin Validation ──────────────────────────────────────────────


def test_vnc_ws_rejects_cross_origin(app_client: TestClient):
    """VNC WebSocket should reject cross-origin browser connections."""
    create = app_client.post("/api/profiles", json={"name": "OriginVnc"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    with pytest.raises(Exception):
        with app_client.websocket_connect(
            f"/api/profiles/{pid}/vnc",
            headers={"origin": "http://evil.com"},
        ):
            pass
    main.browser_mgr.running.pop(pid, None)


def test_cdp_ws_rejects_cross_origin(app_client: TestClient):
    """CDP WebSocket should reject cross-origin browser connections."""
    create = app_client.post("/api/profiles", json={"name": "OriginCdp"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    with pytest.raises(Exception):
        with app_client.websocket_connect(
            f"/api/profiles/{pid}/cdp",
            headers={"origin": "http://evil.com"},
        ):
            pass
    main.browser_mgr.running.pop(pid, None)


def test_ws_allows_same_origin(app_client: TestClient):
    """WebSocket from same origin should pass Origin check (not get 4403)."""
    create = app_client.post("/api/profiles", json={"name": "OriginOk"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    # Same-origin passes Origin check. VNC proxy then fails to connect to
    # real KasmVNC (not running), but that's fine — we're testing Origin only.
    # The connection is accepted (no 4403), then closes due to VNC connect error.
    try:
        with app_client.websocket_connect(
            f"/api/profiles/{pid}/vnc",
            headers={"origin": "http://testserver"},
        ) as ws:
            pass  # connection accepted = Origin check passed
    except Exception as exc:
        # Any error other than 4403 means Origin check passed
        assert "4403" not in str(exc)
    main.browser_mgr.running.pop(pid, None)


def test_ws_allows_no_origin(app_client: TestClient):
    """WebSocket without Origin header (Playwright/Puppeteer) should be accepted."""
    create = app_client.post("/api/profiles", json={"name": "NoOrigin"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    try:
        with app_client.websocket_connect(f"/api/profiles/{pid}/vnc") as ws:
            pass
    except Exception as exc:
        assert "4403" not in str(exc)
    main.browser_mgr.running.pop(pid, None)


# ── Shutdown CSRF Guard ──────────────────────────────────────────────────────


def _fake_request(headers: dict[str, str]):
    """Build a minimal Starlette Request carrying the given headers."""
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "headers": raw})


def test_shutdown_rejects_cross_origin(app_client: TestClient):
    """A cross-origin POST /api/shutdown must be refused (403), never quit."""
    resp = app_client.post("/api/shutdown", headers={"origin": "http://evil.com"})
    assert resp.status_code == 403


def test_shutdown_rejects_cross_port(app_client: TestClient):
    """Same host, different port is still cross-origin (403)."""
    resp = app_client.post(
        "/api/shutdown", headers={"origin": "http://testserver:9999"}
    )
    assert resp.status_code == 403


def test_same_origin_request_matches_host():
    assert _same_origin_request_allows(
        {"origin": "http://localhost:8080", "host": "localhost:8080"}
    )


def test_same_origin_request_rejects_mismatched_host():
    assert not _same_origin_request_allows(
        {"origin": "http://evil.com", "host": "localhost:8080"}
    )


def test_same_origin_request_rejects_mismatched_port():
    assert not _same_origin_request_allows(
        {"origin": "http://localhost:9999", "host": "localhost:8080"}
    )


def test_same_origin_request_allows_no_origin():
    """Non-browser clients (curl, Playwright) send no Origin — allowed."""
    assert _same_origin_request_allows({"host": "localhost:8080"})


def test_same_origin_request_normalizes_default_ports():
    """origin :443 vs bare host on https, and :80 on http, both match."""
    assert _same_origin_request_allows(
        {"origin": "https://app.example:443", "host": "app.example"}
    )
    assert _same_origin_request_allows(
        {"origin": "http://app.example", "host": "app.example:80"}
    )


def _same_origin_request_allows(headers: dict[str, str]) -> bool:
    return main._same_origin_request(_fake_request(headers))


# ── Settings ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def settings_env(tmp_db: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate settings.json into the temp dir and neutralise the binary
    re-resolve (which would validate/download a real Pro build)."""
    from backend import settings_store

    path = tmp_db / "settings.json"
    monkeypatch.setattr(settings_store, "_settings_path", lambda: path)
    monkeypatch.setattr(main.browser_mgr, "resolve_binary_status", MagicMock())
    # Known baseline so masking/clearing is deterministic.
    monkeypatch.setattr(main.browser_mgr, "license_key", None)
    monkeypatch.setattr(main.browser_mgr, "release_channel", "stable")
    return path


def test_get_settings_no_key(app_client: TestClient, settings_env: Path):
    resp = app_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["license_key_set"] is False
    assert data["license_key_masked"] is None
    assert data["release_channel"] == "stable"


def test_get_settings_masks_key(app_client: TestClient, settings_env: Path):
    main.browser_mgr.license_key = "cb_ba5f52e422b142a68bc3088f5cbb63aa"
    resp = app_client.get("/api/settings")
    data = resp.json()
    assert data["license_key_set"] is True
    masked = data["license_key_masked"]
    # Recognisable but not the whole key.
    assert masked == "cb_ba…63aa"
    assert "b142a68bc3088" not in masked


def test_put_settings_persists_and_hot_applies(
    app_client: TestClient, settings_env: Path
):
    key = "cb_ba5f52e422b142a68bc3088f5cbb63aa"
    resp = app_client.put("/api/settings", json={"license_key": key})
    assert resp.status_code == 200
    # Hot-applied to the live browser manager.
    assert main.browser_mgr.license_key == key
    main.browser_mgr.resolve_binary_status.assert_called_once()
    # Persisted to settings.json.
    import json

    assert json.loads(settings_env.read_text())["license_key"] == key


def test_put_settings_clears_key(app_client: TestClient, settings_env: Path):
    key = "cb_ba5f52e422b142a68bc3088f5cbb63aa"
    main.browser_mgr.license_key = key
    settings_env.write_text('{"license_key": "%s"}' % key)

    resp = app_client.put("/api/settings", json={"license_key": ""})
    assert resp.status_code == 200
    assert main.browser_mgr.license_key is None
    import json

    assert "license_key" not in json.loads(settings_env.read_text())


def test_put_settings_sets_channel(app_client: TestClient, settings_env: Path):
    resp = app_client.put("/api/settings", json={"release_channel": "preview"})
    assert resp.status_code == 200
    assert main.browser_mgr.release_channel == "preview"
    import json

    assert json.loads(settings_env.read_text())["release_channel"] == "preview"


def test_put_settings_rejects_bad_channel(
    app_client: TestClient, settings_env: Path
):
    resp = app_client.put("/api/settings", json={"release_channel": "nightly"})
    assert resp.status_code == 400
    # Nothing persisted / applied on rejection.
    assert main.browser_mgr.release_channel == "stable"
    assert not settings_env.exists()


# ── settings_store + runtime units ───────────────────────────────────────────


def test_settings_store_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from backend import settings_store

    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_store, "_settings_path", lambda: path)
    assert settings_store.load_settings() == {}  # missing file → empty
    settings_store.save_settings({"license_key": "cb_x", "release_channel": "preview"})
    assert settings_store.load_settings() == {
        "license_key": "cb_x",
        "release_channel": "preview",
    }


def test_settings_store_ignores_corrupt_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from backend import settings_store

    path = tmp_path / "settings.json"
    path.write_text("{not valid json")
    monkeypatch.setattr(settings_store, "_settings_path", lambda: path)
    assert settings_store.load_settings() == {}


def test_bundle_dir_is_repo_root_when_unfrozen():
    from backend import runtime

    root = runtime.bundle_dir()
    # Running from source: repo root holds the backend package + app_entry.py.
    assert (root / "backend").is_dir()
    assert (root / "app_entry.py").exists()


# ── Profile Reset ────────────────────────────────────────────────────────────


def test_reset_profile_not_found(app_client: TestClient):
    resp = app_client.post("/api/profiles/nonexistent/reset")
    assert resp.status_code == 404


def test_reset_profile_returns_updated_profile(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "ResetTest", "fingerprint_seed": 99999})
    pid = create.json()["id"]
    resp = app_client.post(f"/api/profiles/{pid}/reset")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "ResetTest"
    assert data["fingerprint_seed"] != 99999
    assert data["status"] == "stopped"


def test_reset_profile_stops_running(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "ResetRunning"})
    pid = create.json()["id"]
    mock_running = MagicMock(spec=RunningProfile)
    mock_running.display = 100
    mock_running.ws_port = 6100
    mock_running.cdp_port = 5100
    main.browser_mgr.running[pid] = mock_running
    main.browser_mgr.stop = AsyncMock(side_effect=lambda p: main.browser_mgr.running.pop(p, None))
    resp = app_client.post(f"/api/profiles/{pid}/reset")
    assert resp.status_code == 200
    main.browser_mgr.stop.assert_called_once_with(pid)
    main.browser_mgr.running.pop(pid, None)


def test_reset_profile_wipes_state_but_preserves_config_files(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "ResetWipe"})
    pid = create.json()["id"]
    default_dir = Path(app_client.get(f"/api/profiles/{pid}").json()["user_data_dir"]) / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "Cookies").write_text("c")
    (default_dir / "History").write_text("h")
    (default_dir / "Web Data").write_text("w")
    (default_dir / "Secure Preferences").write_text("s")
    (default_dir / "Bookmarks").write_text("{}")
    (default_dir / "Preferences").write_text("{}")
    cache = default_dir / "Cache"
    cache.mkdir()
    (cache / "data").write_text("x")

    resp = app_client.post(f"/api/profiles/{pid}/reset")
    assert resp.status_code == 200
    # Wiped: identity/session state
    assert not (default_dir / "Cookies").exists()
    assert not (default_dir / "History").exists()
    assert not cache.exists()
    # Preserved: config + default search engine (Web Data holds the keyword row,
    # Secure Preferences the default pointer) + bookmarks
    assert (default_dir / "Web Data").exists()
    assert (default_dir / "Secure Preferences").exists()
    assert (default_dir / "Bookmarks").exists()
    assert (default_dir / "Preferences").exists()


def test_reset_profile_preserves_search_engine_marker(app_client: TestClient):
    """Reset keeps the search config, so the one-time setup marker must survive
    (no fragile Google rebuild is triggered on the next launch)."""
    from backend.browser_manager import SEARCH_ENGINE_MARKER

    create = app_client.post("/api/profiles", json={"name": "ResetMarker"})
    pid = create.json()["id"]
    user_data_dir = Path(app_client.get(f"/api/profiles/{pid}").json()["user_data_dir"])
    (user_data_dir / "Default").mkdir(parents=True, exist_ok=True)
    marker = user_data_dir / SEARCH_ENGINE_MARKER
    marker.write_text("google\n")

    resp = app_client.post(f"/api/profiles/{pid}/reset")
    assert resp.status_code == 200
    assert marker.exists()
    assert marker.read_text().strip() == "google"


# ── Duplicate ────────────────────────────────────────────────────────────────


def _seed_browser_state(user_data_dir: Path) -> None:
    """Lay down what a launched-then-stopped profile leaves on disk, lock included."""
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True)
    (user_data_dir / "Local State").write_text("{}")
    (default_dir / "Cookies").write_text("session=abc")
    (default_dir / "Preferences").write_text("{}")
    (default_dir / "Local Storage").mkdir()
    (default_dir / "Local Storage" / "leveldb").write_text("kv")
    (user_data_dir / "last_screenshot.jpg").write_bytes(b"jpg")
    (user_data_dir / "SingletonCookie").write_text("1")
    try:
        # Chromium's real lock is a dangling symlink to "<host>-<pid>".
        os.symlink("host-12345", user_data_dir / "SingletonLock")
    except OSError:  # symlink creation needs a privilege on some Windows hosts
        (user_data_dir / "SingletonLock").write_text("host-12345")


def test_duplicate_profile_not_found(app_client: TestClient):
    resp = app_client.post("/api/profiles/nonexistent/duplicate")
    assert resp.status_code == 404


def test_duplicate_profile_is_config_only_by_default(app_client: TestClient):
    create = app_client.post(
        "/api/profiles", json={"name": "Src", "fingerprint_seed": 4242, "proxy": "http://host:8080"}
    )
    src = create.json()
    _seed_browser_state(Path(src["user_data_dir"]))

    resp = app_client.post(f"/api/profiles/{src['id']}/duplicate")
    assert resp.status_code == 201
    clone = resp.json()
    assert clone["id"] != src["id"]
    assert clone["name"] == "Src (copy)"
    assert clone["fingerprint_seed"] == 4242
    assert clone["proxy"] == "http://host:8080"
    assert clone["user_data_dir"] != src["user_data_dir"]
    # No browser state travels with a config-only clone
    assert not Path(clone["user_data_dir"]).exists()


def test_duplicate_profile_with_browser_state_copies_session(app_client: TestClient):
    src = app_client.post("/api/profiles", json={"name": "Src", "fingerprint_seed": 4242}).json()
    src_dir = Path(src["user_data_dir"])
    _seed_browser_state(src_dir)

    resp = app_client.post(f"/api/profiles/{src['id']}/duplicate", json={"include_browser_state": True})
    assert resp.status_code == 201
    clone = resp.json()
    clone_dir = Path(clone["user_data_dir"])
    assert clone_dir != src_dir
    assert clone["fingerprint_seed"] == 4242
    # Session state travels with the copy
    assert (clone_dir / "Local State").read_text() == "{}"
    assert (clone_dir / "Default" / "Cookies").read_text() == "session=abc"
    assert (clone_dir / "Default" / "Preferences").read_text() == "{}"
    assert (clone_dir / "Default" / "Local Storage" / "leveldb").read_text() == "kv"
    # Chromium's single-instance lock and the source's preview frame do not
    for name in ("SingletonLock", "SingletonCookie", "last_screenshot.jpg"):
        assert not os.path.lexists(clone_dir / name), name
    # The source is left exactly as it was
    assert (src_dir / "Default" / "Cookies").read_text() == "session=abc"
    assert os.path.lexists(src_dir / "SingletonLock")


def test_duplicate_profile_with_browser_state_when_never_launched(app_client: TestClient):
    """A source with no user_data_dir yet is fine: the clone simply starts empty."""
    src = app_client.post("/api/profiles", json={"name": "Fresh"}).json()
    resp = app_client.post(f"/api/profiles/{src['id']}/duplicate", json={"include_browser_state": True})
    assert resp.status_code == 201
    assert not Path(resp.json()["user_data_dir"]).exists()


def test_duplicate_profile_with_browser_state_rejects_running_source(app_client: TestClient):
    src = app_client.post("/api/profiles", json={"name": "Live"}).json()
    pid = src["id"]
    main.browser_mgr.running[pid] = MagicMock(spec=RunningProfile)
    try:
        resp = app_client.post(f"/api/profiles/{pid}/duplicate", json={"include_browser_state": True})
    finally:
        main.browser_mgr.running.pop(pid, None)
    assert resp.status_code == 409
    # Nothing was created
    assert [p["id"] for p in app_client.get("/api/profiles").json()] == [pid]


def test_duplicate_profile_config_only_is_allowed_while_running(app_client: TestClient):
    src = app_client.post("/api/profiles", json={"name": "Live"}).json()
    pid = src["id"]
    main.browser_mgr.running[pid] = MagicMock(spec=RunningProfile)
    try:
        resp = app_client.post(f"/api/profiles/{pid}/duplicate")
    finally:
        main.browser_mgr.running.pop(pid, None)
    assert resp.status_code == 201


def test_duplicate_profile_leaves_nothing_behind_when_copy_fails(
    app_client: TestClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    src = app_client.post("/api/profiles", json={"name": "Src"}).json()
    _seed_browser_state(Path(src["user_data_dir"]))
    monkeypatch.setattr(main.shutil, "copytree", MagicMock(side_effect=OSError("disk full")))

    resp = app_client.post(f"/api/profiles/{src['id']}/duplicate", json={"include_browser_state": True})
    assert resp.status_code == 500
    assert "disk full" in resp.json()["detail"]
    # No clone row was ever written, and the clone directory does not linger
    assert [p["id"] for p in app_client.get("/api/profiles").json()] == [src["id"]]
    assert [d.name for d in (tmp_db / "profiles").iterdir()] == [src["id"]]
    # The source is untouched and can still be launched (the hold was released)
    assert not main.browser_mgr._held


def test_duplicate_profile_rejects_unknown_fields(app_client: TestClient):
    src = app_client.post("/api/profiles", json={"name": "Src"}).json()
    resp = app_client.post(f"/api/profiles/{src['id']}/duplicate", json={"copy_state": True})
    assert resp.status_code == 422


# ── Duplicate: lifecycle safety ──────────────────────────────────────────────
# These exercise the route coroutines directly so a copy can be paused mid-way
# and other requests interleaved with it.

_WITH_STATE = ProfileDuplicateRequest(include_browser_state=True)


def _seeded_source() -> dict:
    src = db.create_profile(name="Src", fingerprint_seed=4242)
    _seed_browser_state(Path(src["user_data_dir"]))
    return src


class _PausableCopy:
    """Monkeypatch target for main._copy_browser_state: blocks until released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()
        self.dst: Path | None = None
        self.fail_with: BaseException | None = None  # raise this instead of copying
        self._real = main._copy_browser_state

    def __call__(self, src: Path, dst: Path) -> None:
        self.dst = dst
        self.started.set()
        try:
            assert self.release.wait(5), "copy was never released"
            if self.fail_with is not None:
                raise self.fail_with
            self._real(src, dst)
        finally:
            self.done.set()


@pytest.fixture()
def real_lifecycle(monkeypatch: pytest.MonkeyPatch):
    """Earlier tests swap browser_mgr.launch/stop for AsyncMocks and never put them
    back; these tests need the real state machine (launching/stopping sets)."""
    mgr = main.browser_mgr
    monkeypatch.setattr(mgr, "launch", BrowserManager.launch.__get__(mgr))
    monkeypatch.setattr(mgr, "stop", BrowserManager.stop.__get__(mgr))
    return mgr


async def _status_of(coro) -> int:
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code
    return 200


async def test_duplicate_state_refuses_a_launching_source(tmp_db: Path):
    """launch() registers in `running` only at the end; the gap must count as active."""
    src = _seeded_source()
    main.browser_mgr._launching.add(src["id"])
    try:
        assert await _status_of(main.duplicate_profile(src["id"], _WITH_STATE)) == 409
    finally:
        main.browser_mgr._launching.discard(src["id"])
    assert [p["id"] for p in db.list_profiles()] == [src["id"]]
    assert [d.name for d in (tmp_db / "profiles").iterdir()] == [src["id"]]


async def test_duplicate_state_refuses_a_source_still_closing(tmp_db: Path, real_lifecycle, monkeypatch: pytest.MonkeyPatch):
    """stop() pops `running` before the context closes; Chrome is still flushing then."""
    src = _seeded_source()
    closing, finish = asyncio.Event(), asyncio.Event()

    async def blocked_close(*_args):
        closing.set()
        await finish.wait()

    monkeypatch.setattr(main.browser_mgr, "_close_context", blocked_close)
    main.browser_mgr.running[src["id"]] = RunningProfile(src["id"], object(), 19001, capture_preview=False)
    stop = asyncio.create_task(main.browser_mgr.stop(src["id"]))
    await asyncio.wait_for(closing.wait(), 2)
    try:
        assert not stop.done()
        assert src["id"] not in main.browser_mgr.running  # the reviewer's exact window
        assert await _status_of(main.duplicate_profile(src["id"], _WITH_STATE)) == 409
    finally:
        finish.set()
        await stop
    # Once the close has really finished, the copy is allowed
    clone = await main.duplicate_profile(src["id"], _WITH_STATE)
    assert (Path(clone.user_data_dir) / "Default" / "Cookies").read_text() == "session=abc"


async def test_duplicate_state_clone_is_unlisted_until_the_copy_completes(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    src = _seeded_source()
    copy = _PausableCopy()
    monkeypatch.setattr(main, "_copy_browser_state", copy)
    task = asyncio.create_task(main.duplicate_profile(src["id"], _WITH_STATE))
    assert await asyncio.to_thread(copy.started.wait, 2)
    try:
        # Mid-copy: the clone does not exist for anyone — not listed, not addressable
        assert [p.id for p in await main.list_profiles()] == [src["id"]]
    finally:
        copy.release.set()
    clone = await task
    assert [p.id for p in await main.list_profiles()] == [clone.id, src["id"]]
    assert (Path(clone.user_data_dir) / "Default" / "Cookies").read_text() == "session=abc"


async def test_duplicate_state_holds_the_source_for_the_whole_copy(
    tmp_db: Path, real_lifecycle, monkeypatch: pytest.MonkeyPatch
):
    """launch / reset / delete of the source are refused until the copy is done."""
    src = _seeded_source()
    copy = _PausableCopy()
    monkeypatch.setattr(main, "_copy_browser_state", copy)
    task = asyncio.create_task(main.duplicate_profile(src["id"], _WITH_STATE))
    assert await asyncio.to_thread(copy.started.wait, 2)
    try:
        assert await _status_of(main.reset_profile(src["id"])) == 409
        assert await _status_of(main.delete_profile(src["id"])) == 409
        assert await _status_of(main.launch_profile(src["id"])) == 409
        assert await _status_of(main.duplicate_profile(src["id"], _WITH_STATE)) == 409
        # ...while a config-only duplicate never touches the directory and is fine
        assert (await main.duplicate_profile(src["id"], None)).name == "Src (copy)"
    finally:
        copy.release.set()
    clone = await task
    # The snapshot is intact: the interleaved reset never got to wipe the source
    assert (Path(clone.user_data_dir) / "Default" / "Cookies").read_text() == "session=abc"
    assert (Path(src["user_data_dir"]) / "Default" / "Cookies").read_text() == "session=abc"
    # The hold is released afterwards: a reset now goes through
    assert await _status_of(main.reset_profile(src["id"])) == 200
    assert not main.browser_mgr._held


async def test_duplicate_state_cancellation_waits_for_the_worker_and_cleans_up(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """Cancelling the request cannot stop the copy thread. The source must stay
    held until the worker is done, and the unpublished directory must go."""
    src = _seeded_source()
    copy = _PausableCopy()
    monkeypatch.setattr(main, "_copy_browser_state", copy)
    task = asyncio.create_task(main.duplicate_profile(src["id"], _WITH_STATE))
    assert await asyncio.to_thread(copy.started.wait, 2)

    task.cancel()
    await asyncio.sleep(0.05)
    try:
        # Cancelled, but the worker is still copying: nothing may be released yet
        assert not task.done()
        assert not copy.done.is_set()
        assert src["id"] in main.browser_mgr._held
        assert await _status_of(main.reset_profile(src["id"])) == 409
    finally:
        copy.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert copy.done.is_set()
    assert copy.dst is not None and not copy.dst.exists()  # no orphaned clone dir
    assert [p["id"] for p in db.list_profiles()] == [src["id"]]  # no row either
    assert not main.browser_mgr._held
    assert (Path(src["user_data_dir"]) / "Default" / "Cookies").read_text() == "session=abc"
    assert await _status_of(main.reset_profile(src["id"])) == 200


async def test_duplicate_state_cancellation_wins_over_a_late_worker_error(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """A worker that fails AFTER the request was cancelled must not turn the
    cancellation into a 500; either way nothing is left behind."""
    src = _seeded_source()
    copy = _PausableCopy()
    copy.fail_with = OSError("disk full")
    monkeypatch.setattr(main, "_copy_browser_state", copy)
    task = asyncio.create_task(main.duplicate_profile(src["id"], _WITH_STATE))
    assert await asyncio.to_thread(copy.started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    copy.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert copy.dst is not None and not copy.dst.exists()
    assert [p["id"] for p in db.list_profiles()] == [src["id"]]
    assert not main.browser_mgr._held


async def test_reset_and_delete_refuse_a_profile_mid_launch(tmp_db: Path):
    """Same guard, same reason: wiping or removing a dir Chrome is initialising is unsafe."""
    src = _seeded_source()
    main.browser_mgr._launching.add(src["id"])
    try:
        assert await _status_of(main.reset_profile(src["id"])) == 409
        assert await _status_of(main.delete_profile(src["id"])) == 409
    finally:
        main.browser_mgr._launching.discard(src["id"])
    assert (Path(src["user_data_dir"]) / "Default" / "Cookies").read_text() == "session=abc"
    assert db.get_profile(src["id"]) is not None


# ── CDP proxy: download capture survives external clients ────────────────────


class _FakeUpstream:
    """A CDP target that accepts a connection, swallows ``expect`` frames, then hangs up.

    The browser ends the session here because the test client cancels the app task the
    moment its own side disconnects, before the proxy could finish.
    """

    def __init__(self, expect: int) -> None:
        self.expect = expect
        self.closed = False
        self.received: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def send(self, frame) -> None:
        self.received.append(frame)

    def __aiter__(self):
        return self

    async def __anext__(self):
        while len(self.received) < self.expect:
            await asyncio.sleep(0.01)
        raise StopAsyncIteration


def _cdp_version_client() -> AsyncMock:
    version = MagicMock()
    version.json.return_value = {"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"}
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=version)
    return client


def _capturing_profile(app_client: TestClient) -> RunningProfile:
    pid = app_client.post("/api/profiles", json={"name": "Detach"}).json()["id"]
    running = RunningProfile(pid, MagicMock(), 53123, download_rearm=asyncio.Event())
    main.browser_mgr.running[pid] = running
    return running


def _wait_for(condition, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)


def _run_proxied_client(
    app_client: TestClient, path: str, frames: list[dict], until
) -> tuple[_FakeUpstream, list[bool]]:
    """Connect through the proxy, send ``frames``, and stay connected until ``until`` holds
    or the browser side has hung up. Returns the fake browser and, for each time the detach
    hook fired, whether the upstream socket was already closed."""
    upstream = _FakeUpstream(expect=len(frames))
    seen_closed: list[bool] = []
    real_hook = main.browser_mgr.cdp_client_detached

    def hook(running: RunningProfile) -> None:
        seen_closed.append(upstream.closed)
        real_hook(running)

    with patch("websockets.connect", return_value=upstream), \
         patch("httpx.AsyncClient", return_value=_cdp_version_client()), \
         patch.object(main.browser_mgr, "cdp_client_detached", new=hook):
        with app_client.websocket_connect(path, headers={"origin": "http://testserver"}) as ws:
            for frame in frames:
                ws.send_text(json.dumps(frame))
            _wait_for(lambda: until() or upstream.closed)
            time.sleep(0.05)  # let the proxy run to the end after the browser hung up
    return upstream, seen_closed


_CDP_ROUTES = ["cdp", "cdp/devtools/page/abc"]


@pytest.mark.parametrize("route", _CDP_ROUTES)
def test_a_client_that_set_download_behaviour_re_arms_capture_when_it_leaves(
    app_client: TestClient, route: str
):
    """Chromium reverts browser-wide download behaviour to its default when such a client
    detaches; capture must be re-armed, and only once the upstream socket is closed."""
    running = _capturing_profile(app_client)
    upstream, seen_closed = _run_proxied_client(
        app_client, f"/api/profiles/{running.profile_id}/{route}", [
            {"id": 1, "method": "Target.getTargets"},
            {"id": 2, "method": "Browser.setDownloadBehavior", "params": {"behavior": "deny"}},
        ], until=running.download_rearm.is_set,
    )
    assert running.download_rearm.is_set()
    assert seen_closed == [True]
    assert len(upstream.received) == 2  # the frames still reached the browser
    main.browser_mgr.running.pop(running.profile_id, None)


@pytest.mark.parametrize("route", _CDP_ROUTES)
def test_a_client_that_left_download_behaviour_alone_does_not_re_arm(
    app_client: TestClient, route: str
):
    """Its detach does not reset Chromium — and re-arming would take the download path away
    from another client that is still attached and set its own."""
    running = _capturing_profile(app_client)
    upstream, seen_closed = _run_proxied_client(
        app_client, f"/api/profiles/{running.profile_id}/{route}", [
            {"id": 1, "method": "Target.getTargets"},
            {"id": 2, "method": "Runtime.evaluate", "params": {"expression": "'setDownloadBehavior'"}},
        ], until=lambda: False,
    )
    assert upstream.closed
    assert seen_closed == []
    assert not running.download_rearm.is_set()
    main.browser_mgr.running.pop(running.profile_id, None)


def test_a_proxy_that_never_reached_the_browser_does_not_re_arm(app_client: TestClient):
    running = _capturing_profile(app_client)
    with patch("websockets.connect", side_effect=OSError("refused")), \
         patch("httpx.AsyncClient", return_value=_cdp_version_client()):
        with app_client.websocket_connect(
            f"/api/profiles/{running.profile_id}/cdp", headers={"origin": "http://testserver"}
        ):
            pass
    time.sleep(0.1)
    assert not running.download_rearm.is_set()
    main.browser_mgr.running.pop(running.profile_id, None)


@pytest.mark.parametrize("frame, expected", [
    ('{"id":1,"method":"Browser.setDownloadBehavior","params":{"behavior":"deny"}}', True),
    # Through a flattened page session it still applies to the profile's own context.
    ('{"sessionId":"S","id":2,"method":"Browser.setDownloadBehavior","params":{"behavior":"allow","downloadPath":"/x"}}', True),
    ('{"id":3,"method":"Page.setDownloadBehavior","params":{"behavior":"allow","downloadPath":"/x"}}', True),
    # A context the client created itself is not the profile's; Chromium resets only that one.
    ('{"id":4,"method":"Browser.setDownloadBehavior","params":{"behavior":"deny","browserContextId":"C1"}}', False),
    ('{"id":5,"method":"Runtime.evaluate","params":{"expression":"\'setDownloadBehavior\'"}}', False),
    ('{"id":6,"method":"Target.getTargets"}', False),
    ('not json but mentions setDownloadBehavior', False),
    ('["setDownloadBehavior"]', False),
])
def test_only_download_behaviour_commands_for_the_profile_context_count(frame: str, expected: bool):
    assert main._sets_download_behaviour(frame) is expected
