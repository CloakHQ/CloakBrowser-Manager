"""Tests for backend/resources.py — per-profile CPU/memory usage."""

from __future__ import annotations

import os
import subprocess

import pytest

from backend import browser_manager as bm
from backend import resources


def _self_as_browser_process() -> bm.BrowserProcess:
    """A BrowserProcess describing the CURRENT test process — genuinely
    alive, with a real (pid, starttime) pair process_is_alive() will accept.
    Any subprocess a test spawns becomes a real child of it, so the process
    tree walk has something real to find."""
    stat = bm._proc_stat(os.getpid())
    assert stat is not None
    return bm.BrowserProcess(
        pid=os.getpid(), starttime=stat[2], user_data_dir="/tmp", cdp_port=5100,
    )


@pytest.mark.asyncio
async def test_get_resource_usage_for_a_live_process():
    proc = _self_as_browser_process()

    usage = await resources.get_resource_usage(proc)

    assert usage["process_count"] >= 1
    assert usage["memory_mb"] is not None
    assert usage["memory_mb"] > 0
    assert usage["cpu_percent"] is not None
    assert usage["cpu_percent"] >= 0.0


@pytest.mark.asyncio
async def test_get_resource_usage_counts_child_processes():
    proc = _self_as_browser_process()
    children = [subprocess.Popen(["sleep", "2"]) for _ in range(2)]
    try:
        usage = await resources.get_resource_usage(proc)
        # self + at least the 2 just spawned — a floor, not an exact count,
        # since the test runner may legitimately have other children too.
        assert usage["process_count"] >= 3
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=5)


@pytest.mark.asyncio
async def test_get_resource_usage_returns_none_for_a_dead_process():
    # No process can plausibly be alive at this pid with this starttime.
    proc = bm.BrowserProcess(
        pid=999999, starttime=999999999, user_data_dir="/tmp", cdp_port=5100,
    )

    usage = await resources.get_resource_usage(proc)

    assert usage == {"cpu_percent": None, "memory_mb": None, "process_count": 0}


@pytest.mark.asyncio
async def test_get_resource_usage_detects_a_recycled_pid():
    """The whole point of checking process_is_alive() first: a genuinely
    live process at this pid whose starttime does NOT match the claimed one
    must read as gone, not as itself — the recycled-pid case every other
    BrowserProcess consumer already guards against."""
    proc = bm.BrowserProcess(
        pid=os.getpid(), starttime=1,  # a real, alive pid; a wrong starttime
        user_data_dir="/tmp", cdp_port=5100,
    )

    usage = await resources.get_resource_usage(proc)

    assert usage == {"cpu_percent": None, "memory_mb": None, "process_count": 0}
