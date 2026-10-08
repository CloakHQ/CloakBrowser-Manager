"""Tests for capturing files the profile's browser downloads."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from unittest.mock import MagicMock
from starlette.testclient import TestClient

from backend import artifacts
from backend import database as db
from backend import downloads
from backend import main
from backend.browser_manager import RunningProfile


@pytest.fixture()
def profile(tmp_db: Path) -> dict:
    return db.create_profile(name="Downloader")


def _stage(profile_id: str, guid: str, payload: bytes) -> Path:
    """Stand in for Chromium writing a GUID-named file into the staging directory."""
    staging = downloads.staging_dir(profile_id)
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / guid
    path.write_bytes(payload)
    return path


def test_a_started_download_is_recorded_as_pending(profile: dict):
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "weekly report.csv")

    row = db.get_artifact(profile["id"], artifact_id)
    assert row["state"] == "pending"
    assert row["kind"] == "download"
    assert row["name"] == "weekly report.csv"
    assert row["size"] == 0


def test_a_suggested_filename_cannot_escape_the_artifact_directory(profile: dict):
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "../../../../etc/passwd")
    assert db.get_artifact(profile["id"], artifact_id)["name"] == "passwd"


def test_a_nameless_download_still_gets_a_name(profile: dict):
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", None)
    assert db.get_artifact(profile["id"], artifact_id)["name"] == "download"


def test_a_completed_download_is_published_with_its_real_size(profile: dict):
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "orders.csv")
    _stage(profile["id"], "guid-1", b"store_id,orders\n1,2\n")

    tracker.progress("guid-1", "completed", received_bytes=999)

    row = db.get_artifact(profile["id"], artifact_id)
    assert row["state"] == "ready"
    # Size comes from the bytes on disk, not from the browser's claim.
    assert row["size"] == len(b"store_id,orders\n1,2\n")
    stored = artifacts.artifact_path(profile["id"], artifact_id, "orders.csv")
    assert stored.read_bytes() == b"store_id,orders\n1,2\n"
    # The staging copy is gone; the file lives at the canonical path only.
    assert not (downloads.staging_dir(profile["id"]) / "guid-1").exists()


def test_an_in_progress_download_is_not_published(profile: dict):
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "big.csv")
    _stage(profile["id"], "guid-1", b"partial")

    tracker.progress("guid-1", "inProgress", received_bytes=7)

    assert db.get_artifact(profile["id"], artifact_id)["state"] == "pending"
    assert not artifacts.artifact_path(profile["id"], artifact_id, "big.csv").exists()


def test_a_download_that_landed_elsewhere_is_not_recorded_at_all(profile: dict):
    """Another CDP client can re-point the download path — Playwright's connect_over_cdp
    does exactly that — while its events still arrive here. Those downloads are not ours,
    and keeping a failed row for each would fill the profile with phantom artifacts."""
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "someone-elses.csv")

    tracker.progress("guid-1", "completed", received_bytes=10)

    assert db.get_artifact(profile["id"], artifact_id) is None
    assert db.list_artifacts(profile["id"]) == []
    assert not artifacts.artifact_path(profile["id"], artifact_id, "someone-elses.csv").exists()


def test_a_phantom_download_does_not_consume_the_profile_quota(profile: dict):
    """The size a foreign download reports must never be charged to this profile."""
    tracker = downloads.DownloadTracker(profile["id"])
    for n in range(5):
        tracker.begin(f"guid-{n}", f"elsewhere-{n}.csv")
        tracker.progress(f"guid-{n}", "completed", received_bytes=500 * 1024 * 1024)

    assert db.list_artifacts(profile["id"]) == []
    artifacts.check_quota(profile["id"], 1024)  # would raise if the phantoms were counted


def test_a_cancelled_download_leaves_nothing_behind(profile: dict):
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "aborted.csv")
    _stage(profile["id"], "guid-1", b"half")

    tracker.progress("guid-1", "canceled", received_bytes=4)

    assert db.get_artifact(profile["id"], artifact_id) is None
    assert not (downloads.staging_dir(profile["id"]) / "guid-1").exists()
    assert db.list_artifacts(profile["id"]) == []


def test_progress_for_an_unknown_download_is_ignored(profile: dict):
    downloads.DownloadTracker(profile["id"]).progress("never-seen", "completed", 1)
    assert db.list_artifacts(profile["id"]) == []


def test_downloads_are_refused_once_the_profile_is_at_its_cap(
    profile: dict, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACTS_PER_PROFILE", 1)
    tracker = downloads.DownloadTracker(profile["id"])
    assert tracker.begin("guid-1", "one.csv") is not None
    assert tracker.begin("guid-2", "two.csv") is None
    assert len(db.list_artifacts(profile["id"])) == 1


def test_a_published_download_shows_up_in_the_file_chooser_view(profile: dict, docker_runtime):
    tracker = downloads.DownloadTracker(profile["id"])
    tracker.begin("guid-1", "payout.csv")
    _stage(profile["id"], "guid-1", b"x")
    tracker.progress("guid-1", "completed", 1)

    link = artifacts.picker_dir(profile["id"]) / "payout.csv"
    assert link.is_symlink() and link.read_bytes() == b"x"


def test_handle_event_routes_cdp_messages(profile: dict):
    tracker = downloads.DownloadTracker(profile["id"])
    downloads.handle_event(tracker, {
        "method": "Browser.downloadWillBegin",
        "params": {"guid": "g", "suggestedFilename": "report.xlsx", "url": "https://x/y"},
    })
    assert [row["state"] for row in db.list_artifacts(profile["id"])] == ["pending"]

    _stage(profile["id"], "g", b"sheet")
    downloads.handle_event(tracker, {
        "method": "Browser.downloadProgress",
        "params": {"guid": "g", "state": "completed", "receivedBytes": 5, "totalBytes": 5},
    })
    row = db.list_artifacts(profile["id"])[0]
    assert (row["state"], row["size"], row["name"]) == ("ready", 5, "report.xlsx")


def test_handle_event_ignores_unrelated_methods(profile: dict):
    downloads.handle_event(downloads.DownloadTracker(profile["id"]), {"method": "Page.loadEventFired"})
    assert db.list_artifacts(profile["id"]) == []


# ── API surface ──────────────────────────────────────────────────────────────


def test_a_download_in_flight_is_listed_but_not_servable(app_client: TestClient, tmp_db: Path):
    pid = app_client.post("/api/profiles", json={"name": "Downloader"}).json()["id"]
    artifact_id = downloads.DownloadTracker(pid).begin("guid-1", "in-flight.csv")

    listed = app_client.get(f"/api/profiles/{pid}/files").json()
    assert [(a["name"], a["state"], a["kind"]) for a in listed] == [("in-flight.csv", "pending", "download")]

    resp = app_client.get(f"/api/profiles/{pid}/files/{artifact_id}")
    assert resp.status_code == 409
    assert "still in progress" in resp.json()["detail"]


def test_a_download_over_the_size_cap_is_refused_at_publication(
    profile: dict, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 8)
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "huge.csv")
    _stage(profile["id"], "guid-1", b"x" * 64)

    tracker.progress("guid-1", "completed", received_bytes=64)

    assert db.get_artifact(profile["id"], artifact_id)["state"] == "failed"
    assert not (downloads.staging_dir(profile["id"]) / "guid-1").exists()
    assert not artifacts.artifact_path(profile["id"], artifact_id, "huge.csv").exists()


def test_a_runaway_download_is_cancelled_mid_flight(
    profile: dict, monkeypatch: pytest.MonkeyPatch
):
    """Declining to record it would not stop Chromium filling the disk."""
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 8)
    cancelled: list[str] = []
    tracker = downloads.DownloadTracker(profile["id"], cancel=cancelled.append)
    artifact_id = tracker.begin("guid-1", "huge.csv")
    _stage(profile["id"], "guid-1", b"x" * 64)

    tracker.progress("guid-1", "inProgress", received_bytes=64)

    assert cancelled == ["guid-1"]
    assert db.get_artifact(profile["id"], artifact_id)["state"] == "failed"
    assert not (downloads.staging_dir(profile["id"]) / "guid-1").exists()


def test_an_in_progress_download_under_the_cap_is_left_alone(profile: dict):
    cancelled: list[str] = []
    tracker = downloads.DownloadTracker(profile["id"], cancel=cancelled.append)
    artifact_id = tracker.begin("guid-1", "fine.csv")

    tracker.progress("guid-1", "inProgress", received_bytes=1024)

    assert cancelled == []
    assert db.get_artifact(profile["id"], artifact_id)["state"] == "pending"


def test_downloads_interrupted_by_a_restart_are_failed_not_left_pending(profile: dict):
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "half.csv")
    _stage(profile["id"], "guid-1", b"partial")

    # A new browser cannot resume the old one's transfers.
    assert downloads.reconcile_interrupted(profile["id"]) == 1

    assert db.get_artifact(profile["id"], artifact_id)["state"] == "failed"
    assert list(downloads.staging_dir(profile["id"]).iterdir()) == []


def test_reconcile_leaves_finished_downloads_alone(profile: dict):
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "done.csv")
    _stage(profile["id"], "guid-1", b"ok")
    tracker.progress("guid-1", "completed", 2)

    assert downloads.reconcile_interrupted(profile["id"]) == 0
    assert db.get_artifact(profile["id"], artifact_id)["state"] == "ready"


def test_a_refused_download_is_cancelled_once_staging_proves_it_is_ours(
    profile: dict, monkeypatch: pytest.MonkeyPatch
):
    """Declining to record a download does not stop Chromium writing it — but the cancel
    waits for evidence, so another CDP client's transfers are never killed."""
    monkeypatch.setattr(artifacts, "MAX_ARTIFACTS_PER_PROFILE", 1)
    cancelled: list[str] = []
    tracker = downloads.DownloadTracker(profile["id"], cancel=cancelled.append)
    tracker.begin("guid-1", "one.csv")

    assert tracker.begin("guid-2", "refused.csv") is None
    assert cancelled == []  # nothing staged yet: ownership unproven

    _stage(profile["id"], "guid-2", b"partial")
    tracker.progress("guid-2", "inProgress", received_bytes=7)
    assert cancelled == ["guid-2"]

    tracker.progress("guid-2", "canceled", received_bytes=7)
    assert not (downloads.staging_dir(profile["id"]) / "guid-2").exists()
    assert len(db.list_artifacts(profile["id"])) == 1


