"""Tests for the idle-timeout reaper and CDP-hit auto-launch.

reap_idle_profiles combines two signals a running profile can be idle on: a
VNC viewer probed fresh every pass (that traffic never reaches this process —
nginx proxies straight to Kasm) and CDP traffic touched directly by the proxy.
_ensure_running_for_cdp is the safeguard that launches a profile on its first
CDP hit if nothing launched it first.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from backend import browser_manager as bm
from backend import main
from backend.browser_manager import BrowserManager, RunningProfile

# ── reap_idle_profiles / touch_activity ──────────────────────────────────────


def _idle_running(profile_id: str, idle_timeout_seconds: int, idle_for: float) -> RunningProfile:
    context = MagicMock()
    context.is_closed.return_value = False
    running = bm.RunningProfile(
        profile_id=profile_id, context=context, display=None, ws_port=None,
        cdp_port=5100, idle_timeout_seconds=idle_timeout_seconds,
    )
    running.last_active = time.monotonic() - idle_for
    return running


@pytest.mark.asyncio
async def test_reap_idle_profiles_stops_profile_past_timeout(monkeypatch):
    mgr = BrowserManager()
    mgr.running["p1"] = _idle_running("p1", idle_timeout_seconds=60, idle_for=120)
    monkeypatch.setattr(mgr, "_probe_viewer_activity", AsyncMock())
    stop = AsyncMock()
    monkeypatch.setattr(mgr, "stop", stop)

    await mgr.reap_idle_profiles()

    stop.assert_awaited_once_with("p1")


@pytest.mark.asyncio
async def test_reap_idle_profiles_leaves_fresh_profile_running(monkeypatch):
    mgr = BrowserManager()
    mgr.running["p1"] = _idle_running("p1", idle_timeout_seconds=60, idle_for=5)
    monkeypatch.setattr(mgr, "_probe_viewer_activity", AsyncMock())
    stop = AsyncMock()
    monkeypatch.setattr(mgr, "stop", stop)

    await mgr.reap_idle_profiles()

    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_reap_idle_profiles_skips_profiles_with_timeout_disabled(monkeypatch):
    """0 means "never idle-timeout" — must not even probe for activity."""
    mgr = BrowserManager()
    mgr.running["p1"] = _idle_running("p1", idle_timeout_seconds=0, idle_for=10_000)
    probe = AsyncMock()
    monkeypatch.setattr(mgr, "_probe_viewer_activity", probe)
    stop = AsyncMock()
    monkeypatch.setattr(mgr, "stop", stop)

    await mgr.reap_idle_profiles()

    stop.assert_not_awaited()
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_reap_idle_profiles_saved_by_viewer_activity_in_same_pass(monkeypatch):
    """A viewer probe result must count for THIS pass, not just the next one."""
    mgr = BrowserManager()
    running = _idle_running("p1", idle_timeout_seconds=60, idle_for=120)
    mgr.running["p1"] = running

    async def fake_probe(r):
        mgr.touch_activity(r.profile_id)

    monkeypatch.setattr(mgr, "_probe_viewer_activity", fake_probe)
    stop = AsyncMock()
    monkeypatch.setattr(mgr, "stop", stop)

    await mgr.reap_idle_profiles()

    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_reap_idle_profiles_survives_one_bad_probe(monkeypatch):
    """One profile's broken probe/stop must not strand the rest of the sweep."""
    mgr = BrowserManager()
    mgr.running["bad"] = _idle_running("bad", idle_timeout_seconds=60, idle_for=120)
    mgr.running["good"] = _idle_running("good", idle_timeout_seconds=60, idle_for=120)

    async def flaky_probe(r):
        if r.profile_id == "bad":
            raise RuntimeError("kasm unreachable")

    monkeypatch.setattr(mgr, "_probe_viewer_activity", flaky_probe)
    stop = AsyncMock()
    monkeypatch.setattr(mgr, "stop", stop)

    await mgr.reap_idle_profiles()

    stop.assert_any_await("bad")
    stop.assert_any_await("good")


def test_touch_activity_resets_idle_clock():
    mgr = BrowserManager()
    running = _idle_running("p1", idle_timeout_seconds=60, idle_for=120)
    mgr.running["p1"] = running
    before = running.last_active

    mgr.touch_activity("p1")

    assert running.last_active > before


def test_touch_activity_is_a_no_op_for_an_unknown_profile():
    mgr = BrowserManager()
    mgr.touch_activity("nope")  # must not raise


# ── _probe_viewer_activity ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_viewer_activity_touches_on_attached_client():
    mgr = BrowserManager()
    running = _idle_running("p1", idle_timeout_seconds=60, idle_for=120)
    running.display = 100
    running.ws_port = 6100
    mgr.running["p1"] = running  # touch_activity looks the profile up by id

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"manager": {"a::websocket": [1, 2, 3, 4]}}
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)

    with patch("httpx.AsyncClient", return_value=client):
        await mgr._probe_viewer_activity(running)

    assert time.monotonic() - running.last_active < 1


@pytest.mark.asyncio
async def test_probe_viewer_activity_no_touch_when_no_viewer():
    mgr = BrowserManager()
    running = _idle_running("p1", idle_timeout_seconds=60, idle_for=120)
    running.display = 100
    running.ws_port = 6100
    stale = running.last_active

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {}
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)

    with patch("httpx.AsyncClient", return_value=client):
        await mgr._probe_viewer_activity(running)

    assert running.last_active == stale


