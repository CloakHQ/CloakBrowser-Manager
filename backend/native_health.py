"""How fast each profile's exit is, and how that compares with what Facebook needs.

The Manager launches native profiles through cloak, so the proxy lives in ``~/.cloakbrowser/proxies.json`` (by ref)
and the last measured exit lives in the profile's ``cloak.json``. This module only reads those files: no browser, no
network, no secret. The numbers come from the last speed test cloak ran (``cloak proxy check --speed`` refreshes it).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CLOAK_HOME = Path(os.getenv("CLOAK_HOME", Path.home() / ".cloakbrowser"))
PROFILES_ROOT = Path(os.getenv("CLOAK_NATIVE_PROFILES_ROOT", CLOAK_HOME / "profiles"))

# Mirrors cloak-browser lib/floppydata.mjs MIN_SPEED. Facebook did not draw at ~50 KB/s with a 4 s handshake; 100 KB/s
# and a 2.5 s TLS handshake is the floor a page needs to paint while a person watches.
EXPECTATION = {"kbps": 100, "tls_sec": 2.5}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _record_path(name: str) -> Path:
    """profiles/<group>/<name>/cloak.json, or the older flat profiles/<name>/cloak.json."""
    flat = PROFILES_ROOT / name / "cloak.json"
    if flat.exists():
        return flat
    return PROFILES_ROOT / name.split("-")[0] / name / "cloak.json"


def _native_name(profile: dict[str, Any]) -> str | None:
    for arg in profile.get("launch_args") or []:
        if arg.startswith("--native-profile="):
            return arg.partition("=")[2] or None
    return None


def _verdict(kbps: float | None, tls_sec: float | None) -> str:
    if kbps is None:
        return "unmeasured"
    if kbps < EXPECTATION["kbps"]:
        return "slow"
    if tls_sec is not None and tls_sec > EXPECTATION["tls_sec"]:
        return "handshake"
    return "good"


def _row(profile: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any] | None:
    name = _native_name(profile)
    if not name:
        return None
    record = _read_json(_record_path(name))
    ref = ((record.get("browser") or {}).get("proxy") or {}).get("ref")
    if not ref:
        return None
    entry = registry.get(ref) or {}
    speed = entry.get("speed") or {}
    kbps = speed.get("kbps") if isinstance(speed.get("kbps"), (int, float)) else None
    tls_sec = speed.get("tlsSec") if isinstance(speed.get("tlsSec"), (int, float)) else None
    exit_info = record.get("history", {}).get("exit") or {}
    record_browser = record.get("browser") or {}
    expected_tz = record_browser.get("timezone") or None
    exit_tz = exit_info.get("timezone") or None
    score = entry.get("lastScore") or {}
    return {
        "profile": name,
        "profile_id": profile.get("id"),
        "status": profile.get("status"),
        "proxy": ref,
        "server": entry.get("server"),
        "ip": exit_info.get("ip") or entry.get("ip"),
        "family": exit_info.get("family"),
        "drifted": exit_info.get("drifted") or [],
        "country": exit_info.get("country") or entry.get("country"),
        "city": exit_info.get("city") or entry.get("city"),
        "timezone": exit_tz,
        "profile_timezone": expected_tz,
        # Does the exit sit where the profile says it should? A US clock on a French exit is the mismatch a login sees.
        "place_match": None if not (exit_tz and expected_tz) else exit_tz == expected_tz,
        "network": entry.get("network"),
        "kbps": round(kbps) if kbps is not None else None,
        "tls_sec": round(tls_sec, 2) if tls_sec is not None else None,
        "ratio": round(kbps / EXPECTATION["kbps"], 3) if kbps is not None else None,
        "verdict": _verdict(kbps, tls_sec),
        "measured_at": score.get("at"),
        "reputation": score.get("verdict"),
    }


def proxy_health(profiles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One row per native profile that has a proxy, plus the expectation each is judged against.

    A profile with no proxy, or with no measured speed, is reported with ``verdict`` "unmeasured" rather than hidden:
    "we have not looked" and "we looked and it is slow" are different answers.
    """
    registry = _read_json(CLOAK_HOME / "proxies.json")
    rows: list[dict[str, Any]] = []
    for profile in profiles or []:
        row = _row(profile, registry)
        if row:
            rows.append(row)
    # Worst first, so the badge opens on the line that needs attention.
    order = {"slow": 0, "handshake": 1, "unmeasured": 2, "good": 3}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), r["ratio"] if r["ratio"] is not None else 9))
    return {
        "expectation": EXPECTATION,
        "at": datetime.now(timezone.utc).isoformat(),
        "profiles": rows,
    }