def test_a_refused_download_belonging_to_another_client_is_left_alone(
    profile: dict, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACTS_PER_PROFILE", 1)
    cancelled: list[str] = []
    tracker = downloads.DownloadTracker(profile["id"], cancel=cancelled.append)
    tracker.begin("guid-1", "one.csv")
    assert tracker.begin("guid-2", "not-ours.csv") is None

    # No staged file ever appears, because it is being written somewhere else entirely.
    tracker.progress("guid-2", "inProgress", received_bytes=1024)

    assert cancelled == []


def test_publishing_does_not_re_claim_the_slot_it_already_holds(
    profile: dict, monkeypatch: pytest.MonkeyPatch
):
    """The count was enforced when the transfer began; re-applying it fails a valid download."""
    monkeypatch.setattr(artifacts, "MAX_ARTIFACTS_PER_PROFILE", 1)
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "only.csv")
    _stage(profile["id"], "guid-1", b"x")

    tracker.progress("guid-1", "completed", received_bytes=1)

    assert db.get_artifact(profile["id"], artifact_id)["state"] == "ready"


def test_a_download_whose_artifact_vanished_drops_its_staged_bytes(profile: dict):
    tracker = downloads.DownloadTracker(profile["id"])
    artifact_id = tracker.begin("guid-1", "orphan.csv")
    _stage(profile["id"], "guid-1", b"bytes")
    db.delete_artifact(profile["id"], artifact_id)

    tracker.progress("guid-1", "completed", received_bytes=5)

    assert not (downloads.staging_dir(profile["id"]) / "guid-1").exists()


