"""Tests for FastAPI routes via TestClient."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
import pathlib
import socket

from starlette.testclient import TestClient

from backend import main
from backend.browser_manager import BrowserManager, RunningProfile


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
        "platform": "macos",
        "screen_width": 2560,
        "screen_height": 1440,
        "humanize": True,
        "human_preset": "careful",
        "tags": [{"tag": "work", "color": "#ff0000"}],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["fingerprint_seed"] == 42
    assert data["platform"] == "macos"
    assert len(data["tags"]) == 1


def test_create_profile_invalid_platform(app_client: TestClient):
    resp = app_client.post("/api/profiles", json={"name": "Bad", "platform": "android"})
    assert resp.status_code == 422


def test_get_profile(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "Get Me"})
    pid = create.json()["id"]
    resp = app_client.get(f"/api/profiles/{pid}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Get Me"


def test_get_profile_not_found(app_client: TestClient):
    resp = app_client.get("/api/profiles/nonexistent")
    assert resp.status_code == 404


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


def test_delete_profile_stops_running(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """Deleting a running profile should stop it first."""
    create = app_client.post("/api/profiles", json={"name": "Running"})
    pid = create.json()["id"]

    # Inject mock running profile
    mock_running = MagicMock(spec=RunningProfile)
    mock_running.display = 100
    mock_running.ws_port = 6100
    mock_running.cdp_port = 5100
    main.browser_mgr.running[pid] = mock_running
    # monkeypatch, not assignment: browser_mgr is a module singleton shared by
    # the whole session, so a plain assignment would leave every later test
    # calling this mock instead of the real method.
    # emulate the real stop(): it removes the profile from `running`, which
    # delete then re-checks before touching the filesystem
    calls: list[str] = []

    async def stop(target: str) -> bool:
        calls.append(target)
        main.browser_mgr.running.pop(target, None)
        return True  # browser really closed

    monkeypatch.setattr(main.browser_mgr, "stop", stop)

    resp = app_client.delete(f"/api/profiles/{pid}")
    assert resp.status_code == 200
    assert calls == [pid]


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


def test_status_stopped_alive_fields_null(app_client: TestClient):
    """Stopped profiles should report xvnc_alive/browser_alive as null."""
    create = app_client.post("/api/profiles", json={"name": "AliveStopped"})
    pid = create.json()["id"]
    resp = app_client.get(f"/api/profiles/{pid}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["xvnc_alive"] is None
    assert data["browser_alive"] is None


def test_status_starting_while_launch_in_flight(app_client: TestClient):
    """A profile mid-launch reports "starting", never "stopped"."""
    create = app_client.post("/api/profiles", json={"name": "Booting"})
    pid = create.json()["id"]

    main.browser_mgr._launching.add(pid)
    try:
        data = app_client.get(f"/api/profiles/{pid}/status").json()
        assert data["status"] == "starting"
        # list/detail views agree with the status endpoint
        assert app_client.get(f"/api/profiles/{pid}").json()["status"] == "starting"
    finally:
        main.browser_mgr._launching.discard(pid)

    assert app_client.get(f"/api/profiles/{pid}/status").json()["status"] == "stopped"


def test_status_starting_while_queued_for_auto_launch(app_client: TestClient):
    """Auto-launch is sequential; queued profiles must not read as stopped."""
    create = app_client.post("/api/profiles", json={"name": "Queued"})
    pid = create.json()["id"]

    main.browser_mgr._pending_auto_launch.add(pid)
    try:
        assert app_client.get(f"/api/profiles/{pid}/status").json()["status"] == "starting"
    finally:
        main.browser_mgr._pending_auto_launch.discard(pid)


def test_viewer_token_starting_is_retryable_503(app_client: TestClient):
    """503 (retry) not 404 (terminal) while the profile is coming up."""
    create = app_client.post("/api/profiles", json={"name": "BootingToken"})
    pid = create.json()["id"]

    main.browser_mgr._pending_auto_launch.add(pid)
    try:
        resp = app_client.post(f"/api/profiles/{pid}/viewer-token")
        assert resp.status_code == 503
    finally:
        main.browser_mgr._pending_auto_launch.discard(pid)

    assert app_client.post(f"/api/profiles/{pid}/viewer-token").status_code == 404


def test_launch_while_starting_conflicts(app_client: TestClient):
    """Launching a profile that is already coming up is a 409, not a 500."""
    create = app_client.post("/api/profiles", json={"name": "DoubleLaunch"})
    pid = create.json()["id"]

    main.browser_mgr._launching.add(pid)
    try:
        resp = app_client.post(f"/api/profiles/{pid}/launch")
        assert resp.status_code == 409
    finally:
        main.browser_mgr._launching.discard(pid)


def test_status_running_alive_fields(app_client: TestClient):
    """Running profiles report real liveness for Xvnc and the browser.

    browser_alive comes from a real connect to the CDP port — a listening
    socket stands in for Chromium's DevTools endpoint.
    """
    create = app_client.post("/api/profiles", json={"name": "AliveRunning"})
    pid = create.json()["id"]
    mock = _mock_running_profile(pid)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    mock.cdp_port = listener.getsockname()[1]

    # Fake a live Xvnc process for the profile's display
    fake_vnc = MagicMock()
    fake_vnc.process.poll.return_value = None
    main.browser_mgr.vnc._allocated[100] = fake_vnc

    try:
        resp = app_client.get(f"/api/profiles/{pid}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["xvnc_alive"] is True
        assert data["browser_alive"] is True
    finally:
        listener.close()
        main.browser_mgr.vnc._allocated.pop(100, None)
        main.browser_mgr.running.pop(pid, None)


def test_status_running_dead_processes(app_client: TestClient):
    """Exited Xvnc / a CDP port nobody is listening on report False."""
    create = app_client.post("/api/profiles", json={"name": "AliveDead"})
    pid = create.json()["id"]
    mock = _mock_running_profile(pid)
    mock.cdp_port = _closed_port()  # Chromium is gone

    fake_vnc = MagicMock()
    fake_vnc.process.poll.return_value = 1  # exited
    main.browser_mgr.vnc._allocated[100] = fake_vnc

    try:
        resp = app_client.get(f"/api/profiles/{pid}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["xvnc_alive"] is False
        assert data["browser_alive"] is False
    finally:
        main.browser_mgr.vnc._allocated.pop(100, None)
        main.browser_mgr.running.pop(pid, None)


def _closed_port() -> int:
    """A port that was bound and released — nothing is listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


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


