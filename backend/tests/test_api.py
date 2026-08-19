"""Tests for FastAPI routes via TestClient."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from backend import main
from backend.browser_manager import RunningProfile
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
    main.browser_mgr.stop = AsyncMock()

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
    main.browser_mgr.stop = AsyncMock()
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
