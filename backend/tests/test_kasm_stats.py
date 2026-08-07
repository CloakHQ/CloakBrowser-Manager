"""Tests for the KasmVNC management-API proxies: /kasm-stats and /viewer-attached.

Both endpoints exist to read state out of Xvnc's HTTP layer without inheriting
its two traps: /api/get_sessions never clears once populated, and
/api/get_frame_stats blocks for 10s and then 503s whenever no encoded frame is
produced. The tests below pin the behaviour that avoids each.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from backend import main
from backend.browser_manager import RunningProfile

# The exact bodies Kasm returns, captured from a live 1.5.0 session. `-nan` is
# not valid JSON, so a probe that only tries resp.json() sees an unparsable
# body and would report "unknown" for a genuinely attached viewer.
BOTTLENECK_EMPTY = "{\n}\n"
BOTTLENECK_ONE_CLIENT = (
    '{\n\t"manager": {\n\t\t"127.0.0.1_1785507350.707677::websocket": '
    "[ -nan, -nan, -nan, -nan ]\n\t}\n}\n"
)
BOTTLENECK_TWO_CLIENTS = (
    '{\n\t"manager": {\n\t\t"127.0.0.1_1785507350.707677::websocket": '
    '[ 1, 2, 3, 4 ],\n\t\t"127.0.0.1_1785507399.111111::websocket": '
    "[ 1, 2, 3, 4 ]\n\t}\n}\n"
)
# Verified live: identical payload before and after the viewer disconnected.
SESSIONS_STALE = {"users": [{"username": "manager", "connected_since": "2026-07-31 14:15:50"}]}


def _running(pid: str) -> MagicMock:
    mock = MagicMock(spec=RunningProfile)
    mock.display = 100
    mock.ws_port = 6100
    mock.cdp_port = 5100
    mock.profile_id = pid
    mock.proc = None
    mock.session_epoch = "epoch-under-test"
    main.browser_mgr.running[pid] = mock
    return mock


def _text_response(status: int, body: str) -> MagicMock:
    """A response whose body is raw Kasm text (json() fails on `-nan`)."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    resp.json.side_effect = ValueError("not json")
    return resp


def _json_response(status: int, payload) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _client(*responses) -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    mock.get = AsyncMock(side_effect=list(responses))
    return mock


def _make_profile(app_client: TestClient, name: str) -> str:
    pid = app_client.post("/api/profiles", json={"name": name}).json()["id"]
    _running(pid)
    return pid


# ── /kasm-stats: the frame-stats gate ────────────────────────────────────────


def test_stale_sessions_do_not_trigger_frame_stats(app_client: TestClient):
    """The real post-disconnect state: sessions stale-populated, bottleneck empty.

    Gating on sessions made get_frame_stats fire on every poll for the rest of
    the profile's life, each one burning the client's read timeout while Kasm's
    handler thread spun for the full 10s behind it.
    """
    pid = _make_profile(app_client, "StatsStaleSessions")
    mock_client = _client(
        _text_response(200, BOTTLENECK_EMPTY),
        _json_response(200, SESSIONS_STALE),
    )

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = app_client.get(f"/api/profiles/{pid}/kasm-stats")
        assert resp.status_code == 200
        assert resp.json()["frame"] is None
        assert mock_client.get.await_count == 2
        assert not any(
            "get_frame_stats" in call.args[0] for call in mock_client.get.await_args_list
        )
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_frame_stats_runs_when_a_viewer_is_really_attached(app_client: TestClient):
    pid = _make_profile(app_client, "StatsLiveViewer")
    mock_client = _client(
        _text_response(200, BOTTLENECK_ONE_CLIENT),
        _json_response(200, SESSIONS_STALE),
        _json_response(200, {"clients": {"all": {"fps": 30}}}),
    )

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = app_client.get(f"/api/profiles/{pid}/kasm-stats")
        assert resp.status_code == 200
        assert resp.json()["frame"] == {"clients": {"all": {"fps": 30}}}
        assert "get_frame_stats" in mock_client.get.await_args_list[2].args[0]
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_frame_stats_carries_its_own_short_timeout(app_client: TestClient):
    """A static screen makes this request hang by design — bound it explicitly.

    Without a per-request timeout it falls back to the shared client's 5s, so
    every stats poll on an idle profile costs 5s instead of 1.5s.
    """
    pid = _make_profile(app_client, "StatsFrameTimeout")
    mock_client = _client(
        _text_response(200, BOTTLENECK_ONE_CLIENT),
        _json_response(200, SESSIONS_STALE),
        _json_response(200, {}),
    )

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):
            app_client.get(f"/api/profiles/{pid}/kasm-stats")
        frame_call = mock_client.get.await_args_list[2]
        assert "get_frame_stats" in frame_call.args[0]
        assert frame_call.kwargs["timeout"] == main.KASM_FRAME_STATS_TIMEOUT_S
        assert main.KASM_FRAME_STATS_TIMEOUT_S <= 2
    finally:
        main.browser_mgr.running.pop(pid, None)


