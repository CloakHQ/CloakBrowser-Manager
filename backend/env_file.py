"""Load a `.env` file from the manager root into os.environ (set once, after clone).

Dependency-free. Values already present in the environment win (so Docker's
injected container env, or a real shell export, overrides the file). Used for
the app-wide CloakBrowser license key and release channel — see README.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _default_env_path() -> Path:
    # Frozen native app: look for a .env next to the executable (optional —
    # native users set the key in-app; see settings_store). From source, the
    # manager root is one level up from the backend package.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return Path(__file__).resolve().parent.parent / ".env"


_ENV_PATH = _default_env_path()


def load_env_file(path: Path = _ENV_PATH) -> None:
    """Read `KEY=VALUE` lines from `path` into os.environ without overriding."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
