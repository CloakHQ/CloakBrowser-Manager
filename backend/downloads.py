"""Capture what a profile's browser downloads, as artifacts.

Chromium is told to name downloads by GUID (``allowAndName``) and to emit progress events, so
an artifact is published only once the browser reports the transfer finished. A filename
appearing in a directory proves nothing while a download is still running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

import httpx

from . import artifacts
from . import database as db

logger = logging.getLogger("cloakbrowser.manager.downloads")

STAGING_DIRNAME = artifacts.STAGING_DIRNAME


def staging_dir(profile_id: str) -> Path:
    """Where Chromium drops GUID-named files before they are published."""
    return artifacts.artifact_dir(profile_id) / STAGING_DIRNAME


class DownloadTracker:
    """Turns one profile's CDP download events into artifact rows.

    Deliberately free of websocket plumbing so the state machine is testable without a browser.
    """

    def __init__(self, profile_id: str, cancel: Callable[[str], None] | None = None) -> None:
        self.profile_id = profile_id
        # Called with a GUID to ask Chromium to stop a transfer that broke a limit.
        self._cancel = cancel
        self._by_guid: dict[str, str] = {}
        # Transfers we declined to record. Chromium keeps downloading regardless, so their
        # GUIDs are remembered until a terminal event lets us clear the staged bytes.
        self._refused: set[str] = set()

    def bind_cancel(self, cancel: Callable[[str], None] | None) -> None:
        """Point cancellation at the current capture socket: the tracker outlives sockets."""
        self._cancel = cancel

    def begin(self, guid: str | None, suggested_filename: str | None) -> str | None:
        """Record a started download as a pending artifact. Returns its id, or None if refused."""
        if not guid:
            return None
        name = artifacts.safe_filename(suggested_filename, fallback="download")
        try:
            artifacts.check_quota(self.profile_id)
        except artifacts.ArtifactQuotaExceeded as exc:
            # Declining to record it does not stop the browser, so the transfer is cancelled
            # too — but only once staging proves it is ours. Another CDP client (Playwright's
            # connect_over_cdp sets its own download path) still delivers its events here, and
            # cancelling those would break an automation session that never asked for any of
            # this.
            logger.warning("Profile %s: not capturing %r: %s", self.profile_id, name, exc)
            self._refused.add(guid)
            return None
        artifact_id = db.new_artifact_id()
        db.create_artifact(
            artifact_id, self.profile_id, name, 0, kind="download", state="pending",
            picker_name=artifacts.reserve_picker_name(self.profile_id, name),
        )
        self._by_guid[guid] = artifact_id
        return artifact_id

    def progress(self, guid: str | None, state: str | None, received_bytes: int = 0) -> None:
        """Publish or discard a tracked download once the browser says it is done."""
        key = guid or ""
        if key in self._refused:
            staged = staging_dir(self.profile_id) / key
            if state == "inProgress":
                # By now the bytes would be visible if this download were ours.
                if staged.exists() and self._cancel:
                    self._cancel(key)
                return
            self._refused.discard(key)
            staged.unlink(missing_ok=True)
            return
        artifact_id = self._by_guid.get(key)
        if artifact_id is None:
            return
        if state == "inProgress":
            # Refusing to record it would not stop Chromium, which would keep filling the
            # staging directory unseen. Stop the transfer instead.
            if received_bytes > artifacts.MAX_ARTIFACT_BYTES:
                # Only cancel a transfer we can see: the events may belong to another CDP
                # client that re-pointed the download path.
                if not (staging_dir(self.profile_id) / (guid or "")).exists():
                    return
                self._fail(artifact_id, guid or "", "exceeded the maximum artifact size")
                self._by_guid.pop(guid or "", None)
                if self._cancel:
                    self._cancel(guid or "")
                artifacts.sync_picker_view(self.profile_id)
            return
        self._by_guid.pop(guid or "", None)
        if state == "completed":
            self._publish(artifact_id, guid or "", received_bytes)
        else:
            self._discard(artifact_id, guid or "")
        artifacts.sync_picker_view(self.profile_id)

    def _publish(self, artifact_id: str, guid: str, received_bytes: int) -> None:
        row = db.get_artifact(self.profile_id, artifact_id)
        if row is None:
            # The artifact was deleted while its transfer ran; drop what it staged.
            (staging_dir(self.profile_id) / guid).unlink(missing_ok=True)
            return
        source = staging_dir(self.profile_id) / guid
        dest = artifacts.artifact_path(self.profile_id, artifact_id, row["name"])
        # The size is only known for certain now, so the caps are checked against the real
        # bytes rather than against the zero we had when the transfer started.
        if not source.exists():
            # Nothing staged: another CDP client owns the download path, so these events were
            # never ours. Keeping a failed row would accumulate phantom artifacts for every
            # download that automation makes.
            logger.debug(
                "Profile %s: download %r completed elsewhere; not ours to record",
                self.profile_id, row["name"],
            )
            db.delete_artifact(self.profile_id, artifact_id)
            return
        try:
            actual = source.stat().st_size
        except OSError as exc:
            logger.warning("Profile %s: download %r did not land: %s", self.profile_id, row["name"], exc)
            db.update_artifact(self.profile_id, artifact_id, state="failed", size=received_bytes)
            return
        if actual > artifacts.MAX_ARTIFACT_BYTES:
            self._fail(artifact_id, guid, "exceeded the maximum artifact size")
            return
        try:
            # This artifact already holds its slot, claimed when the transfer began.
            artifacts.check_quota(self.profile_id, actual, replacing=artifact_id)
        except artifacts.ArtifactQuotaExceeded as exc:
            self._fail(artifact_id, guid, str(exc))
            return
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, dest)
            size = dest.stat().st_size
        except OSError as exc:
            logger.warning("Profile %s: download %r did not land: %s", self.profile_id, row["name"], exc)
            db.update_artifact(self.profile_id, artifact_id, state="failed", size=received_bytes)
            return
        db.update_artifact(self.profile_id, artifact_id, state="ready", size=size)

    def _fail(self, artifact_id: str, guid: str, reason: str) -> None:
        """Record a refused or broken transfer and drop whatever it staged."""
        logger.warning("Profile %s: download refused — %s", self.profile_id, reason)
        (staging_dir(self.profile_id) / guid).unlink(missing_ok=True)
        db.update_artifact(self.profile_id, artifact_id, state="failed")

    def _discard(self, artifact_id: str, guid: str) -> None:
        (staging_dir(self.profile_id) / guid).unlink(missing_ok=True)
        artifacts.delete_file(self.profile_id, artifact_id)
        db.delete_artifact(self.profile_id, artifact_id)


def reconcile_interrupted(profile_id: str) -> int:
    """Fail any download left pending by a previous browser, and clear its staged bytes.

    A fresh browser will never resume them, so leaving them pending would show a transfer
    that is permanently in flight.
    """
    stranded = [row for row in db.list_artifacts(profile_id) if row["state"] == "pending"]
    for row in stranded:
        db.update_artifact(profile_id, row["id"], state="failed")
    staging = staging_dir(profile_id)
    if staging.is_dir():
        for leftover in staging.iterdir():
            if leftover.is_file():
                leftover.unlink(missing_ok=True)
    return len(stranded)


async def _browser_websocket_url(cdp_port: int) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=5)
    return resp.json()["webSocketDebuggerUrl"]


def handle_event(tracker: DownloadTracker, message: dict) -> None:
    params = message.get("params") or {}
    method = message.get("method")
    if method == "Browser.downloadWillBegin":
        tracker.begin(params.get("guid"), params.get("suggestedFilename"))
    elif method == "Browser.downloadProgress":
        tracker.progress(params.get("guid"), params.get("state"), int(params.get("receivedBytes") or 0))


# Backoff between reconnects while the browser is still running.
_RECONNECT_DELAYS = (1, 2, 5, 10, 30)


class _Session:
    """Id-matched calls over one browser-level CDP socket; every other message is an event.

    A reply is recognised by its id, never by arrival order: events interleave with replies.
    """

    def __init__(self, ws: Any, profile_id: str) -> None:
        self._ws = ws
        self.profile_id = profile_id
        self.on_event: Callable[[dict], None] = lambda message: None
        self._next_id = 0
        self._replies: dict[int, asyncio.Future] = {}

    async def call(self, method: str, params: dict | None = None, *, timeout: float = 10) -> dict:
        self._next_id += 1
        call_id = self._next_id
        reply: asyncio.Future = asyncio.get_running_loop().create_future()
        self._replies[call_id] = reply
        try:
            await self._ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
            message = await asyncio.wait_for(reply, timeout)
        finally:
            self._replies.pop(call_id, None)
        if "error" in message:
            raise RuntimeError(f"{method} refused: {message['error']}")
        return message.get("result") or {}

    async def pump(self) -> None:
        """Deliver replies to their callers and events to ``on_event`` until the socket closes."""
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                call_id = message.get("id")
                reply = self._replies.get(call_id) if isinstance(call_id, int) else None
                if reply is not None:
                    if not reply.done():
                        reply.set_result(message)
                    continue
                try:
                    self.on_event(message)
                except Exception as exc:  # one bad event must not end capture
                    logger.warning("Profile %s: download event failed: %s", self.profile_id, exc)
        finally:
            for reply in self._replies.values():
                if not reply.done():
                    reply.set_exception(ConnectionError("CDP socket closed"))


async def _arm(session: _Session, staging: Path) -> None:
    await session.call("Browser.setDownloadBehavior", {
        "behavior": "allowAndName", "downloadPath": str(staging), "eventsEnabled": True,
    })


def _cancel_via(session: _Session, profile_id: str) -> Callable[[str], None]:
    """A tracker callback that asks Chromium to stop a transfer, reporting a refusal."""
    in_flight: set[asyncio.Future] = set()  # the loop only holds tasks weakly

    def cancel(guid: str) -> None:
        task = asyncio.ensure_future(session.call("Browser.cancelDownload", {"guid": guid}))
        in_flight.add(task)

        def report(done: asyncio.Future) -> None:
            in_flight.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.warning(
                    "Profile %s: could not cancel download %s: %s", profile_id, guid, done.exception()
                )

        task.add_done_callback(report)

    return cancel


async def _rearm_on_detach(
    session: _Session, staging: Path, rearm: asyncio.Event, profile_id: str
) -> None:
    """Chromium drops download behaviour back to its default when a client that set its own
    detaches — not back to ours — so a Playwright session leaving would switch capture off."""
    while True:
        await rearm.wait()
        rearm.clear()
        await _arm(session, staging)
        logger.debug("Profile %s: download capture re-armed after a CDP client detached", profile_id)


async def _run_capture(
    session: _Session,
    staging: Path,
    rearm: asyncio.Event | None,
    profile_id: str,
    on_armed: Callable[[], None] | None = None,
) -> None:
    """Arm capture and keep it armed until the socket closes. Raises if it drops."""
    pump = asyncio.ensure_future(session.pump())
    tasks = [pump]
    try:
        # Only claim capture is on once Chromium has actually accepted the configuration.
        await _arm(session, staging)
        logger.info("Download capture armed for profile %s", profile_id)
        if on_armed is not None:
            on_armed()
        if rearm is not None:
            tasks.append(asyncio.ensure_future(_rearm_on_detach(session, staging, rearm, profile_id)))
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # The socket has the last word: a clean close returns even if it cut a re-arm short.
        if pump.done():
            pump.result()
            return
        for task in tasks[1:]:
            if task.done():
                task.result()  # a refused re-arm propagates to the reconnect loop
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _capture_session(
    profile_id: str,
    cdp_port: int,
    staging: Path,
    rearm: asyncio.Event | None = None,
    on_armed: Callable[[], None] | None = None,
    tracker: DownloadTracker | None = None,
) -> None:
    """One CDP connection's worth of capture. Returns when the socket closes cleanly."""
    import websockets

    tracker = tracker or DownloadTracker(profile_id)
    url = await _browser_websocket_url(cdp_port)
    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
        session = _Session(ws, profile_id)
        tracker.bind_cancel(_cancel_via(session, profile_id))
        session.on_event = lambda message: handle_event(tracker, message)
        await _run_capture(session, staging, rearm, profile_id, on_armed)