def test_a_download_in_flight_cannot_be_deleted_while_the_browser_runs(
    app_client: TestClient, tmp_db: Path
):
    """Removing the row would not stop the browser, and its bytes would land unattached."""
    pid = app_client.post("/api/profiles", json={"name": "Downloader"}).json()["id"]
    artifact_id = downloads.DownloadTracker(pid).begin("guid-1", "in-flight.csv")
    main.browser_mgr.running[pid] = MagicMock(spec=RunningProfile)
    try:
        resp = app_client.delete(f"/api/profiles/{pid}/files/{artifact_id}")
    finally:
        main.browser_mgr.running.pop(pid, None)

    assert resp.status_code == 409
    assert "still in progress" in resp.json()["detail"]
    assert db.get_artifact(pid, artifact_id) is not None


def test_a_pending_download_can_be_cleared_once_the_browser_is_gone(
    app_client: TestClient, tmp_db: Path
):
    """Otherwise a stop mid-transfer leaves a row that says 'downloading' forever."""
    pid = app_client.post("/api/profiles", json={"name": "Downloader"}).json()["id"]
    artifact_id = downloads.DownloadTracker(pid).begin("guid-1", "stranded.csv")

    assert app_client.delete(f"/api/profiles/{pid}/files/{artifact_id}").status_code == 200
    assert db.get_artifact(pid, artifact_id) is None


# ── CDP session plumbing ──────────────────────────────────────────────────────


_CLOSED = object()
_DROPPED = object()


