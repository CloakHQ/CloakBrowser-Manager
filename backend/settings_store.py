"""Persist Manager settings as a JSON file in the data dir.

Replaces hand-editing a ``.env`` for the native (frozen) app, where there is
no terminal and no reachable file next to a ``.app`` bundle. A plain dict keyed
by string — adding a future setting is just a new key, no schema migration.

Stored at ``<data_dir>/settings.json`` (the same cross-platform data dir the
profiles DB lives in — see runtime.default_data_dir). Written atomically.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .runtime import resolve_runtime


def _settings_path() -> Path:
    return resolve_runtime().data_dir / "settings.json"


def load_settings() -> dict:
    """Return the stored settings dict, or an empty dict if none/unreadable."""
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data: dict) -> None:
    """Atomically persist the settings dict (temp file + os.replace)."""
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