async def watch(
    profile_id: str,
    cdp_port: int,
    still_running: Callable[[], bool] | None = None,
    rearm: asyncio.Event | None = None,
) -> None:
    """Arm download capture on a running profile and publish what it downloads.

    Never raises into the launch path: losing capture must not cost the browser. A dropped
    socket is retried while the browser is still running, because otherwise capture would
    stay off silently until the next launch. ``rearm`` is set whenever an external CDP client
    disconnects, since it may have taken the browser's download behaviour with it.
    """
    try:
        staging = staging_dir(profile_id)
        staging.mkdir(parents=True, exist_ok=True)
        # Once, before the first connection: transfers from a previous browser can never
        # resume. A reconnect must NOT repeat this — it would delete a live transfer's bytes.
        stranded = reconcile_interrupted(profile_id)
        if stranded:
            logger.info("Profile %s: failed %d download(s) interrupted by a restart", profile_id, stranded)
        # One tracker for the browser's lifetime, not per socket: a socket can drop while a
        # transfer is in flight and Chromium carries on, and the completion event then arrives
        # on the next socket. A fresh tracker would not know the download and leave it pending.
        tracker = DownloadTracker(profile_id)

        budget: list[int] = []

        def armed() -> None:
            # A session that armed earns a fresh budget: a long-lived browser must not end
            # up without capture because of a few drops spread over days.
            budget[:] = list(_RECONNECT_DELAYS)

        armed()
        while True:
            if still_running is not None and not still_running():
                return
            try:
                await _capture_session(
                    profile_id, cdp_port, staging, rearm, on_armed=armed, tracker=tracker
                )
                return  # closed cleanly — the browser is going away
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Profile %s: download capture dropped: %s", profile_id, exc)
            if not budget:
                logger.warning("Profile %s: download capture gave up reconnecting", profile_id)
                return
            await asyncio.sleep(budget.pop(0))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Download capture stopped for profile %s: %s", profile_id, exc)
