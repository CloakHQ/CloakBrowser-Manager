"""Container-level self-check for the UI's diagnostics panel: GPU mode,
whether a license key is configured, and disk headroom on
the volume backing /data. Deliberately side-effect-free — it does not
launch a browser and does not validate the license key against CloakHQ,
both of which only happen for real at launch (see browser_manager.launch).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TypedDict

from .browser_manager import _chrome_gpu_mode

# Hardcoded, not probed at runtime: this is the exact version the Dockerfile
# installs (see its `wget .../v1.5.0/kasmvncserver_...` line) — the same
# convention the Dockerfile itself already uses rather than shelling out to
# `kasmvncserver --version` at request time for a value that cannot change
# without a rebuild anyway.
KASMVNC_VERSION = "1.5.0"


class SystemCheck(TypedDict):
    gpu_mode: str
    license_configured: bool
    kasmvnc_version: str
    disk_total_bytes: int
    disk_used_bytes: int
    disk_free_bytes: int
    disk_percent_used: float


def get_system_check(data_dir: Path) -> SystemCheck:
    usage = shutil.disk_usage(data_dir)
    percent_used = round(100 * usage.used / usage.total, 1) if usage.total else 0.0

    return {
        "gpu_mode": _chrome_gpu_mode(),
        # Whether the container has a key at all, not whether it is
        # VALID — that only gets checked for real when a browser actually
        # launches (missing is exit 77, over the plan's concurrent-session
        # limit is exit 76 — see browser_manager.py / README.md). A
        # per-profile override existing is not visible here, deliberately:
        # this panel is container-level, and a profile with its own key
        # is unaffected by the container having none.
        "license_configured": bool(os.environ.get("CLOAKBROWSER_LICENSE_KEY")),
        "kasmvnc_version": KASMVNC_VERSION,
        "disk_total_bytes": usage.total,
        "disk_used_bytes": usage.used,
        "disk_free_bytes": usage.free,
        "disk_percent_used": percent_used,
    }