def test_launch_invalid_proxy_400(app_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """ValueError from browser_mgr.launch should map to 400."""
    create = app_client.post("/api/profiles", json={"name": "BadProxy"})
    pid = create.json()["id"]
    monkeypatch.setattr(
        main.browser_mgr, "launch",
        AsyncMock(side_effect=ValueError("Invalid proxy scheme 'ftp'")),
    )
    resp = app_client.post(f"/api/profiles/{pid}/launch")
    assert resp.status_code == 400
    assert "ftp" in resp.json()["detail"]


def test_launch_failure_500(app_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Generic exception from browser_mgr.launch should map to 500."""
    create = app_client.post("/api/profiles", json={"name": "Crash"})
    pid = create.json()["id"]
    monkeypatch.setattr(
        main.browser_mgr, "launch", AsyncMock(side_effect=RuntimeError("Xvnc failed")),
    )
    resp = app_client.post(f"/api/profiles/{pid}/launch")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to launch browser"


def test_stop_not_running(app_client: TestClient):
    resp = app_client.post("/api/profiles/nonexistent/stop")
    assert resp.status_code == 404


# ── Viewer Sessions ──────────────────────────────────────────────────────────


def test_viewer_token_not_running(app_client: TestClient):
    resp = app_client.post("/api/profiles/nonexistent/viewer-token")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Profile not running"


def test_viewer_token_success(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "ViewerTok"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    resp = app_client.post(f"/api/profiles/{pid}/viewer-token")
    assert resp.status_code == 200
    data = resp.json()
    assert data["token"]
    assert data["viewer_url"] == f"/viewer/{data['token']}/"
    assert data["expires_in"] == 3600

    # Token actually validates against the store
    session = main.viewer_tokens.validate(data["token"])
    assert session is not None
    assert session.profile_id == pid
    assert session.ws_port == 6100

    # Cleanup
    main.viewer_tokens.revoke_profile(pid)
    main.browser_mgr.running.pop(pid, None)


def test_viewer_token_fresh_per_call(app_client: TestClient):
    """Each call issues a fresh token; older tokens stay valid until expiry."""
    create = app_client.post("/api/profiles", json={"name": "ViewerFresh"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    t1 = app_client.post(f"/api/profiles/{pid}/viewer-token").json()["token"]
    t2 = app_client.post(f"/api/profiles/{pid}/viewer-token").json()["token"]
    assert t1 != t2
    assert main.viewer_tokens.validate(t1) is not None
    assert main.viewer_tokens.validate(t2) is not None

    # Cleanup
    main.viewer_tokens.revoke_profile(pid)
    main.browser_mgr.running.pop(pid, None)


def test_stop_revokes_viewer_tokens(app_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Stopping a profile revokes its viewer tokens."""
    create = app_client.post("/api/profiles", json={"name": "ViewerStop"})
    pid = create.json()["id"]
    mock = _mock_running_profile(pid)
    # Model Playwright: is_closed() is sync, close() is a coroutine.
    mock.context = MagicMock()
    mock.context.is_closed.return_value = False
    mock.context.close = AsyncMock()
    monkeypatch.setattr(main.browser_mgr.vnc, "stop_vnc", AsyncMock())
    token = main.viewer_tokens.issue(pid, 6100)

    resp = app_client.post(f"/api/profiles/{pid}/stop")
    assert resp.status_code == 200
    assert main.viewer_tokens.validate(token) is None

    # Cleanup
    main.browser_mgr.running.pop(pid, None)


def test_delete_profile_revokes_viewer_tokens(app_client: TestClient):
    """Deleting a (stopped) profile revokes its viewer tokens unconditionally."""
    create = app_client.post("/api/profiles", json={"name": "ViewerDel"})
    pid = create.json()["id"]
    token = main.viewer_tokens.issue(pid, 6100)

    resp = app_client.delete(f"/api/profiles/{pid}")
    assert resp.status_code == 200
    assert main.viewer_tokens.validate(token) is None


# ── Viewer Auth (nginx auth_request) ─────────────────────────────────────────


def test_viewer_auth_missing_original_uri(app_client: TestClient):
    resp = app_client.get("/api/viewer-auth")
    assert resp.status_code == 403


def test_viewer_auth_non_viewer_uri(app_client: TestClient):
    resp = app_client.get("/api/viewer-auth", headers={"X-Original-URI": "/api/profiles"})
    assert resp.status_code == 403


def test_viewer_auth_bad_token(app_client: TestClient):
    resp = app_client.get("/api/viewer-auth", headers={"X-Original-URI": "/viewer/bogus/"})
    assert resp.status_code == 403


def test_viewer_auth_profile_stopped(app_client: TestClient):
    """Valid token but the profile is no longer running → 403.

    Must be 403 (or 401): nginx auth_request turns any other non-2xx into a
    500 for the client. Verified live against the shipped config — a 404
    subrequest logs "auth request unexpected status: 404" and serves 500.
    """
    create = app_client.post("/api/profiles", json={"name": "ViewerGone"})
    pid = create.json()["id"]
    token = main.viewer_tokens.issue(pid, 6100)  # no running profile registered

    resp = app_client.get("/api/viewer-auth", headers={"X-Original-URI": f"/viewer/{token}/"})
    assert resp.status_code == 403

    # Cleanup
    main.viewer_tokens.revoke_profile(pid)


def test_viewer_auth_success(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "ViewerOk"})
    pid = create.json()["id"]
    _mock_running_profile(pid)
    token = main.viewer_tokens.issue(pid, 6100)

    resp = app_client.get("/api/viewer-auth", headers={"X-Original-URI": f"/viewer/{token}/"})
    assert resp.status_code == 200
    assert resp.headers["X-Viewer-Upstream"] == "127.0.0.1:6100"

    # Nested asset paths (with query strings) share the same token
    resp = app_client.get(
        "/api/viewer-auth",
        headers={"X-Original-URI": f"/viewer/{token}/app/dist/main.js?v=1"},
    )
    assert resp.status_code == 200

    # Cleanup
    main.viewer_tokens.revoke_profile(pid)
    main.browser_mgr.running.pop(pid, None)


def test_viewer_auth_injects_kasm_basic_auth(app_client: TestClient, monkeypatch):
    """When per-display Kasm credentials exist, viewer-auth hands them to nginx."""
    create = app_client.post("/api/profiles", json={"name": "ViewerAuth"})
    pid = create.json()["id"]
    _mock_running_profile(pid)
    token = main.viewer_tokens.issue(pid, 6100)
    monkeypatch.setattr(
        main.browser_mgr.vnc, "get_api_credentials", lambda _d: ("manager", "pw123")
    )

    resp = app_client.get("/api/viewer-auth", headers={"X-Original-URI": f"/viewer/{token}/"})
    assert resp.status_code == 200
    import base64

    expected = "Basic " + base64.b64encode(b"manager:pw123").decode()
    assert resp.headers["X-Viewer-Authorization"] == expected

    # Cleanup
    main.viewer_tokens.revoke_profile(pid)
    main.browser_mgr.running.pop(pid, None)


def test_kasm_stats_not_running(app_client: TestClient):
    resp = app_client.get("/api/profiles/nonexistent/kasm-stats")
    assert resp.status_code == 404


def test_kasm_stats_success(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "StatsOk"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    bottleneck_resp = MagicMock()
    bottleneck_resp.status_code = 200
    bottleneck_resp.json.return_value = {"code": 0, "bottleneck": "cpu"}
    sessions_resp = MagicMock()
    sessions_resp.status_code = 200
    sessions_resp.json.return_value = {"users": [{"username": "manager"}]}
    frame_resp = MagicMock()
    frame_resp.status_code = 200
    frame_resp.json.return_value = {"clients": {"all": {"fps": 60}}}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=[bottleneck_resp, sessions_resp, frame_resp])

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = app_client.get(f"/api/profiles/{pid}/kasm-stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["bottleneck"] == {"code": 0, "bottleneck": "cpu"}
    assert data["sessions"] == {"users": [{"username": "manager"}]}
    assert data["frame"] == {"clients": {"all": {"fps": 60}}}

    # Cleanup
    main.browser_mgr.running.pop(pid, None)


def test_kasm_stats_no_viewers_skips_frame_stats(app_client: TestClient):
    """get_frame_stats hangs with no clients — it must not be called."""
    create = app_client.post("/api/profiles", json={"name": "StatsEmpty"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    bottleneck_resp = MagicMock()
    bottleneck_resp.status_code = 200
    bottleneck_resp.json.return_value = {}
    sessions_resp = MagicMock()
    sessions_resp.status_code = 200
    sessions_resp.json.return_value = {"users": []}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=[bottleneck_resp, sessions_resp])

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = app_client.get(f"/api/profiles/{pid}/kasm-stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["frame"] is None
    assert mock_client.get.await_count == 2  # no get_frame_stats call

    # Cleanup
    main.browser_mgr.running.pop(pid, None)


def test_kasm_stats_frame_stats_failure_isolated(app_client: TestClient):
    """Frame-stats errors (503/timeout) must not fail the whole endpoint."""
    create = app_client.post("/api/profiles", json={"name": "StatsIso"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    bottleneck_resp = MagicMock()
    bottleneck_resp.status_code = 200
    bottleneck_resp.json.return_value = {"manager": {}}
    sessions_resp = MagicMock()
    sessions_resp.status_code = 200
    sessions_resp.json.return_value = {"users": [{"username": "manager"}]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(
        side_effect=[bottleneck_resp, sessions_resp, ConnectionError("timeout")]
    )

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = app_client.get(f"/api/profiles/{pid}/kasm-stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["bottleneck"] == {"manager": {}}
    assert data["frame"] is None

    # Cleanup
    main.browser_mgr.running.pop(pid, None)


def test_kasm_stats_kasm_unreachable(app_client: TestClient):
    """502 when the KasmVNC stats endpoint is down."""
    create = app_client.post("/api/profiles", json={"name": "StatsDown"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=ConnectionError("refused"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = app_client.get(f"/api/profiles/{pid}/kasm-stats")

    assert resp.status_code == 502

    # Cleanup
    main.browser_mgr.running.pop(pid, None)


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

    # Same-origin passes Origin check. The CDP proxy then fails to reach real
    # Chrome (not running) and closes — fine, we're testing Origin only.
    # The connection is accepted (no 4403), then closes on CDP connect error.
    try:
        with app_client.websocket_connect(
            f"/api/profiles/{pid}/cdp",
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
        with app_client.websocket_connect(f"/api/profiles/{pid}/cdp") as ws:
            pass
    except Exception as exc:
        assert "4403" not in str(exc)
    main.browser_mgr.running.pop(pid, None)


def test_kasm_stats_upstream_error_is_502(app_client: TestClient):
    """A Kasm 401/5xx must not be handed back as 200 with an HTML body."""
    create = app_client.post("/api/profiles", json={"name": "StatsAuthFail"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    unauthorized = MagicMock()
    unauthorized.status_code = 401
    unauthorized.text = "<html>401 Unauthorized</html>"
    unauthorized.json.side_effect = ValueError("not json")
    sessions_resp = MagicMock()
    sessions_resp.status_code = 200
    sessions_resp.json.return_value = {"users": []}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=[unauthorized, sessions_resp])

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = app_client.get(f"/api/profiles/{pid}/kasm-stats")
        assert resp.status_code == 502
        assert "401" in resp.json()["detail"]
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_delete_while_starting_conflicts(app_client: TestClient):
    """Deleting mid-launch would rmtree the user_data_dir under Chromium."""
    create = app_client.post("/api/profiles", json={"name": "DeleteRace"})
    pid = create.json()["id"]

    main.browser_mgr._launching.add(pid)
    try:
        assert app_client.delete(f"/api/profiles/{pid}").status_code == 409
    finally:
        main.browser_mgr._launching.discard(pid)

    # still there, and deletable once the launch settles
    assert app_client.get(f"/api/profiles/{pid}").status_code == 200
    assert app_client.delete(f"/api/profiles/{pid}").status_code == 200


def test_stop_while_starting_conflicts(app_client: TestClient):
    """Mid-launch is not "not running" — 409, not 404."""
    create = app_client.post("/api/profiles", json={"name": "StopRace"})
    pid = create.json()["id"]

    main.browser_mgr._pending_auto_launch.add(pid)
    try:
        assert app_client.post(f"/api/profiles/{pid}/stop").status_code == 409
    finally:
        main.browser_mgr._pending_auto_launch.discard(pid)

    assert app_client.post(f"/api/profiles/{pid}/stop").status_code == 404


def test_browser_mgr_singleton_is_not_permanently_shadowed():
    """No test may leave a mock bolted onto the shared BrowserManager.

    browser_mgr is a module-level singleton. A plain `main.browser_mgr.stop =
    AsyncMock()` is never undone, so every later test that exercises stop()
    or launch() silently asserts against the mock instead of production code.
    monkeypatch.setattr restores on teardown; assignment does not.
    """
    for name in ("stop", "launch"):
        bound = getattr(main.browser_mgr, name)
        assert getattr(bound, "__func__", None) is getattr(BrowserManager, name), (
            f"browser_mgr.{name} is still a test double — "
            "use monkeypatch.setattr instead of assignment"
        )


def test_viewer_auth_rejects_a_token_from_a_previous_run(app_client: TestClient):
    """Upstream and credentials must describe the same instance."""
    create = app_client.post("/api/profiles", json={"name": "ViewerStale"})
    pid = create.json()["id"]
    mock = _mock_running_profile(pid)
    mock.ws_port = 6105  # relaunched on a different display
    token = main.viewer_tokens.issue(pid, 6100)  # token from the previous run

    try:
        resp = app_client.get(
            "/api/viewer-auth", headers={"X-Original-URI": f"/viewer/{token}/"},
        )
        assert resp.status_code == 403
    finally:
        main.viewer_tokens.revoke_profile(pid)
        main.browser_mgr.running.pop(pid, None)


def test_delete_rechecks_after_stopping(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """A launch claiming the profile during stop() must not get its dir deleted."""
    create = app_client.post("/api/profiles", json={"name": "DeleteAfterStop"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    async def stop_then_relaunch(_pid: str) -> bool:
        # what a concurrent POST /launch would do while stop() awaits the close
        main.browser_mgr.running.pop(_pid, None)
        main.browser_mgr._launching.add(_pid)
        return True

    monkeypatch.setattr(main.browser_mgr, "stop", stop_then_relaunch)
    try:
        resp = app_client.delete(f"/api/profiles/{pid}")
        assert resp.status_code == 409
        # and the profile still exists
        assert app_client.get(f"/api/profiles/{pid}").status_code == 200
    finally:
        main.browser_mgr._launching.discard(pid)
        main.browser_mgr.running.pop(pid, None)


def test_delete_refuses_when_the_browser_did_not_close(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """A wedged teardown must not let rmtree run under a live Chromium."""
    create = app_client.post("/api/profiles", json={"name": "DeleteWedged"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    async def stop_but_not_closed(target: str) -> bool:
        main.browser_mgr.running.pop(target, None)
        return False  # bounded close timed out; Chromium may still be alive

    monkeypatch.setattr(main.browser_mgr, "stop", stop_but_not_closed)
    try:
        assert app_client.delete(f"/api/profiles/{pid}").status_code == 409
        assert app_client.get(f"/api/profiles/{pid}").status_code == 200
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_raced_launch_is_409_not_500(app_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Losing the launch race is a conflict, not a server error."""
    from backend.browser_manager import ProfileAlreadyRunning

    create = app_client.post("/api/profiles", json={"name": "RacedLaunch"})
    pid = create.json()["id"]

    async def already(_profile):
        raise ProfileAlreadyRunning("Profile is already running")

    monkeypatch.setattr(main.browser_mgr, "launch", already)
    assert app_client.post(f"/api/profiles/{pid}/launch").status_code == 409


def test_launch_is_blocked_while_a_delete_is_in_flight(app_client: TestClient):
    """The delete claim closes the window the post-stop re-check only narrowed."""
    create = app_client.post("/api/profiles", json={"name": "DeleteClaim"})
    pid = create.json()["id"]

    main.browser_mgr.claim_for_delete(pid)
    try:
        assert app_client.post(f"/api/profiles/{pid}/launch").status_code == 409
    finally:
        main.browser_mgr.release_delete_claim(pid)

    # released again once the delete finishes
    assert main.browser_mgr.is_starting(pid) is False


def test_concurrent_delete_is_refused_rather_than_sharing_the_claim(
    app_client: TestClient,
):
    """The second delete must not be able to release the first one's claim."""
    create = app_client.post("/api/profiles", json={"name": "DoubleDelete"})
    pid = create.json()["id"]

    assert main.browser_mgr.claim_for_delete(pid) is True
    try:
        # a second delete arriving while the first is in flight
        assert app_client.delete(f"/api/profiles/{pid}").status_code == 409
        # and the first claim is still held, so launches stay blocked
        assert app_client.post(f"/api/profiles/{pid}/launch").status_code == 409
    finally:
        main.browser_mgr.release_delete_claim(pid)

    assert main.browser_mgr.claim_for_delete(pid) is True
    main.browser_mgr.release_delete_claim(pid)


def test_delete_refuses_while_a_previous_stop_is_still_wedged(app_client: TestClient):
    """The round-4 guard only fired when the delete itself did the stop."""
    create = app_client.post("/api/profiles", json={"name": "WedgedThenDelete"})
    pid = create.json()["id"]

    from unittest.mock import MagicMock as _MM
    import time as _t
    main.browser_mgr._closing[pid] = (_MM(), _t.monotonic() + 60, None, _t.monotonic())  # a previous /stop left Chromium alive
    try:
        assert app_client.delete(f"/api/profiles/{pid}").status_code == 409
        assert app_client.post(f"/api/profiles/{pid}/launch").status_code == 409
        assert app_client.get(f"/api/profiles/{pid}").status_code == 200
    finally:
        main.browser_mgr._closing.pop(pid, None)

    assert app_client.delete(f"/api/profiles/{pid}").status_code == 200


def test_manual_launch_has_a_server_side_deadline(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """A wedged launch must not hang the request and strand `_launching`."""
    import asyncio as aio

    create = app_client.post("/api/profiles", json={"name": "LaunchHang"})
    pid = create.json()["id"]

    async def never_returns(_profile):
        await aio.sleep(3600)

    monkeypatch.setattr(main.browser_mgr, "launch", never_returns)
    monkeypatch.setattr(main, "LAUNCH_TIMEOUT_S", 0.05)
    resp = app_client.post(f"/api/profiles/{pid}/launch")
    assert resp.status_code == 504


def test_delete_does_not_block_the_event_loop(app_client: TestClient, monkeypatch):
    """rmtree of a large profile dir must not stall every other request."""
    create = app_client.post("/api/profiles", json={"name": "BigDelete"})
    body = create.json()
    pid = body["id"]
    # the dir only exists once the profile has been launched at least once
    pathlib.Path(body["user_data_dir"]).mkdir(parents=True, exist_ok=True)

    called: dict[str, object] = {}
    real_rmtree = main.shutil.rmtree

    def tracking_rmtree(path, **kw):
        import asyncio as aio
        # An executor thread has no running loop; the loop's own thread does.
        # Thread NAMES prove nothing here — TestClient already runs the app off
        # the main thread.
        try:
            aio.get_running_loop()
            called["on_event_loop"] = True
        except RuntimeError:
            called["on_event_loop"] = False
        return real_rmtree(path, **kw)

    monkeypatch.setattr(main.shutil, "rmtree", tracking_rmtree)
    assert app_client.delete(f"/api/profiles/{pid}").status_code == 200
    assert called.get("on_event_loop") is False


def test_status_endpoint_probes_off_the_event_loop(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """_browser_alive opens a TCP connection; GET /status must not do that on
    the loop that also serves nginx's viewer auth_request subrequests."""
    import asyncio as aio

    create = app_client.post("/api/profiles", json={"name": "ProbeLoop"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    seen: dict[str, bool] = {}

    def probing(_running):
        try:
            aio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return True

    monkeypatch.setattr(main.browser_mgr, "_browser_alive", probing)
    try:
        assert app_client.get(f"/api/profiles/{pid}/status").status_code == 200
        assert seen.get("on_loop") is False
    finally:
        main.browser_mgr.running.pop(pid, None)
