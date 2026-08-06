"""Tracks CloakBrowser Chromium download progress for the UI banner.

The vendored `cloakbrowser` package's ensure_binary() only logs its progress —
there's no callback hook to attach to — so this installs a small
logging.Handler on the "cloakbrowser" logger and parses the handful of INFO
messages it emits while downloading. Best-effort: any message it doesn't
recognize is ignored, and the caller resets state in a `finally` (see
browser_manager.launch) so a failed download never leaves the banner stuck on.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Optional, TypedDict


class BinaryStatus(TypedDict):
    downloading: bool
    state: Optional[str]
    percent: Optional[int]


_lock = threading.Lock()
_state: BinaryStatus = {"downloading": False, "state": None, "percent": None}

# Matched against cloakbrowser.download's exact log message templates —
# see _download_and_extract/_ensure_pro_binary/_extract_archive in that
# package's download.py.
_START_RE = re.compile(r"^(?:Stealth Chromium .+ not found\. Downloading|Downloading Pro Chromium .+) for ")
_PROGRESS_RE = re.compile(r"^Download progress: (\d+)% \(")
_EXTRACT_RE = re.compile(r"^Extracting to ")
_READY_RE = re.compile(r"^Binary ready: ")


class _ProgressHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        with _lock:
            if _START_RE.match(message):
                _state.update(downloading=True, state="downloading", percent=0)
            elif match := _PROGRESS_RE.match(message):
                _state.update(downloading=True, state="downloading", percent=int(match.group(1)))
            elif _EXTRACT_RE.match(message):
                _state.update(downloading=True, state="extracting", percent=100)
            elif _READY_RE.match(message):
                _state.update(downloading=False, state=None, percent=None)


_installed = False


def install() -> None:
    """Attach the progress handler to the cloakbrowser logger. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    handler = _ProgressHandler()
    handler.setLevel(logging.INFO)
    logging.getLogger("cloakbrowser").addHandler(handler)


def mark_idle() -> None:
    """Force the banner off. Called after every ensure_binary() call (success
    or failure) so a download that errors out — never reaching the "Binary
    ready" log line the handler above resets on — doesn't leave it stuck on.
    """
    with _lock:
        _state.update(downloading=False, state=None, percent=None)


def snapshot() -> BinaryStatus:
    with _lock:
        return dict(_state)  # type: ignore[return-value]
