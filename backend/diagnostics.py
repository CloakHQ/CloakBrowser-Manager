"""Startup + crash diagnostics for CloakBrowser Manager.

Everything here aims at one goal: make an arbitrary FUTURE crash diagnosable
from the rotating manager.log alone, without a repro. Three layers:

1. A one-line startup fingerprint (`startup_line`) — identity of the box, build,
   and env, so every support ticket starts with "which build, which OS, frozen?,
   which tier/plan, what stream encoding".
2. Global crash hooks (`install_crash_hooks` / `install_asyncio_handler`) — an
   uncaught exception in ANY thread or the asyncio loop lands in the file log
   with a full traceback instead of vanishing to a frozen app's dead stderr.
3. A stderr tee (`install_stderr_tee`) — the wrapper (cloakbrowser) writes some
   things (welcome banner, preview-fallback notice) STRAIGHT to sys.stderr,
   bypassing logging. The tee mirrors those direct writes into the log so
   "everything the wrapper shows" is captured, not just what it logs.

The stream encoding is captured at process entry (`capture_stream_state`) BEFORE
app_entry reconfigures the streams to errors="replace" — otherwise the
fingerprint would always read "replace" and hide the cp1252/strict that actually
crashes frozen Windows apps.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import threading

# Raw stdout/stderr encoding+errors, captured once at the earliest point.
_stream_state: str | None = None
_hooks_installed = False
_tee_installed = False


def _read_stream_state() -> str:
    parts = []
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        enc = getattr(stream, "encoding", None) or "?"
        errs = getattr(stream, "errors", None) or "?"
        parts.append(f"{name}={enc}/{errs}")
    return " ".join(parts)


def capture_stream_state() -> None:
    """Record stdout/stderr encoding+errors once, before any reconfigure."""
    global _stream_state
    if _stream_state is None:
        _stream_state = _read_stream_state()


def app_version() -> str:
    """Best-effort Manager build version.

    env CB_VERSION (CI/build) -> bundled backend/version.txt (frozen app) ->
    git describe (dev checkout) -> "unknown".
    """
    env = os.environ.get("CB_VERSION")
    if env and env.strip():
        return env.strip()
    try:
        from .runtime import bundle_dir

        vf = bundle_dir() / "backend" / "version.txt"
        if vf.exists():
            text = vf.read_text(encoding="utf-8").strip()
            if text:
                return text
    except Exception:
        pass
    try:
        import subprocess

        out = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def wrapper_version() -> str:
    """Version of the cloakbrowser wrapper package (drives download/launch)."""
    try:
        from importlib.metadata import version

        return version("cloakbrowser")
    except Exception:
        try:
            from cloakbrowser._version import __version__

            return __version__
        except Exception:
            return "unknown"


def redact_proxy(url: str | None) -> str:
    """Proxy string with any user:pass stripped — never log credentials."""
    if not url:
        return "none"
    import re

    return re.sub(r"//[^/@]*@", "//", url)


def startup_line(browser_mgr) -> str:
    """One-line environment fingerprint for the log."""
    rt = getattr(browser_mgr, "runtime", None)
    try:
        from . import database as db

        data_dir = str(db.DATA_DIR)
    except Exception:
        data_dir = "?"
    return (
        f"diag: manager={app_version()} py={platform.python_version()} "
        f"os={platform.system()}-{platform.release()} arch={platform.machine()} "
        f"frozen={bool(getattr(sys, 'frozen', False))} | "
        f"streams: {_stream_state or _read_stream_state()} | "
        f"ui={os.environ.get('CLOAKBROWSER_MANAGER_UI', 'auto')} "
        f"runtime={getattr(rt, 'runtime_mode', '?')} "
        f"host_os={getattr(rt, 'host_os', '?')} data_dir={data_dir} | "
        f"wrapper={wrapper_version()} "
        f"binary_cache={os.environ.get('CLOAKBROWSER_CACHE_DIR', '?')}"
    )


def install_crash_hooks(logger: logging.Logger) -> None:
    """Route uncaught exceptions from the main thread and worker threads to the
    file logger with a full traceback. uvicorn runs in a thread and auto-launch
    spawns tasks, so without this a novel crash off the request path dies on the
    frozen app's dead stderr and leaves nothing behind."""
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True

    def _sys_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _sys_hook

    def _thread_hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        logger.critical(
            "Uncaught exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook


def install_asyncio_handler(loop, logger: logging.Logger) -> None:
    """Log unhandled exceptions from the asyncio loop (tasks, callbacks)."""

    def _handler(_loop, context):
        exc = context.get("exception")
        msg = context.get("message", "asyncio error")
        if exc is not None:
            logger.error("Asyncio: %s", msg, exc_info=exc)
        else:
            logger.error("Asyncio: %s", msg)

    loop.set_exception_handler(_handler)


class _StderrTee:
    """Wrap the real stderr: pass writes through unchanged AND mirror complete
    lines to a logger, so direct sys.stderr.write() (the wrapper's banner /
    preview-fallback notice / any stray print) lands in manager.log.

    Loop-safe: logging's own StreamHandler captured the ORIGINAL stderr at
    construction (before this tee is installed), so formatted log records bypass
    the tee — only genuine direct writes are mirrored. Records go to a distinct
    'cloakbrowser.console' logger to keep them visually separate.
    """

    def __init__(self, real, logger: logging.Logger):
        self._real = real
        self._logger = logger
        self._buf = ""

    def write(self, text: str):
        try:
            self._real.write(text)
        except Exception:
            pass
        try:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.rstrip()
                if line:
                    self._logger.info(line)
        except Exception:
            self._buf = ""
        return len(text)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        # isatty, encoding, errors, fileno, etc. — defer to the real stream.
        return getattr(self._real, name)


def install_stderr_tee() -> None:
    """Mirror direct sys.stderr writes into the log (once)."""
    global _tee_installed
    if _tee_installed or sys.stderr is None:
        return
    _tee_installed = True
    sys.stderr = _StderrTee(sys.stderr, logging.getLogger("cloakbrowser.console"))
