"""Tests for the shared license key pool and per-profile key leasing."""
from __future__ import annotations

import pathlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend import database as db
from backend import key_pool
from backend.browser_manager import (
    BrowserManager,
    CloakBrowserLicenseError,
    SEARCH_ENGINE_MARKER,
    is_cooldown_error,
    is_seat_limit_error,
)
from backend.runtime import RuntimeConfig

NATIVE_RUNTIME = RuntimeConfig(
    host_os="windows",
    runtime_mode="native",
    viewer_mode="native-window",
    data_dir=Path("C:/manager-data"),
)


@pytest.fixture()
def pool(tmp_db: Path):
    """The pool lives beside the temp DB, so each test starts empty."""
    return key_pool


def _profile(tmp_path: Path, name: str) -> dict:
    user_data_dir = tmp_path / name
    user_data_dir.mkdir(parents=True, exist_ok=True)
    (user_data_dir / SEARCH_ENGINE_MARKER).write_text("google\n")
    return {
        "id": name,
        "name": name,
        "user_data_dir": str(user_data_dir),
        "screen_width": 1920,
        "screen_height": 1080,
        "launch_args": [],
    }


# ── pool storage ─────────────────────────────────────────────────────────────
def test_add_ignores_blanks_comments_and_duplicates(pool):
    result = pool.add("# comment\n\ncb_one\ncb_two\ncb_one\n")
    assert result == {"added": 2, "duplicates": 1}
    assert [entry["key"] for entry in pool.entries()] == ["cb_one", "cb_two"]


def test_entries_never_expose_the_key_through_the_api_shape(pool):
    pool.add("cb_secret_key_value")
    entry = pool.entries()[0]
    assert entry["id"] == pool.key_id("cb_secret_key_value")
    assert pool.mask_key(entry["key"]) == "cb_se…alue"


def test_state_transitions_and_invalid_keys_drop_out_of_rotation(pool):
    pool.add("cb_one\ncb_two")
    one, two = pool.entries()
    pool.set_state_by_id(one["id"], "invalid")
    assert pool.usable_keys() == ["cb_two"]
    # An unreachable server is transient and must not retire a healthy key.
    pool.set_state_by_id(two["id"], "unreachable")
    assert pool.usable_keys() == ["cb_two"]


def test_remove_by_id(pool):
    pool.add("cb_one")
    entry = pool.entries()[0]
    assert pool.remove(entry["id"]) is True
    assert pool.entries() == []
    assert pool.remove(entry["id"]) is False


def test_corrupt_pool_file_reads_as_empty(tmp_db: Path, pool):
    pool.pool_path().write_text("{not json", encoding="utf-8")
    assert pool.entries() == []


def test_validate_records_outcomes(pool, monkeypatch):
    pool.add("cb_good\ncb_bad")
    import cloakbrowser.license as license_module

    class Info:
        def __init__(self, valid: bool, plan: str, expires: str | None):
            self.valid = valid
            self.plan = plan
            self.expires = expires

    def fake_validate(key: str):
        return Info(key == "cb_good", "free", None)

    monkeypatch.setattr(license_module, "validate_license", fake_validate)
    results = pool.validate()
    states = {result["state"] for result in results}
    assert states == {"valid", "invalid"}
    assert pool.usable_keys() == ["cb_good"]


def test_validate_marks_unreachable_when_the_server_never_answers(pool, monkeypatch):
    pool.add("cb_one")
    import cloakbrowser.license as license_module

    monkeypatch.setattr(license_module, "validate_license", lambda key: None)
    pool.validate(retries=0)
    assert pool.entries()[0]["state"] == "unreachable"
    # Still usable: an unreachable server says nothing about the key.
    assert pool.usable_keys() == ["cb_one"]


# ── leasing ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_each_launch_leases_a_distinct_key(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    pool.add("cb_one\ncb_two")
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    async def launch(**kwargs):
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        return context

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)

    first = await manager.launch(_profile(tmp_path, "a"))
    second = await manager.launch(_profile(tmp_path, "b"))
    assert first.license_key_id != second.license_key_id
    assert set(manager.leased_keys().values()) == {first.license_key_id, second.license_key_id}