# ── _viewer_attached: the classifier ─────────────────────────────────────────


def test_viewer_attached_classifier_distinguishes_empty_from_unknown():
    assert main._viewer_attached({}) is False
    assert main._viewer_attached({"manager": {"a::websocket": []}}) is True
    # Unparsable/absent answers must never masquerade as "no viewer".
    assert main._viewer_attached(None) is None
    assert main._viewer_attached("<html>401</html>") is None


def test_viewer_client_count_counts_every_socket():
    assert main._viewer_client_count({}) == 0
    assert main._viewer_client_count({"manager": {"a": [], "b": []}}) == 2
    assert main._viewer_client_count(None) is None


# ── /viewer-attached ─────────────────────────────────────────────────────────


def test_viewer_attached_not_running(app_client: TestClient):
    assert app_client.get("/api/profiles/nope/viewer-attached").status_code == 404


def test_viewer_attached_true_for_a_live_socket(app_client: TestClient):
    """The populated form carries `-nan`, which plain json() cannot parse."""
    pid = _make_profile(app_client, "ViewerLive")
    mock_client = _client(_text_response(200, BOTTLENECK_ONE_CLIENT))

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = app_client.get(f"/api/profiles/{pid}/viewer-attached")
        assert resp.status_code == 200
        assert resp.json() == {"viewer_attached": True, "clients": 1}
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_viewer_attached_counts_multiple_sockets(app_client: TestClient):
    """-AlwaysShared allows several viewers; each is its own peerEndpoint key."""
    pid = _make_profile(app_client, "ViewerTwo")
    mock_client = _client(_text_response(200, BOTTLENECK_TWO_CLIENTS))

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = app_client.get(f"/api/profiles/{pid}/viewer-attached")
        assert resp.json() == {"viewer_attached": True, "clients": 2}
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_viewer_attached_false_after_disconnect(app_client: TestClient):
    """An empty object is definitive — this is the whole point of the endpoint."""
    pid = _make_profile(app_client, "ViewerGone")
    mock_client = _client(_text_response(200, BOTTLENECK_EMPTY))

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = app_client.get(f"/api/profiles/{pid}/viewer-attached")
        assert resp.json() == {"viewer_attached": False, "clients": 0}
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_viewer_attached_unknown_on_upstream_error(app_client: TestClient):
    """A 401 from the stats API says nothing about the viewer socket."""
    pid = _make_profile(app_client, "ViewerAuthFail")
    mock_client = _client(_text_response(401, "<html>401 Unauthorized</html>"))

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = app_client.get(f"/api/profiles/{pid}/viewer-attached")
        assert resp.status_code == 200
        assert resp.json() == {"viewer_attached": None, "clients": None}
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_viewer_attached_unknown_when_unreachable(app_client: TestClient):
    """Never 5xx: the caller is a heartbeat that must tolerate a blip."""
    pid = _make_profile(app_client, "ViewerDown")
    mock_client = _client(ConnectionError("refused"))

    try:
        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = app_client.get(f"/api/profiles/{pid}/viewer-attached")
        assert resp.status_code == 200
        assert resp.json() == {"viewer_attached": None, "clients": None}
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_viewer_attached_probe_is_bounded(app_client: TestClient):
    """Kasm answers in ~3ms; an unbounded probe would stall the heartbeat."""
    pid = _make_profile(app_client, "ViewerBounded")
    mock_client = _client(_text_response(200, BOTTLENECK_EMPTY))

    try:
        with patch("httpx.AsyncClient", return_value=mock_client) as factory:
            app_client.get(f"/api/profiles/{pid}/viewer-attached")
        timeout = factory.call_args.kwargs["timeout"]
        assert timeout == main.KASM_VIEWER_PROBE_TIMEOUT_S
        assert 0 < timeout <= 2
        # Exactly one request — no get_sessions, no frame stats.
        assert mock_client.get.await_count == 1
        assert "get_bottleneck_stats" in mock_client.get.await_args_list[0].args[0]
    finally:
        main.browser_mgr.running.pop(pid, None)


def test_viewer_attached_uses_owner_credentials(app_client: TestClient, monkeypatch):
    """Kasm 401s /api without them, which would degrade the answer to unknown."""
    pid = _make_profile(app_client, "ViewerCreds")
    monkeypatch.setattr(
        main.browser_mgr.vnc, "get_api_credentials", lambda _d: ("manager", "pw123"),
    )
    mock_client = _client(_text_response(200, BOTTLENECK_EMPTY))

    try:
        with patch("httpx.AsyncClient", return_value=mock_client) as factory:
            app_client.get(f"/api/profiles/{pid}/viewer-attached")
        assert factory.call_args.kwargs["auth"] == ("manager", "pw123")
    finally:
        main.browser_mgr.running.pop(pid, None)
