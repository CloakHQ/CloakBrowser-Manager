"""Per-profile CPU/memory usage — the whole Chromium process tree, not just
the root process.

A profile's real cost is the root browser process PLUS every renderer, GPU,
network-service and utility subprocess Chromium spawns under it — reading
just the root pid would under-report by however many tabs/services are
active. psutil.Process.children(recursive=True) walks that tree without us
hand-rolling a /proc scan.

CPU% needs two samples: an instant Process.cpu_percent() call always
answers 0.0 on the very first call for that Process *object* (it has no
prior sample to diff against — see psutil's own docs). Rather than caching
Process objects across polls (a whole persistence/cleanup problem in
BrowserManager for state nobody else needs), this takes both samples
inside one request: prime every process, sleep a short interval, read
again. That costs CPU_SAMPLE_INTERVAL_S of latency per call, which is fine
for a periodic status poll and worth it to keep this module stateless.
"""

from __future__ import annotations

import asyncio
import logging

import psutil

from .browser_manager import BrowserProcess, process_is_alive

logger = logging.getLogger("cloakbrowser.manager.resources")

# Long enough for a meaningful CPU delta, short enough that a periodic
# frontend poll (a few seconds apart) barely notices the latency.
CPU_SAMPLE_INTERVAL_S = 0.2


def _process_tree(root_pid: int) -> list[psutil.Process]:
    """The root process plus every descendant, or [] if the root is already
    gone (a profile whose Chromium exited between the caller's liveness
    check and this call — not an error, just nothing left to measure)."""
    try:
        root = psutil.Process(root_pid)
    except psutil.NoSuchProcess:
        return []
    procs = [root]
    try:
        procs.extend(root.children(recursive=True))
    except psutil.NoSuchProcess:
        # The root died between construction and the children() call.
        pass
    return procs


async def get_resource_usage(proc: BrowserProcess) -> dict[str, float | int | None]:
    """CPU%, RSS memory (MB), and live process count for a profile's whole
    process tree, or all-None/0 if the root process is already gone.

    process_is_alive() re-verifies (pid, starttime) first — the same
    recycled-pid guard every other consumer of BrowserProcess uses (see its
    docstring). Without it, a dead browser whose pid the kernel already
    handed to an unrelated process would report THAT process's resource
    usage as this profile's, silently.

    CPU% is the un-normalized per-core convention psutil (and `top`) use by
    default: one fully-busy core is 100%, so three renderer processes truly
    running in parallel can legitimately sum past 100%. Never raises — a
    process racing to exit mid-sample is expected, not a bug in the caller.
    """
    if not process_is_alive(proc):
        return {"cpu_percent": None, "memory_mb": None, "process_count": 0}

    procs = _process_tree(proc.pid)
    if not procs:
        return {"cpu_percent": None, "memory_mb": None, "process_count": 0}

    for p in procs:
        try:
            p.cpu_percent(interval=None)  # primes; this first value is meaningless
        except psutil.NoSuchProcess:
            continue

    await asyncio.sleep(CPU_SAMPLE_INTERVAL_S)

    cpu_total = 0.0
    memory_total_bytes = 0
    alive = 0
    for p in procs:
        try:
            cpu_total += p.cpu_percent(interval=None)
            memory_total_bytes += p.memory_info().rss
            alive += 1
        except psutil.NoSuchProcess:
            continue

    if alive == 0:
        return {"cpu_percent": None, "memory_mb": None, "process_count": 0}

    return {
        "cpu_percent": round(cpu_total, 1),
        "memory_mb": round(memory_total_bytes / (1024 * 1024), 1),
        "process_count": alive,
    }
