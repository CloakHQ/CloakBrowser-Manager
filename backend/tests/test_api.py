"""Tests for FastAPI routes via TestClient."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
import pathlib
import socket

from starlette.testclient import TestClient

from backend import browser_manager as bm
from backend import main
from backend.browser_manager import BrowserManager, RunningProfile


def _wedge(pid: str) -> bm.ClosingClaim:
    """Record a teardown claim whose browser is provably still alive.

    A real live pid, not a sentinel: the claim resolves by asking /proc
    whether THAT process is still running, so a fabricated identity would be
    released by the first check and the test would silently exercise nothing.
    """
    import os as _os

    _state, _ppid, starttime = bm._proc_stat(_os.getpid())
    claim = bm.ClosingClaim(
        context=MagicMock(),
        proc=bm.BrowserProcess(
            pid=_os.getpid(), starttime=starttime,
            user_data_dir="/tmp/udd", cdp_port=5100,
        ),
        user_data_dir="/tmp/udd", cdp_port=5100,
        # Fresh, so the sweeper's escalation clock cannot fire and SIGTERM the
        # test runner.
        claimed_at=__import__("time").monotonic(),
    )
    main.browser_mgr._closing[pid] = claim
    return claim


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
    token = main.viewer_tokens.issue(pid, 6100, session_epoch="epoch-under-test")

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
    token = main.viewer_tokens.issue(pid, 6100, session_epoch="epoch-under-test")
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

    # Cleanup — including the tracked xclip: browser_mgr is a session-wide
    # singleton and so is _xclip_procs.
    main.browser_mgr.running.pop(pid, None)
    main._xclip_procs.pop(100, None)
    main._xclip_locks.pop(100, None)


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
        assert profile["status"] in ("running", "starting", "stopping", "stopped")


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
    mock.proc = None
    # Real value, not a MagicMock: viewer_auth compares it against the token's
    # epoch, and a MagicMock would make every token look stale.
    mock.session_epoch = "epoch-under-test"
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
    token = main.viewer_tokens.issue(
        pid, 6100, session_epoch="epoch-under-test",  # token from the previous run
    )

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

    _wedge(pid)  # a previous /stop left Chromium alive
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


# ── "stopping" lifecycle ─────────────────────────────────────────────────────


def test_a_teardown_in_flight_tells_one_coherent_story(app_client: TestClient):
    """One state, one answer, on every path.

    A wedged teardown used to produce four mutually contradictory answers:
    list/detail/status said "stopped", launch said "Profile is already
    running", stop said 404 "Profile is not running", and only DELETE told the
    truth. The UI rendered the grey dot and an enabled Launch button that was
    guaranteed to 409.
    """
    create = app_client.post("/api/profiles", json={"name": "Wedged"})
    pid = create.json()["id"]
    _wedge(pid)
    try:
        assert app_client.get("/api/profiles").json()[0]["status"] == "stopping"
        assert app_client.get(f"/api/profiles/{pid}").json()["status"] == "stopping"
        assert app_client.put(
            f"/api/profiles/{pid}", json={"name": "Wedged"},
        ).json()["status"] == "stopping"

        status = app_client.get(f"/api/profiles/{pid}/status").json()
        assert status["status"] == "stopping"
        # nothing left to probe: same shape as "stopped"
        assert status["xvnc_alive"] is None
        assert status["browser_alive"] is None
        assert status["vnc_ws_port"] is None
        assert status["cdp_url"] is None

        detail = "Browser is still shutting down; try again"
        launch = app_client.post(f"/api/profiles/{pid}/launch")
        assert launch.status_code == 409
        assert launch.json()["detail"] == detail

        stop = app_client.post(f"/api/profiles/{pid}/stop")
        assert stop.status_code == 409
        assert stop.json()["detail"] == detail

        delete = app_client.delete(f"/api/profiles/{pid}")
        assert delete.status_code == 409
        assert delete.json()["detail"] == detail
    finally:
        main.browser_mgr._closing.pop(pid, None)

    assert app_client.get(f"/api/profiles/{pid}").json()["status"] == "stopped"


def test_starting_wins_over_a_teardown_claim(app_client: TestClient):
    """A launch holds a claim for its whole duration, so both can be true.

    Every mutation path checks is_starting first and answers "Profile is
    starting", so the status has to agree with the refusal the caller gets.
    """
    create = app_client.post("/api/profiles", json={"name": "Both"})
    pid = create.json()["id"]
    _wedge(pid)
    main.browser_mgr._launching.add(pid)
    try:
        assert app_client.get(f"/api/profiles/{pid}/status").json()["status"] == "starting"
        assert app_client.get(f"/api/profiles/{pid}").json()["status"] == "starting"
        assert app_client.post(f"/api/profiles/{pid}/launch").json()["detail"] == (
            "Profile is already starting"
        )
    finally:
        main.browser_mgr._launching.discard(pid)
        main.browser_mgr._closing.pop(pid, None)


def test_the_status_endpoint_never_mutates_the_teardown_claim(app_client: TestClient):
    """The 3s poll reaches get_status on an EXECUTOR thread.

    A probing, mutating check there raced the loop's own writes to _closing:
    a `del` raised KeyError when the close handler removed the entry first,
    and a re-write resurrected a claim stop() had already released.
    """
    create = app_client.post("/api/profiles", json={"name": "PollWedged"})
    pid = create.json()["id"]
    claim = _wedge(pid)
    try:
        for _ in range(5):
            assert app_client.get(
                f"/api/profiles/{pid}/status",
            ).json()["status"] == "stopping"
        assert main.browser_mgr._closing[pid] is claim
        assert claim.sigterm_at is None
    finally:
        main.browser_mgr._closing.pop(pid, None)


def test_the_lifespan_sweeper_releases_a_claim_with_no_user_action(
    tmp_db, monkeypatch: pytest.MonkeyPatch,
):
    """Nothing else opens the valve once the status path is a pure peek.

    Without the sweeper the profile shows an orange "stopping" dot and a
    DISABLED "Shutting down…" button indefinitely — so the user's only escape
    is a control they cannot click.
    """
    import time as _t
    from unittest.mock import AsyncMock as _AM

    monkeypatch.setattr(bm, "CLAIM_SWEEP_INTERVAL_S", 0.01)
    monkeypatch.setattr(main.browser_mgr, "cleanup_stale", _AM())
    monkeypatch.setattr(main.browser_mgr, "cleanup_all", _AM())
    monkeypatch.setattr(main.browser_mgr.vnc, "cleanup_stale", _AM())

    with TestClient(main.app) as client:
        pid = client.post("/api/profiles", json={"name": "Sweepable"}).json()["id"]
        # a claim whose process is provably gone: same pid, different starttime
        claim = _wedge(pid)
        claim.proc = bm.BrowserProcess(
            pid=claim.proc.pid, starttime=claim.proc.starttime + 1,
            user_data_dir=claim.proc.user_data_dir, cdp_port=claim.proc.cdp_port,
        )
        assert client.get(f"/api/profiles/{pid}").json()["status"] == "stopping"

        deadline = _t.monotonic() + 5
        while _t.monotonic() < deadline:
            if client.get(f"/api/profiles/{pid}").json()["status"] == "stopped":
                break
            _t.sleep(0.02)
        assert client.get(f"/api/profiles/{pid}").json()["status"] == "stopped"
        assert pid not in main.browser_mgr._closing


# ── viewer token session identity ────────────────────────────────────────────


def test_viewer_auth_rejects_a_token_from_a_previous_session_on_the_same_port(
    app_client: TestClient,
):
    """allocate() gap-fills, so a relaunch reuses the identical ws_port.

    The port comparison therefore can never fire for the case it was written
    for; only a per-launch nonce can tell two sessions apart.
    """
    create = app_client.post("/api/profiles", json={"name": "EpochStale"})
    pid = create.json()["id"]
    mock = _mock_running_profile(pid)
    token = main.viewer_tokens.issue(pid, 6100, session_epoch="epoch-1")

    # relaunched: same display, same ws_port, new session
    mock.session_epoch = "epoch-2"
    try:
        resp = app_client.get(
            "/api/viewer-auth", headers={"X-Original-URI": f"/viewer/{token}/"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Viewer token is stale"
    finally:
        main.viewer_tokens.revoke_profile(pid)
        main.browser_mgr.running.pop(pid, None)


def test_viewer_token_carries_the_running_session_epoch(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "EpochOk"})
    pid = create.json()["id"]
    _mock_running_profile(pid)
    try:
        token = app_client.post(f"/api/profiles/{pid}/viewer-token").json()["token"]
        assert main.viewer_tokens.validate(token).session_epoch == "epoch-under-test"
    finally:
        main.viewer_tokens.revoke_profile(pid)
        main.browser_mgr.running.pop(pid, None)


# ── AuthMiddleware robustness ────────────────────────────────────────────────


def _auth_scope(header: bytes) -> dict:
    return {"type": "http", "path": "/api/profiles", "headers": [(b"authorization", header)]}


def test_a_non_ascii_authorization_header_is_unauthenticated_not_a_crash(monkeypatch):
    """httpx refuses to send these, so this has to be asserted at the ASGI layer.

    A bare latin-1 byte used to raise UnicodeDecodeError out of val.decode(),
    and a well-formed non-ASCII token raised TypeError out of
    hmac.compare_digest — both surfacing as an unauthenticated HTTP 500 with a
    traceback, before any authentication decision.
    """
    monkeypatch.setattr(main, "AUTH_TOKEN", "test-secret")
    assert main._check_auth(_auth_scope(b"Bearer \xe9vil")) is False
    assert main._check_auth(_auth_scope(b"Bearer \xc3\xa9vil")) is False
    assert main._check_auth(_auth_scope(b"Bearer test-secret")) is True
    assert main._check_auth(
        {"type": "http", "path": "/api/profiles",
         "headers": [(b"cookie", b"auth_token=\xe9vil")]},
    ) is False


# ── Clipboard robustness ─────────────────────────────────────────────────────


def test_get_clipboard_skips_a_wedged_page_instead_of_dying_on_it(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """page.evaluate has no timeout in playwright 1.60 and hangs forever.

    One page running `while(true){}` used to kill this endpoint permanently
    for that profile: the loop never reached the next page or the xclip
    fallback, and uvicorn does not cancel a handler when the client gives up,
    so every poll added another immortal task.
    """
    import asyncio as aio
    import time as _t

    monkeypatch.setattr(main, "_CLIPBOARD_PAGE_TIMEOUT_S", 0.05)

    # Real coroutine functions, not AsyncMock: an AsyncMock side_effect that
    # returns a coroutine hands it back as the RESULT, so nothing ever hangs
    # and the test would pass against an unbounded evaluate.
    async def wedged_evaluate(_js):
        await aio.sleep(3600)

    reached: list[str] = []

    async def good_evaluate(_js):
        reached.append(_js)
        return "from the second tab"

    wedged = MagicMock()
    wedged.evaluate = wedged_evaluate
    good = MagicMock()
    good.evaluate = good_evaluate

    create = app_client.post("/api/profiles", json={"name": "ClipWedged"})
    pid = create.json()["id"]
    mock = _mock_running_profile(pid)
    mock.context = MagicMock()
    mock.context.pages = [wedged, good]

    try:
        started = _t.monotonic()
        resp = app_client.get(f"/api/profiles/{pid}/clipboard")
        elapsed = _t.monotonic() - started
        assert resp.status_code == 200
        assert resp.json()["text"] == "from the second tab"
        assert reached, "the wedged tab must be skipped, not fatal"
        assert elapsed < 3.0
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_get_clipboard_caps_the_response_at_1mb(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "ClipHuge"})
    pid = create.json()["id"]
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value="x" * (main._CLIPBOARD_MAX_READ + 500))
    mock = _mock_running_profile(pid)
    mock.context = MagicMock()
    mock.context.pages = [page]
    try:
        resp = app_client.get(f"/api/profiles/{pid}/clipboard")
        assert len(resp.json()["text"]) == main._CLIPBOARD_MAX_READ
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_clipboard_endpoints_degrade_when_xclip_is_missing(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """No xclip is the README's local-dev path; it must not be a 500."""
    create = app_client.post("/api/profiles", json={"name": "NoXclip"})
    pid = create.json()["id"]
    mock = _mock_running_profile(pid)
    mock.context = MagicMock()
    mock.context.pages = []

    async def missing(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory", "xclip")

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", missing)
    try:
        assert app_client.get(f"/api/profiles/{pid}/clipboard").json() == {"text": ""}
        post = app_client.post(f"/api/profiles/{pid}/clipboard", json={"text": "hi"})
        assert post.status_code == 503
    finally:
        main.browser_mgr.running.pop(pid, None)
        main._xclip_procs.pop(100, None)
        main._xclip_locks.pop(100, None)


@pytest.mark.asyncio
async def test_concurrent_set_clipboard_leaves_no_orphan_xclip(
    tmp_db, monkeypatch: pytest.MonkeyPatch,
):
    """Three pastes in the same millisecond used to orphan two xclips.

    The pop/spawn/store sequence contains two awaits, so every caller popped
    the same entry and only the last was tracked. The untracked ones owned the
    X11 CLIPBOARD selection, were never killed, and outlived the request.
    """
    import asyncio as aio

    spawned: list[MagicMock] = []

    async def fake_exec(*_a, **_k):
        await aio.sleep(0)                      # the real await points
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.drain = AsyncMock()

        async def wait():
            proc.returncode = -9
        proc.wait = wait
        proc.kill = MagicMock(side_effect=lambda: setattr(proc, "returncode", -9))
        spawned.append(proc)
        return proc

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)
    pid = "clip-race"
    mock = MagicMock(spec=RunningProfile)
    mock.display = 100
    main.browser_mgr.running[pid] = mock
    try:
        await aio.gather(*(
            main.set_clipboard(pid, main.ClipboardRequest(text=f"t{i}"))
            for i in range(3)
        ))
        assert len(spawned) == 3
        survivors = [p for p in spawned if p.returncode is None]
        assert len(survivors) == 1
        assert main._xclip_procs[100] is survivors[0]
    finally:
        main.browser_mgr.running.pop(pid, None)
        main._xclip_procs.pop(100, None)
        main._xclip_locks.pop(100, None)