@pytest.mark.asyncio
async def test_exhausted_pool_refuses_the_launch_as_a_seat_limit(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    pool.add("cb_only")
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    async def launch(**kwargs):
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        return context

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    await manager.launch(_profile(tmp_path, "a"))

    with pytest.raises(Exception) as raised:
        await manager.launch(_profile(tmp_path, "b"))
    # The UI shows the upgrade CTA only for the seat reason, so the wording matters.
    assert is_seat_limit_error(raised.value)
    # The refused launch must not have left a reservation behind.
    assert "b" not in manager.leased_keys()


@pytest.mark.asyncio
async def test_stopping_a_profile_releases_its_key(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    pool.add("cb_only")
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    async def launch(**kwargs):
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        context.close = AsyncMock()
        return context

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    profile = _profile(tmp_path, "a")
    await manager.launch(profile)
    await manager.stop("a")

    assert manager.leased_keys() == {}
    # The same single key can now serve the next launch.
    await manager.launch(_profile(tmp_path, "b"))
    assert list(manager.leased_keys()) == ["b"]


@pytest.mark.asyncio
async def test_pinned_key_is_reserved_for_its_profile(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    pool.add("cb_one\ncb_two")
    pinned_id = pool.key_id("cb_two")
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    async def launch(**kwargs):
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        return context

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    pinned = _profile(tmp_path, "a")
    pinned["license_key_id"] = pinned_id
    running = await manager.launch(pinned)
    assert running.license_key_id == pinned_id

    # Another profile asking for the same pinned key is refused, not double-booked.
    rival = _profile(tmp_path, "b")
    rival["license_key_id"] = pinned_id
    with pytest.raises(Exception) as raised:
        await manager.launch(rival)
    assert is_seat_limit_error(raised.value)


@pytest.mark.asyncio
async def test_pinned_key_missing_from_pool_is_an_explicit_error(pool, tmp_path: Path):
    manager = BrowserManager(NATIVE_RUNTIME)
    profile = _profile(tmp_path, "a")
    profile["license_key_id"] = "deadbeef0000"
    with pytest.raises(Exception) as raised:
        await manager.launch(profile)
    assert "no longer in the key pool" in str(raised.value)


@pytest.mark.asyncio
async def test_invalid_keys_are_skipped_when_leasing(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    pool.add("cb_dead\ncb_live")
    pool.set_state_by_id(pool.key_id("cb_dead"), "invalid")
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    async def launch(**kwargs):
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        return context

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    running = await manager.launch(_profile(tmp_path, "a"))
    assert running.license_key_id == pool.key_id("cb_live")


@pytest.mark.asyncio
async def test_a_pool_of_rejected_keys_falls_back_to_keyless(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()
    seen: list[object] = []

    async def launch(**kwargs):
        seen.append(kwargs.get("license_key"))
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        return context

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    pool.add("cb_dead")
    pool.set_state_by_id(pool.key_id("cb_dead"), "invalid")

    # A dead pool must not block the browser: the keyless build still runs, and
    # refusing would be worse than the pre-pool behaviour.
    running = await manager.launch(_profile(tmp_path, "a"))
    assert seen == [None]
    assert running.license_key_id is None
    assert manager.leased_keys() == {}


@pytest.mark.asyncio
async def test_a_dead_key_denial_retires_the_key(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    pool.add("cb_dead\ncb_live")
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    async def launch(**kwargs):
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        context.close = AsyncMock()
        return context

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    # Force the first launch onto the dead key by pinning it.
    profile = _profile(tmp_path, "a")
    profile["license_key_id"] = pool.key_id("cb_dead")
    running = await manager.launch(profile)

    # The binary writes exit code 77 for an invalid/expired key. conftest stubs
    # the wrapper's code→error mapping, so supply the real mapping here.
    monkeypatch.setattr(module, "read_denial_file", lambda path: 77)
    monkeypatch.setattr(
        module,
        "license_error_for_code",
        lambda code: CloakBrowserLicenseError(
            "CloakBrowser Pro: license key is invalid, expired, or missing."
        )
        if code == 77
        else None,
    )
    running.denial_path = str(tmp_path / "denial.json")
    await manager._on_browser_closed(running)

    assert pool.find("cb_dead")["state"] == "invalid"
    # The healthy key is untouched: a bad key says nothing about the others.
    assert pool.find("cb_live")["state"] == "unknown"


@pytest.mark.asyncio
async def test_a_seat_denial_does_not_retire_the_key(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    pool.add("cb_live")
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    async def launch(**kwargs):
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        context.close = AsyncMock()
        return context

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    running = await manager.launch(_profile(tmp_path, "a"))

    monkeypatch.setattr(module, "read_denial_file", lambda path: 76)
    monkeypatch.setattr(
        module,
        "license_error_for_code",
        lambda code: CloakBrowserLicenseError(
            "CloakBrowser Pro: session limit reached for your plan."
        )
        if code == 76
        else None,
    )
    running.denial_path = str(tmp_path / "denial.json")
    await manager._on_browser_closed(running)

    # Out of seats is not a bad key — retiring it would strand a good key.
    assert pool.find("cb_live")["state"] == "unknown"


@pytest.mark.asyncio
async def test_app_wide_key_is_leased_when_the_pool_is_empty(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    manager = BrowserManager(NATIVE_RUNTIME, license_key="cb_appwide")
    manager._wait_for_cdp = AsyncMock()

    async def launch(**kwargs):
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        return context

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    running = await manager.launch(_profile(tmp_path, "a"))
    assert running.license_key_id == pool.key_id("cb_appwide")


# ── cooldown: a dead browser's seat is not immediately free ───────────────────
def _fake_seats(monkeypatch, *, state="ok", active=0, limit=1, calls=None):
    import cloakbrowser.license as license_module

    def fake(key):
        if calls is not None:
            calls.append(key)
        return license_module.SessionSeats(
            state=state, active=active, limit=limit, reason=None
        )

    monkeypatch.setattr(license_module, "get_session_seats", fake)


async def _launching_manager(module, monkeypatch):
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    async def launch(**kwargs):
        context = MagicMock(pages=[])
        context.add_init_script = AsyncMock()
        context.close = AsyncMock()
        return context

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    return manager


@pytest.mark.asyncio
async def test_a_crashed_browser_cools_its_key_down(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    pool.add("cb_one\ncb_two")
    manager = await _launching_manager(module, monkeypatch)
    running = await manager.launch(_profile(tmp_path, "a"))
    crashed_on = running.license_key_id

    # A crash leaves no denial file: the seat is still counted by the server.
    await manager._on_browser_closed(running)

    entry = pool.find_by_id(crashed_on)
    assert entry["cooldown_until"] is not None
    assert pool.cooldown_remaining(entry) > 0
    assert manager.leased_keys() == {}
    # The probe says the server is busy, so the other key is taken instead and
    # the cooling one is never handed out.
    _fake_seats(monkeypatch, state="ok", active=1, limit=1)
    other = await manager.launch(_profile(tmp_path, "b"))
    assert other.license_key_id != crashed_on


@pytest.mark.asyncio
async def test_a_clean_server_answer_releases_the_key_early(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    pool.add("cb_one")
    manager = await _launching_manager(module, monkeypatch)
    running = await manager.launch(_profile(tmp_path, "a"))
    await manager._on_browser_closed(running)
    assert pool.cooldown_remaining(pool.entries()[0]) > 0

    calls: list[str] = []
    _fake_seats(monkeypatch, state="ok", active=0, limit=1, calls=calls)
    again = await manager.launch(_profile(tmp_path, "b"))

    assert again.license_key_id == pool.key_id("cb_one")
    assert calls == ["cb_one"]  # one probe, not a wait loop
    assert pool.cooldown_remaining(pool.entries()[0]) == 0


@pytest.mark.asyncio
async def test_a_key_the_server_still_counts_refuses_as_a_cooldown(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    pool.add("cb_one")
    manager = await _launching_manager(module, monkeypatch)
    running = await manager.launch(_profile(tmp_path, "a"))
    await manager._on_browser_closed(running)

    _fake_seats(monkeypatch, state="ok", active=1, limit=1)
    with pytest.raises(Exception) as raised:
        await manager.launch(_profile(tmp_path, "b"))
    assert is_seat_limit_error(raised.value)
    assert is_cooldown_error(raised.value)
    assert "cooling down" in str(raised.value)


@pytest.mark.asyncio
async def test_a_pinned_cooling_key_is_probed_and_then_leased(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    pool.add("cb_one")
    entry_id = pool.key_id("cb_one")
    pool.stamp_cooldown(entry_id, 60)
    manager = await _launching_manager(module, monkeypatch)
    _fake_seats(monkeypatch, state="ok", active=0, limit=1)

    profile = _profile(tmp_path, "a")
    profile["license_key_id"] = entry_id
    running = await manager.launch(profile)
    assert running.license_key_id == entry_id


@pytest.mark.asyncio
async def test_a_pinned_cooling_key_that_stays_busy_is_refused_as_a_cooldown(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    pool.add("cb_one")
    entry_id = pool.key_id("cb_one")
    pool.stamp_cooldown(entry_id, 45)
    manager = await _launching_manager(module, monkeypatch)
    _fake_seats(monkeypatch, state="ok", active=1, limit=1)

    profile = _profile(tmp_path, "a")
    profile["license_key_id"] = entry_id
    with pytest.raises(Exception) as raised:
        await manager.launch(profile)
    assert is_cooldown_error(raised.value)
    assert "unpin the key" in str(raised.value)


@pytest.mark.asyncio
async def test_an_unpinned_launch_cannot_take_a_key_pinned_to_a_stopped_profile(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    pool.add("cb_pinned\ncb_free")
    pinned_id = pool.key_id("cb_pinned")
    db.create_profile(name="owner", profile_id="owner", license_key_id=pinned_id)
    manager = await _launching_manager(module, monkeypatch)

    # The pin reserves the key even though its owner is not running: an
    # unpinned launch takes the other key instead of stealing it.
    running = await manager.launch(_profile(tmp_path, "someone-else"))
    assert running.license_key_id == pool.key_id("cb_free")
    assert manager.leased_keys() == {"someone-else": pool.key_id("cb_free")}


@pytest.mark.asyncio
async def test_a_pool_of_reserved_keys_refuses_an_unpinned_launch(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    pool.add("cb_pinned")
    pinned_id = pool.key_id("cb_pinned")
    db.create_profile(name="owner", profile_id="owner", license_key_id=pinned_id)
    manager = await _launching_manager(module, monkeypatch)

    # The only key is reserved, so the intruder must be refused rather than
    # locking the owner out of the key it was assigned.
    with pytest.raises(Exception) as raised:
        await manager.launch(_profile(tmp_path, "intruder"))
    assert is_seat_limit_error(raised.value)
    assert manager.leased_keys() == {}

    owner = _profile(tmp_path, "owner")
    owner["license_key_id"] = pinned_id
    running = await manager.launch(owner)
    assert running.license_key_id == pinned_id


@pytest.mark.asyncio
async def test_a_failed_close_keeps_the_key_cooling(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    pool.add("cb_one")
    manager = await _launching_manager(module, monkeypatch)
    running = await manager.launch(_profile(tmp_path, "a"))
    order: list[str] = []

    async def boom():
        order.append("close")
        raise RuntimeError("context already gone")

    running.context.close = boom
    await manager._on_browser_closed(running)

    assert set(order) == {"close"}
    assert manager.leased_keys() == {}
    assert pool.cooldown_remaining(pool.entries()[0]) > 0


@pytest.mark.asyncio
async def test_the_launch_failure_path_cools_the_key_when_the_close_fails(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    pool.add("cb_one")
    manager = await _launching_manager(module, monkeypatch)
    order: list[str] = []
    original = module.launch_persistent_context_async

    async def launch(**kwargs):
        context = await original(**kwargs)

        async def boom():
            order.append("close")
            raise RuntimeError("context already gone")

        context.close = boom
        return context

    async def failing_wait(_port, denial_path=None):
        raise TimeoutError("cdp never came up")

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    manager._wait_for_cdp = failing_wait

    with pytest.raises(Exception):
        await manager.launch(_profile(tmp_path, "a"))

    # The close was attempted before the key was handed back, and because it
    # failed the browser may still hold the seat — so the key cools down.
    assert set(order) == {"close"}
    assert manager.leased_keys() == {}
    assert pool.leases() == {}
    assert pool.cooldown_remaining(pool.entries()[0]) > 0


@pytest.mark.asyncio
async def test_a_clean_launch_failure_without_a_denial_leaves_no_cooldown(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    pool.add("cb_one")
    manager = await _launching_manager(module, monkeypatch)
    closed: list[str] = []
    original = module.BrowserManager._close_context

    async def tracked(self, context, profile_id):
        closed.append(profile_id)
        return await original(self, context, profile_id)

    async def failing_wait(_port, denial_path=None):
        raise TimeoutError("cdp never came up")

    monkeypatch.setattr(module.BrowserManager, "_close_context", tracked)
    manager._wait_for_cdp = failing_wait

    with pytest.raises(Exception):
        await manager.launch(_profile(tmp_path, "a"))

    # A clean close on a non-license failure means the seat was given back.
    assert set(closed) == {"a"}
    assert manager.leased_keys() == {}
    assert pool.cooldown_remaining(pool.entries()[0]) == 0


@pytest.mark.asyncio
async def test_a_restart_cools_every_stored_lease(pool, monkeypatch, tmp_path: Path):
    from backend import browser_manager as module

    pool.add("cb_one")
    entry_id = pool.key_id("cb_one")
    pool.set_lease("a", entry_id)
    calls: list[str] = []
    _fake_seats(monkeypatch, state="ok", active=1, limit=1, calls=calls)

    manager = BrowserManager(NATIVE_RUNTIME)
    await manager.reconcile_leases()

    assert manager.leased_keys() == {}
    assert pool.leases() == {}
    assert pool.cooldown_remaining(pool.entries()[0]) > 0
    assert calls == ["cb_one"]


@pytest.mark.asyncio
async def test_the_search_engine_setup_launches_on_the_leased_key_and_waits_for_its_seat(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    manager = BrowserManager(NATIVE_RUNTIME)
    headless_keys: list[object] = []
    polls: list[str] = []

    async def fake_sleep(_seconds):
        return None

    async def click(*_args):
        return True

    page = MagicMock()
    page.goto = AsyncMock()
    page.fill = AsyncMock()
    page.evaluate = AsyncMock(
        side_effect=[
            True,  # the Add dialog opened
            True,  # the Add button became enabled
            True,  # the Add dialog submitted
            True,  # "Make default" clicked
            {"keyword": "google.com"},  # the live default read back
        ]
    )
    locator = MagicMock()
    locator.first.fill = AsyncMock()
    page.locator = MagicMock(return_value=locator)
    ctx = MagicMock(pages=[])
    ctx.new_page = AsyncMock(return_value=page)
    ctx.close = AsyncMock()

    async def fake_headless(user_data_dir, license_key=None, proxy=None):
        headless_keys.append(license_key)
        return ctx

    import cloakbrowser.license as license_module

    def fake_seats(key):
        polls.append(key)
        # Busy once, then free: the wait must poll until the seat is back.
        active = 1 if len(polls) == 1 else 0
        return license_module.SessionSeats(state="ok", active=active, limit=1, reason=None)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(manager, "_headless_launch", fake_headless)
    monkeypatch.setattr(license_module, "get_session_seats", fake_seats)

    await manager._setup_google_default(tmp_path / "a", "cb_one")

    assert headless_keys == ["cb_one"]
    assert polls == ["cb_one", "cb_one"]
    assert ctx.close.await_count == 1


@pytest.mark.asyncio
async def test_the_launch_hands_its_leased_key_to_the_search_engine_setup(
    pool, monkeypatch, tmp_path: Path
):
    from backend import browser_manager as module

    pool.add("cb_one")
    manager = await _launching_manager(module, monkeypatch)
    seen: list[object] = []

    async def fake_setup(user_data_dir, license_key=None, proxy=None):
        seen.append(license_key)

    monkeypatch.setattr(manager, "_setup_google_default", fake_setup)
    profile = _profile(tmp_path, "a")
    (pathlib.Path(profile["user_data_dir"]) / SEARCH_ENGINE_MARKER).unlink()
    await manager.launch(profile)
    assert seen == ["cb_one"]
