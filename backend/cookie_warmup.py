"""Cookie warmup: drives a running profile through a curated list of common
sites so a brand-new profile is not a completely blank slate of cookies and
browsing history. Triggered by POST /api/profiles/{id}/cookie-warmup/start in
main.py, which owns the asyncio.Task lifecycle; this module only knows how to
run one warmup pass and report its own progress.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("cloakbrowser.manager")

# Ordinary, high-traffic sites that set cookies on load and are unlikely to
# immediately wall off a fresh automated visit behind a login or a captcha —
# this is about looking like a normal browsing history, not scraping anything.
WARMUP_SITES: tuple[str, ...] = (
    "https://www.google.com",
    "https://www.youtube.com",
    "https://en.wikipedia.org",
    "https://www.amazon.com",
    "https://www.reddit.com",
    "https://www.nytimes.com",
    "https://www.cnn.com",
    "https://www.bbc.com",
    "https://www.espn.com",
    "https://www.ebay.com",
    "https://www.walmart.com",
    "https://www.target.com",
    "https://www.imdb.com",
    "https://weather.com",
    "https://github.com",
    "https://stackoverflow.com",
    "https://www.linkedin.com",
    "https://twitter.com",
    "https://www.instagram.com",
    "https://open.spotify.com",
)

# 10 minutes total, spread evenly across WARMUP_SITES.
WARMUP_DURATION_SECONDS = 600
PAGE_LOAD_TIMEOUT_MS = 20_000


@dataclass
class WarmupStatus:
    """Mutated in place by run() so main.py's status endpoint always reads
    the latest state without needing its own copy of the asyncio.Task."""

    state: str = "idle"  # idle | running | done | error | cancelled
    sites_total: int = len(WARMUP_SITES)
    sites_visited: int = 0
    current_site: str | None = None
    started_at: float | None = None  # time.monotonic()
    finished_at: float | None = None
    error: str | None = None


def new_status() -> WarmupStatus:
    return WarmupStatus()


async def _visit_site(context: Any, url: str, dwell_seconds: float) -> None:
    """Open one tab, let the page settle, sit on it, then close the tab.

    Never raises: a site that times out or errors should not end the whole
    warmup run, it should just be a shorter, quieter visit than the rest.
    """
    page = await context.new_page()
    try:
        await page.goto(url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
        await asyncio.sleep(dwell_seconds)
    except Exception as exc:
        logger.warning("Cookie warmup: failed to visit %s: %s", url, exc)
    finally:
        await page.close()


async def run(context: Any, status: WarmupStatus, is_still_running: Callable[[], bool]) -> None:
    """Visit every WARMUP_SITES entry, ~WARMUP_DURATION_SECONDS/len(sites)
    seconds each, updating `status` after every site.

    `is_still_running` is checked before each site so a profile stopped (or
    restarted, which swaps in a new context under the same profile id)
    partway through ends this cleanly instead of driving a closed context.
    Cancellation (asyncio.Task.cancel(), used by the /stop endpoint) is left
    to propagate — the caller's task owns that state transition.
    """
    status.state = "running"
    status.started_at = time.monotonic()
    dwell_seconds = WARMUP_DURATION_SECONDS / len(WARMUP_SITES)
    try:
        for url in WARMUP_SITES:
            if not is_still_running():
                status.state = "cancelled"
                return
            status.current_site = url
            await _visit_site(context, url, dwell_seconds)
            status.sites_visited += 1
        status.state = "done"
    except asyncio.CancelledError:
        status.state = "cancelled"
        raise
    except Exception as exc:
        logger.error("Cookie warmup failed: %s", exc)
        status.state = "error"
        status.error = str(exc)
    finally:
        status.current_site = None
        status.finished_at = time.monotonic()