@pytest.mark.asyncio
async def test_stopping_a_profile_reaps_its_xclip(monkeypatch: pytest.MonkeyPatch):
    """An xclip must not outlive the X server it holds a selection on."""
    mgr = bm.BrowserManager()
    mgr.add_display_released_hook(main._reap_xclip_for_display)
    proc = MagicMock()
    proc.returncode = None
    proc.kill = MagicMock(side_effect=lambda: setattr(proc, "returncode", -9))
    main._xclip_procs[100] = proc

    context = MagicMock()
    context.is_closed.return_value = False
    context.close = AsyncMock()
    mgr.running["p1"] = bm.RunningProfile(
        profile_id="p1", context=context, display=100, ws_port=6100, cdp_port=5100,
    )
    monkeypatch.setattr(mgr.vnc, "stop_vnc", AsyncMock())
    try:
        await mgr.stop("p1")
        proc.kill.assert_called_once()
        assert 100 not in main._xclip_procs
    finally:
        main._xclip_procs.pop(100, None)


# ── CDP WebSocket proxy data plane ───────────────────────────────────────────


class _FakeCdpSocket:
    """Minimal stand-in for the `websockets` client connection."""

    def __init__(self):
        import asyncio as aio

        self.received: list[object] = []
        self.outbound: aio.Queue = aio.Queue()
        self.closed = False

    async def send(self, message):
        self.received.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.outbound.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True
        return False