class FakeSocket:
    """Just enough of a websockets connection: sends are recorded, receives are scripted."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def feed(self, message: object) -> None:
        """Queue a message (any JSON value) for the reader."""
        self._incoming.put_nowait(json.dumps(message))

    def close(self) -> None:
        """The browser closes the socket cleanly."""
        self._incoming.put_nowait(_CLOSED)

    def drop(self) -> None:
        """The socket dies mid-stream, the browser still running."""
        self._incoming.put_nowait(_DROPPED)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        raw = await self._incoming.get()
        if raw is _CLOSED:
            raise StopAsyncIteration
        if raw is _DROPPED:
            raise ConnectionError("socket dropped")
        return raw


async def _until(condition, tries: int = 200) -> None:
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


@pytest.mark.asyncio
async def test_a_reply_is_matched_by_id_not_by_arrival_order(profile: dict):
    ws = FakeSocket()
    session = downloads._Session(ws, profile["id"])
    events: list[dict] = []
    session.on_event = events.append
    pump = asyncio.ensure_future(session.pump())
    call = asyncio.ensure_future(session.call("Browser.setDownloadBehavior", {"behavior": "allowAndName"}))
    await _until(lambda: ws.sent)
    # An event squeezes in ahead of the reply; it must not be mistaken for the ack.
    ws.feed({"method": "Browser.downloadWillBegin", "params": {"guid": "g1"}})
    ws.feed({"id": ws.sent[0]["id"], "result": {"ok": True}})
    assert await call == {"ok": True}
    assert events == [{"method": "Browser.downloadWillBegin", "params": {"guid": "g1"}}]
    ws.close()
    await pump


@pytest.mark.asyncio
async def test_a_refused_call_raises(profile: dict):
    ws = FakeSocket()
    session = downloads._Session(ws, profile["id"])
    pump = asyncio.ensure_future(session.pump())
    call = asyncio.ensure_future(session.call("Browser.setDownloadBehavior"))
    await _until(lambda: ws.sent)
    ws.feed({"id": ws.sent[0]["id"], "error": {"code": -32000, "message": "nope"}})
    with pytest.raises(RuntimeError, match="nope"):
        await call
    ws.close()
    await pump


@pytest.mark.asyncio
async def test_a_call_fails_fast_when_the_socket_closes(profile: dict):
    ws = FakeSocket()
    session = downloads._Session(ws, profile["id"])
    pump = asyncio.ensure_future(session.pump())
    call = asyncio.ensure_future(session.call("Browser.setDownloadBehavior"))
    await _until(lambda: ws.sent)
    ws.close()
    await pump
    with pytest.raises(ConnectionError):
        await call


@pytest.mark.asyncio
async def test_capture_is_re_armed_when_a_cdp_client_detaches(profile: dict, tmp_path: Path):
    ws = FakeSocket()
    rearm = asyncio.Event()
    session = downloads._Session(ws, profile["id"])
    run = asyncio.ensure_future(downloads._run_capture(session, tmp_path, rearm, profile["id"]))
    await _until(lambda: len(ws.sent) == 1)
    ws.feed({"id": ws.sent[0]["id"], "result": {}})  # Chromium accepts the initial arm
    rearm.set()  # the proxy saw an external client disconnect
    await _until(lambda: len(ws.sent) == 2)
    assert ws.sent[1]["method"] == "Browser.setDownloadBehavior"
    assert ws.sent[1]["params"] == {
        "behavior": "allowAndName", "downloadPath": str(tmp_path), "eventsEnabled": True,
    }
    assert not rearm.is_set()
    ws.feed({"id": ws.sent[1]["id"], "result": {}})
    ws.close()  # the browser goes away
    await run  # a clean close returns without raising


@pytest.mark.asyncio
async def test_a_refused_re_arm_drops_the_session_so_it_reconnects(profile: dict, tmp_path: Path):
    ws = FakeSocket()
    rearm = asyncio.Event()
    session = downloads._Session(ws, profile["id"])
    run = asyncio.ensure_future(downloads._run_capture(session, tmp_path, rearm, profile["id"]))
    await _until(lambda: len(ws.sent) == 1)
    ws.feed({"id": ws.sent[0]["id"], "result": {}})
    rearm.set()
    await _until(lambda: len(ws.sent) == 2)
    ws.feed({"id": ws.sent[1]["id"], "error": {"code": -32000, "message": "not now"}})
    with pytest.raises(RuntimeError, match="not now"):
        await run


@pytest.mark.asyncio
async def test_a_cancel_the_browser_refuses_is_reported(profile: dict, caplog):
    class Refusing:
        async def call(self, method: str, params: dict | None = None) -> dict:
            raise RuntimeError("Browser.cancelDownload refused: no such download")

    cancel = downloads._cancel_via(Refusing(), profile["id"])
    with caplog.at_level(logging.WARNING, logger="cloakbrowser.manager.downloads"):
        cancel("guid-9")
        for _ in range(3):
            await asyncio.sleep(0)
    assert "could not cancel download guid-9" in caplog.text


@pytest.mark.asyncio
async def test_malformed_messages_do_not_end_capture(profile: dict):
    ws = FakeSocket()
    session = downloads._Session(ws, profile["id"])
    events: list[dict] = []
    session.on_event = events.append
    pump = asyncio.ensure_future(session.pump())
    call = asyncio.ensure_future(session.call("Browser.setDownloadBehavior"))
    await _until(lambda: ws.sent)
    for junk in ([], None, "text", 7, {"id": [1]}, {"id": "x", "method": "Browser.downloadProgress"}):
        ws.feed(junk)
    ws.feed({"id": ws.sent[0]["id"], "result": {}})
    assert await call == {}  # the reply still found its caller
    # Only objects reach the handler, which ignores what it does not recognise.
    assert events == [{"id": [1]}, {"id": "x", "method": "Browser.downloadProgress"}]
    ws.close()
    await pump


@pytest.mark.asyncio
async def test_a_clean_close_during_a_re_arm_is_not_reported_as_a_drop(profile: dict, tmp_path: Path):
    ws = FakeSocket()
    rearm = asyncio.Event()
    session = downloads._Session(ws, profile["id"])
    run = asyncio.ensure_future(downloads._run_capture(session, tmp_path, rearm, profile["id"]))
    await _until(lambda: len(ws.sent) == 1)
    ws.feed({"id": ws.sent[0]["id"], "result": {}})
    rearm.set()
    await _until(lambda: len(ws.sent) == 2)
    ws.close()  # the browser goes away before answering the re-arm
    assert await run is None  # a clean close, not a ConnectionError


@pytest.mark.asyncio
async def test_a_session_that_armed_earns_a_fresh_reconnect_budget(profile: dict, monkeypatch):
    monkeypatch.setattr(downloads, "_RECONNECT_DELAYS", (0, 0))
    attempts: list[int] = []

    async def session(profile_id, cdp_port, staging, rearm=None, on_armed=None, tracker=None):
        attempts.append(len(attempts) + 1)
        n = attempts[-1]
        if n == 3:
            on_armed()  # armed fine, dropped later
            raise ConnectionError("dropped")
        if n in (1, 2, 4):
            raise ConnectionError("refused")
        # n == 5: closed cleanly

    monkeypatch.setattr(downloads, "_capture_session", session)
    await downloads.watch(profile["id"], 9222)
    # Without the reset, the two-slot budget would have been spent by attempt 3.
    assert attempts == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_download_tracking_survives_a_capture_reconnect(profile: dict, monkeypatch):
    """The capture socket drops mid-transfer while Chromium keeps downloading; the completion
    event arrives on the reconnected socket and must still publish the file."""
    import websockets

    first, second = FakeSocket(), FakeSocket()
    sockets = iter([first, second])

    async def fake_url(cdp_port: int) -> str:
        return "ws://fake"

    monkeypatch.setattr(downloads, "_RECONNECT_DELAYS", (0,))
    monkeypatch.setattr(downloads, "_browser_websocket_url", fake_url)
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: next(sockets))
    run = asyncio.ensure_future(downloads.watch(profile["id"], 9222))

    await _until(lambda: len(first.sent) == 1)
    first.feed({"id": first.sent[0]["id"], "result": {}})
    first.feed({
        "method": "Browser.downloadWillBegin",
        "params": {"guid": "g", "suggestedFilename": "report.csv", "url": "https://x/y"},
    })
    await _until(lambda: db.list_artifacts(profile["id"]))
    first.drop()  # the socket dies; Chromium carries on

    await _until(lambda: len(second.sent) == 1)
    second.feed({"id": second.sent[0]["id"], "result": {}})
    _stage(profile["id"], "g", b"a,b\n")
    second.feed({
        "method": "Browser.downloadProgress",
        "params": {"guid": "g", "state": "completed", "receivedBytes": 4, "totalBytes": 4},
    })
    await _until(lambda: db.list_artifacts(profile["id"])[0]["state"] != "pending")
    second.close()
    await run

    row = db.list_artifacts(profile["id"])[0]
    assert (row["state"], row["name"], row["size"]) == ("ready", "report.csv", 4)
