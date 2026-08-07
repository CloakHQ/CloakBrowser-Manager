"""Unit tests for cookie_warmup.py's pure warmup-run logic.

Runs entirely against a fake Playwright context (no real browser, no real
network) with WARMUP_SITES/WARMUP_DURATION_SECONDS monkeypatched down to a
handful of sites and near-zero dwell time, so a run that would take 10 real
minutes through the actual module defaults takes milliseconds here.
"""

from __future__ import annotations

import asyncio

import pytest

from backend import cookie_warmup


class FakePage:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.goto_calls: list[str] = []
        self.closed = False

    async def goto(self, url: str, timeout: int, wait_until: str) -> None:
        self.goto_calls.append(url)
        if self.fail:
            raise RuntimeError("simulated navigation failure")

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, fail_urls: frozenset[str] = frozenset()):
        self.fail_urls = fail_urls
        self.pages: list[FakePage] = []

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


@pytest.fixture(autouse=True)
def _small_warmup(monkeypatch):
    """Three sites, ~0s dwell each — every test in this file runs fast."""
    monkeypatch.setattr(cookie_warmup, "WARMUP_SITES", ("https://a.test", "https://b.test", "https://c.test"))
    monkeypatch.setattr(cookie_warmup, "WARMUP_DURATION_SECONDS", 0.03)


# ── new_status ───────────────────────────────────────────────────────────────


def test_new_status_starts_idle():
    status = cookie_warmup.new_status()
    assert status.state == "idle"
    assert status.sites_visited == 0
    assert status.current_site is None


# ── run: happy path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_visits_every_site_and_finishes_done():
    context = FakeContext()
    status = cookie_warmup.new_status()

    await cookie_warmup.run(context, status, lambda: True)

    assert status.state == "done"
    assert status.sites_visited == 3
    assert status.current_site is None
    assert status.started_at is not None
    assert status.finished_at is not None
    assert len(context.pages) == 3
    assert all(p.closed for p in context.pages)
    assert [p.goto_calls[0] for p in context.pages] == list(cookie_warmup.WARMUP_SITES)


@pytest.mark.asyncio
async def test_run_updates_sites_visited_incrementally():
    context = FakeContext()
    status = cookie_warmup.new_status()
    seen_counts = []

    real_visit = cookie_warmup._visit_site

    async def spying_visit(ctx, url, dwell):
        await real_visit(ctx, url, dwell)
        seen_counts.append(status.sites_visited)

    import backend.cookie_warmup as mod
    original = mod._visit_site
    mod._visit_site = spying_visit
    try:
        await cookie_warmup.run(context, status, lambda: True)
    finally:
        mod._visit_site = original

    # sites_visited is bumped by run() AFTER _visit_site returns, so the spy
    # (called from inside _visit_site) always observes the count as it stood
    # for the site just finished, one step behind the running total.
    assert seen_counts == [0, 1, 2]


# ── run: a failing site does not abort the whole run ────────────────────────


@pytest.mark.asyncio
async def test_run_continues_past_a_site_that_fails_to_load():
    class FlakyContext(FakeContext):
        async def new_page(self):
            page = FakePage(fail=(len(self.pages) == 1))  # second site fails
            self.pages.append(page)
            return page

    context = FlakyContext()
    status = cookie_warmup.new_status()

    await cookie_warmup.run(context, status, lambda: True)

    assert status.state == "done"
    assert status.sites_visited == 3  # the failure still counts as "visited"
    assert all(p.closed for p in context.pages)  # finally still closes it


# ── run: is_still_running() stops it early ──────────────────────────────────


@pytest.mark.asyncio
async def test_run_stops_early_when_profile_is_no_longer_running():
    context = FakeContext()
    status = cookie_warmup.new_status()
    calls = {"n": 0}

    def is_still_running():
        calls["n"] += 1
        return calls["n"] <= 1  # true for site 1, false from site 2 onward

    await cookie_warmup.run(context, status, is_still_running)

    assert status.state == "cancelled"
    assert status.sites_visited == 1
    assert status.finished_at is not None


# ── run: asyncio cancellation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_marks_cancelled_and_reraises_on_task_cancel():
    class SlowContext(FakeContext):
        async def new_page(self):
            await asyncio.sleep(10)  # never actually returns in this test
            raise AssertionError("unreachable")

    context = SlowContext()
    status = cookie_warmup.new_status()

    task = asyncio.create_task(cookie_warmup.run(context, status, lambda: True))
    await asyncio.sleep(0)  # let the task start and reach the sleep
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert status.state == "cancelled"
    assert status.finished_at is not None


# ── run: an unexpected error surfaces instead of hanging silently ──────────


@pytest.mark.asyncio
async def test_run_records_error_state_on_unexpected_exception():
    class ExplodingContext(FakeContext):
        async def new_page(self):
            raise RuntimeError("context is already closed")

    context = ExplodingContext()
    status = cookie_warmup.new_status()

    await cookie_warmup.run(context, status, lambda: True)

    assert status.state == "error"
    assert "already closed" in status.error
    assert status.sites_visited == 0