def test_cdp_proxy_forwards_frames_in_both_directions(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """Dropping ALL client->CDP forwarding used to leave the suite green.

    README documents /api/profiles/<id>/cdp as a public integration surface;
    a broken pump makes connect_over_cdp() hang at the handshake with no
    server-side error.
    """
    import types

    create = app_client.post("/api/profiles", json={"name": "CdpPump"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    fake = _FakeCdpSocket()
    fake_module = types.SimpleNamespace(connect=lambda *_a, **_k: fake)
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake_module)

    version = MagicMock()
    version.json.return_value = {"webSocketDebuggerUrl": "ws://127.0.0.1:5100/devtools/browser/x"}
    client = MagicMock()
    client.get = AsyncMock(return_value=version)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: client)

    try:
        with app_client.websocket_connect(f"/api/profiles/{pid}/cdp") as ws:
            ws.send_text('{"id":1,"method":"Target.getTargets"}')
            fake.outbound.put_nowait('{"id":1,"result":{}}')
            assert ws.receive_text() == '{"id":1,"result":{}}'
        assert '{"id":1,"method":"Target.getTargets"}' in fake.received
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_cdp_page_proxy_targets_the_requested_devtools_path(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    import types

    create = app_client.post("/api/profiles", json={"name": "CdpPage"})
    pid = create.json()["id"]
    _mock_running_profile(pid)

    fake = _FakeCdpSocket()
    urls: list[str] = []

    def connect(url, **_k):
        urls.append(url)
        return fake

    monkeypatch.setitem(
        __import__("sys").modules, "websockets", types.SimpleNamespace(connect=connect),
    )
    try:
        with app_client.websocket_connect(
            f"/api/profiles/{pid}/cdp/devtools/page/ABC",
        ) as ws:
            ws.send_bytes(b"\x01\x02")
            fake.outbound.put_nowait(b"\x03")
            assert ws.receive_bytes() == b"\x03"
        assert urls == ["ws://127.0.0.1:5100/devtools/page/ABC"]
        assert b"\x01\x02" in fake.received
    finally:
        main.browser_mgr.running.pop(pid, None)


# ── Headless profiles have no display, no Xvnc and no viewer ─────────────────


def _mock_headless_running_profile(pid: str) -> MagicMock:
    """A running headless profile: no display, no ws_port."""
    mock = _mock_running_profile(pid)
    mock.display = None
    mock.ws_port = None
    # spec=RunningProfile does not expose dataclass fields that have no
    # class-level default, so `context` has to be attached by hand.
    mock.context = MagicMock()
    return mock


def test_headless_profile_reports_no_display_or_vnc_port(app_client: TestClient):
    """A headless profile has no X server, so it must not advertise one.

    Reporting a display it does not own is what made the UI offer a viewer
    affordance onto an empty root window.
    """
    create = app_client.post(
        "/api/profiles", json={"name": "Headless", "headless": True},
    )
    pid = create.json()["id"]
    _mock_headless_running_profile(pid)
    try:
        body = app_client.get(f"/api/profiles/{pid}/status").json()
        assert body["status"] == "running"
        assert body["display"] is None
        assert body["vnc_ws_port"] is None
        assert body["xvnc_alive"] is None      # nothing to be alive
        listed = app_client.get("/api/profiles").json()
        entry = next(p for p in listed if p["id"] == pid)
        assert entry["status"] == "running"
        assert entry["vnc_ws_port"] is None
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_headless_profile_refuses_a_viewer_token(app_client: TestClient):
    """409, not 404: the profile IS running, it just cannot be viewed.

    404 routes the viewer's state machine to endSession("Browser session
    ended"), which claims the browser died when it is running perfectly well.
    """
    create = app_client.post(
        "/api/profiles", json={"name": "HeadlessTok", "headless": True},
    )
    pid = create.json()["id"]
    _mock_headless_running_profile(pid)
    try:
        resp = app_client.post(f"/api/profiles/{pid}/viewer-token")
        assert resp.status_code == 409
        assert "headless" in resp.json()["detail"].lower()
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_headless_profile_refuses_the_x_clipboard(app_client: TestClient):
    """No X server means no X clipboard; ":None" as a DISPLAY is not an answer."""
    create = app_client.post(
        "/api/profiles", json={"name": "HeadlessClip", "headless": True},
    )
    pid = create.json()["id"]
    _mock_headless_running_profile(pid)
    try:
        resp = app_client.post(
            f"/api/profiles/{pid}/clipboard", json={"text": "hello"},
        )
        assert resp.status_code == 409
        # The read side degrades instead of erroring: the Playwright leg is
        # still valid headless, only the xclip fallback is unreachable.
        mock = main.browser_mgr.running[pid]
        mock.context.pages = []
        assert app_client.get(f"/api/profiles/{pid}/clipboard").json() == {"text": ""}
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_headless_launch_returns_200_with_null_display(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """LaunchResponse must accept the nulls a headless launch produces.

    vnc_ws_port and display were non-nullable, so a headless launch raised
    ResponseValidationError AFTER the browser had already started: the caller
    saw 500 for an operation that succeeded, and the retry then answered 409.
    """
    create = app_client.post(
        "/api/profiles", json={"name": "HeadlessLaunch", "headless": True},
    )
    pid = create.json()["id"]

    async def fake_launch(_profile):
        return _mock_headless_running_profile(pid)

    monkeypatch.setattr(main.browser_mgr, "launch", fake_launch)
    try:
        resp = app_client.post(f"/api/profiles/{pid}/launch")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "running"
        assert body["vnc_ws_port"] is None
        assert body["display"] is None
        assert body["cdp_url"] == f"/api/profiles/{pid}/cdp"
    finally:
        main.browser_mgr.running.pop(pid, None)


# ── SPA catch-all containment ────────────────────────────────────────────────


def test_spa_serves_a_real_asset(tmp_path: pathlib.Path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    asset = dist / "assets" / "main.js"
    asset.write_text("console.log(1)")
    assert main._resolve_spa_file(dist, "assets/main.js") == asset.resolve()


def test_spa_refuses_to_escape_the_build_directory(tmp_path: pathlib.Path):
    """`base / full_path` discards the base when full_path is absolute.

    The catch-all is not behind the auth middleware (that gates only /api/*),
    and nginx forwards a percent-encoded %2f without decoding it, so an
    unauthenticated GET /%2fdata/profiles.db arrived here as "/data/profiles.db"
    and returned the SQLite profile database.

    Every attack below targets a file that REALLY EXISTS, created here rather
    than assumed present on the host — otherwise the vulnerable implementation
    also returns None (via is_file()) and the test passes for the wrong reason.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")
    secret = tmp_path / "profiles.db"
    secret.write_text("SQLite format 3\x00")

    # absolute — the case Path.__truediv__ silently swallows
    assert main._resolve_spa_file(dist, str(secret)) is None
    # traversal to the same real file, relative to the build dir
    assert main._resolve_spa_file(dist, "../profiles.db") is None
    assert main._resolve_spa_file(dist, "a/../../profiles.db") is None
    assert main._resolve_spa_file(dist, "..") is None
    # and the containment rule holds for a genuine absolute system path
    assert main._resolve_spa_file(dist, "/etc/passwd") is None


def test_spa_refuses_a_symlink_pointing_outside_the_build(tmp_path: pathlib.Path):
    """resolve() follows links, so a link planted in the build cannot escape."""
    dist = tmp_path / "dist"
    dist.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    (dist / "leak").symlink_to(secret)
    assert main._resolve_spa_file(dist, "leak") is None


def test_spa_falls_through_to_index_for_client_routes(tmp_path: pathlib.Path):
    """A real SPA route is not a file, and must not 404."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")
    assert main._resolve_spa_file(dist, "profiles/abc") is None
    assert main._resolve_spa_file(dist, "") is None
