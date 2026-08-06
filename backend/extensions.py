"""Discovers unpacked Chrome extensions the operator drops on the host.

Scanned ONCE, the first time list_available_extensions() is called (which in
practice is at backend startup — main.py's lifespan calls it directly), and
cached for the rest of the process's life. Deliberately not re-scanned on
every request or watched for changes:

  1. Predictability. A profile's enabled-extensions checkboxes reference
     extension ids by directory name. If the list changed mid-session, a
     profile already running could have an extension vanish out from under a
     checkbox the user is looking at, or a newly-added one could silently
     start applying to profiles that never opted in.
  2. Cost. Reading every extension's manifest.json (and resolving any
     __MSG_..__ localized name/description through _locales/) on every
     GET /api/extensions or every launch is needless I/O for something that,
     by design, never changes without a container restart anyway.

Adding, removing, or editing an extension under EXTENSIONS_DIR requires a
`docker compose restart` (or recreate) to be picked up automatically — OR an
operator can force it via rescan_extensions() (POST /api/extensions/rescan,
a "Rescan" button in the UI). That stays consistent with the reasoning
above: a human explicitly asking for the list to change right now is not
the same problem as it changing silently out from under them mid-session.
See the volumes: comment in docker-compose.yml.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("cloakbrowser.manager.extensions")

EXTENSIONS_DIR = Path("/data/extensions")

_cache: list[dict[str, Any]] | None = None


def _resolve_message(value: str, messages: dict[str, Any]) -> str:
    """Resolve a manifest string that's a `__MSG_key__` placeholder via a
    _locales/<default_locale>/messages.json dict. Returns `value` unchanged
    if it isn't a placeholder, or if the key isn't found (better a raw
    __MSG_..__ string in the UI than a crash on a malformed extension).
    """
    if not (value.startswith("__MSG_") and value.endswith("__")):
        return value
    key = value[len("__MSG_"):-len("__")]
    entry = messages.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("message"), str):
        return entry["message"]
    return value


def _load_messages(ext_dir: Path, default_locale: str | None) -> dict[str, Any]:
    if not default_locale:
        return {}
    messages_path = ext_dir / "_locales" / default_locale / "messages.json"
    try:
        return json.loads(messages_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _read_extension(ext_dir: Path) -> dict[str, Any] | None:
    """Read one extension's manifest.json into a listing entry, or None if
    ext_dir isn't a usable unpacked extension (no readable manifest.json —
    Chromium's own requirement for --load-extension)."""
    manifest_path = ext_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping %s: unreadable manifest.json (%s)", ext_dir, exc)
        return None

    messages = _load_messages(ext_dir, manifest.get("default_locale"))
    name = _resolve_message(manifest.get("name", ext_dir.name), messages)
    description = manifest.get("description")
    if isinstance(description, str):
        description = _resolve_message(description, messages)

    return {
        "id": ext_dir.name,
        "name": name,
        "description": description,
        "version": manifest.get("version"),
        "path": str(ext_dir.resolve()),
    }


def _scan() -> list[dict[str, Any]]:
    if not EXTENSIONS_DIR.is_dir():
        return []
    found = []
    for entry in sorted(EXTENSIONS_DIR.iterdir()):
        # Leading-dot directories (.gitkeep-style placeholders, editor swap
        # dirs) are conventionally not content, so skip them rather than log
        # a warning about every one missing a manifest.json.
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        parsed = _read_extension(entry)
        if parsed is not None:
            found.append(parsed)
    logger.info("Discovered %d extension(s) under %s", len(found), EXTENSIONS_DIR)
    return found


def list_available_extensions() -> list[dict[str, Any]]:
    """All usable extensions under EXTENSIONS_DIR, scanned once and cached."""
    global _cache
    if _cache is None:
        _cache = _scan()
    return _cache


def rescan_extensions() -> list[dict[str, Any]]:
    """Force a fresh scan, replacing the cached list.

    Manual and explicit only — an operator-triggered escape hatch (a UI
    "Rescan" button, or a fresh upload that needs to show up immediately),
    not a background watcher. The module docstring's case against
    auto-refreshing still holds; a human asking for it right now doesn't
    hit it, since nothing changes without them choosing that exact moment.
    """
    global _cache
    _cache = _scan()
    return _cache


def extension_paths_for(enabled_ids: list[str]) -> list[str]:
    """Filesystem paths for the subset of `enabled_ids` that are still
    available. Silently drops ids that no longer exist (a since-removed
    extension, or the container hasn't been restarted since one was added) —
    the whole point of the once-per-start cache is that a stale reference
    here is expected, not an error to surface at launch time."""
    available = {e["id"]: e["path"] for e in list_available_extensions()}
    return [available[eid] for eid in enabled_ids if eid in available]