@pytest.mark.asyncio
async def test_probe_viewer_activity_skips_headless_profiles():
    """No display/ws_port means no Xvnc to probe at all."""
    mgr = BrowserManager()
    running = _idle_running("p1", idle_timeout_seconds=60, idle_for=120)
    running.display = None
    running.ws_port = None
    stale = running.last_active

    await mgr._probe_viewer_activity(running)

    assert running.last_active == stale


# ── CDP-hit auto-launch ───────────────────────────────────────────────────────


def _fake_launch_factory(pid: str):
    async def fake_launch(profile):
        mock = MagicMock(spec=RunningProfile)
        mock.display = 100
        mock.ws_port = 6100
        mock.cdp_port = 5100
        mock.profile_id = pid
        mock.proc = None
        mock.session_epoch = "epoch-under-test"
        main.browser_mgr.running[pid] = mock
        return mock

    return fake_launch


def _mock_chrome_json_version_client() -> AsyncMock:
    chrome_response = MagicMock()
    chrome_response.json.return_value = {
        "webSocketDebuggerUrl": "ws://127.0.0.1:5100/devtools/browser/abc",
    }
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=chrome_response)
    return client


def test_cdp_info_404_for_a_profile_id_that_does_not_exist(app_client: TestClient):
    resp = app_client.get("/api/profiles/00000000-0000-0000-0000-000000000000/cdp")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Profile not found"


def test_cdp_json_version_auto_launches_a_stopped_profile(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    create = app_client.post("/api/profiles", json={"name": "AutoLaunch"})
    pid = create.json()["id"]
    assert pid not in main.browser_mgr.running

    monkeypatch.setattr(main.browser_mgr, "launch", _fake_launch_factory(pid))
    client = _mock_chrome_json_version_client()

    try:
        with patch("httpx.AsyncClient", return_value=client):
            resp = app_client.get(f"/api/profiles/{pid}/cdp/json/version")
        assert resp.status_code == 200
        assert pid in main.browser_mgr.running
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_cdp_json_version_502_when_auto_launch_fails(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    create = app_client.post("/api/profiles", json={"name": "AutoLaunchFail"})
    pid = create.json()["id"]

    async def failing_launch(profile):
        raise RuntimeError("boom")

    monkeypatch.setattr(main.browser_mgr, "launch", failing_launch)

    resp = app_client.get(f"/api/profiles/{pid}/cdp/json/version")

    assert resp.status_code == 502
    assert pid not in main.browser_mgr.running


def test_cdp_json_version_does_not_relaunch_an_already_running_profile(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    create = app_client.post("/api/profiles", json={"name": "AlreadyUp"})
    pid = create.json()["id"]
    mock = MagicMock(spec=RunningProfile)
    mock.display = 100
    mock.ws_port = 6100
    mock.cdp_port = 5100
    mock.profile_id = pid
    main.browser_mgr.running[pid] = mock

    launch = AsyncMock()
    monkeypatch.setattr(main.browser_mgr, "launch", launch)
    client = _mock_chrome_json_version_client()

    try:
        with patch("httpx.AsyncClient", return_value=client):
            resp = app_client.get(f"/api/profiles/{pid}/cdp/json/version")
        assert resp.status_code == 200
        launch.assert_not_awaited()
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_cdp_ws_proxy_auto_launches_a_stopped_profile(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    import types

    create = app_client.post("/api/profiles", json={"name": "AutoLaunchWs"})
    pid = create.json()["id"]
    monkeypatch.setattr(main.browser_mgr, "launch", _fake_launch_factory(pid))

    version = MagicMock()
    version.json.return_value = {"webSocketDebuggerUrl": "ws://127.0.0.1:5100/devtools/browser/x"}
    client = MagicMock()
    client.get = AsyncMock(return_value=version)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: client)

    class _NullCdpSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def send(self, *_a, **_k):
            pass

    fake_module = types.SimpleNamespace(connect=lambda *_a, **_k: _NullCdpSocket())
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake_module)

    try:
        with app_client.websocket_connect(f"/api/profiles/{pid}/cdp"):
            pass
        assert pid in main.browser_mgr.running
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_cdp_proxy_touches_activity_on_forwarded_frames(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """A real CDP command must reset the idle clock the reaper reads."""
    import types

    create = app_client.post("/api/profiles", json={"name": "CdpTouch"})
    pid = create.json()["id"]
    running = bm.RunningProfile(
        profile_id=pid, context=MagicMock(), display=100, ws_port=6100, cdp_port=5100,
    )
    running.last_active = time.monotonic() - 10_000
    main.browser_mgr.running[pid] = running

    version = MagicMock()
    version.json.return_value = {"webSocketDebuggerUrl": "ws://127.0.0.1:5100/devtools/browser/x"}
    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=version)
    http_client.__aenter__ = AsyncMock(return_value=http_client)
    http_client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: http_client)

    class _EchoCdpSocket:
        def __init__(self):
            self.outbound = __import__("asyncio").Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.outbound.get()

        async def send(self, msg):
            await self.outbound.put(msg)

    fake = _EchoCdpSocket()
    monkeypatch.setitem(
        __import__("sys").modules, "websockets",
        types.SimpleNamespace(connect=lambda *_a, **_k: fake),
    )

    try:
        with app_client.websocket_connect(f"/api/profiles/{pid}/cdp") as ws:
            ws.send_text('{"id":1,"method":"Target.getTargets"}')
            assert ws.receive_text() == '{"id":1,"method":"Target.getTargets"}'
        assert time.monotonic() - running.last_active < 5
    finally:
        main.browser_mgr.running.pop(pid, None)
