"""Per-provider-key proxy state: current IP, expiry and rotation cooldown.

Providers hand out one proxy per key and rate-limit how often it can be rotated.
Without local state every launch either wasted a rotation or hit the provider's
limit. This store remembers what each key currently holds so the Manager can
reuse it, respect ``next_request_at``, and implement the profile rotation modes.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import database as db

_LOCK = threading.RLock()


def store_path() -> Path:
    return db.DATA_DIR / "proxy_key_state.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load() -> dict[str, Any]:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"keys": {}, "profiles": {}}
    if not isinstance(data, dict):
        return {"keys": {}, "profiles": {}}
    keys = data.get("keys")
    profiles = data.get("profiles")
    if not isinstance(keys, dict):
        keys = {}
    if not isinstance(profiles, dict):
        profiles = {}
    return {"keys": keys, "profiles": profiles}


def _save(data: dict[str, Any]) -> None:
    path = store_path()
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


def _to_ms(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return int(value if value > 1_000_000_000_000 else value * 1000)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _to_ms(int(text))
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _deadline_ms(entry: dict[str, Any]) -> int | None:
    """Provider ``next_request_at`` as an absolute deadline.

    KiotProxy sends epoch millis, TMProxy may send epoch seconds or a relative
    number of seconds, so anchor small values to when the proxy was assigned.
    """
    raw = entry.get("next_request_at")
    if isinstance(raw, bool) or raw is None:
        return None
    assigned = entry.get("assigned_at_ms") or entry.get("last_rotated_at_ms")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if raw <= 0:
            return None
        if raw > 1_000_000_000_000:
            return int(raw)
        if raw > 1_000_000_000:
            return int(raw * 1000)
        if assigned:
            return int(assigned) + int(raw * 1000)
        return _to_ms(raw)
    text = str(raw).strip()
    if text.isdigit():
        return _deadline_ms({**entry, "next_request_at": int(text)})
    return _to_ms(raw)


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": entry.get("provider"),
        "account_id": entry.get("account_id"),
        "key_id": entry.get("key_id"),
        "key_masked": entry.get("key_masked"),
        "http": entry.get("http"),
        "socks5": entry.get("socks5"),
        "host": entry.get("host"),
        "real_ip": entry.get("real_ip"),
        "location": entry.get("location"),
        "isp": entry.get("isp"),
        "expires_at": entry.get("expires_at"),
        "ttl": entry.get("ttl"),
        "ttc": entry.get("ttc"),
        "next_request_at": entry.get("next_request_at"),
        "assigned_at_ms": entry.get("assigned_at_ms"),
        "proxy_expires_at_ms": entry.get("proxy_expires_at_ms"),
        "last_used_at_ms": entry.get("last_used_at_ms"),
        "last_rotated_at_ms": entry.get("last_rotated_at_ms"),
        "released_at_ms": entry.get("released_at_ms"),
    }


def get(fingerprint: str) -> dict[str, Any] | None:
    with _LOCK:
        entry = _load()["keys"].get(fingerprint)
        return _public_entry(entry) if isinstance(entry, dict) else None


def last_used_at(fingerprint: str) -> int:
    entry = get(fingerprint)
    if not entry:
        return 0
    return int(entry.get("last_used_at_ms") or 0)


def set_current(
    fingerprint: str,
    provider: str,
    *,
    account_id: str | None,
    key_id: str | None,
    info: dict[str, Any],
    rotated: bool = True,
    key_masked: str | None = None,
) -> dict[str, Any]:
    """Record the proxy a key now holds. Never stores ``raw`` provider payloads.

    ``rotated=False`` is a verification refresh: keep the original assignment
    and rotation timestamps so the provider's cooldown is not reset locally.
    """
    with _LOCK:
        data = _load()
        previous = data["keys"].get(fingerprint) or {}
        now = _now_ms()
        entry = {
            "provider": provider,
            "account_id": account_id or previous.get("account_id"),
            "key_id": key_id or previous.get("key_id"),
            "key_masked": key_masked or previous.get("key_masked"),
            "http": info.get("http"),
            "socks5": info.get("socks5"),
            "host": info.get("host"),
            "real_ip": info.get("real_ip"),
            "location": info.get("location"),
            "isp": info.get("isp"),
            "expires_at": info.get("expires_at"),
            "ttl": info.get("ttl"),
            "ttc": info.get("ttc"),
            "next_request_at": info.get("next_request_at"),
            "assigned_at_ms": (now if rotated else previous.get("assigned_at_ms")) or now,
            "proxy_expires_at_ms": _to_ms(info.get("expires_at")),
            "last_used_at_ms": now,
            "last_rotated_at_ms": (
                now if rotated else previous.get("last_rotated_at_ms")
            ) or now,
            "released_at_ms": None,
        }
        data["keys"][fingerprint] = entry
        _save(data)
        return _public_entry(entry)


def touch_used(fingerprint: str) -> None:
    with _LOCK:
        data = _load()
        entry = data["keys"].get(fingerprint)
        if not isinstance(entry, dict):
            return
        entry["last_used_at_ms"] = _now_ms()
        entry["released_at_ms"] = None
        _save(data)


def mark_released(fingerprint: str) -> None:
    with _LOCK:
        data = _load()
        entry = data["keys"].get(fingerprint)
        if isinstance(entry, dict):
            entry["released_at_ms"] = _now_ms()
            _save(data)


def clear(fingerprint: str) -> None:
    with _LOCK:
        data = _load()
        data["keys"].pop(fingerprint, None)
        for profile_id, bound in list(data["profiles"].items()):
            if bound == fingerprint:
                data["profiles"].pop(profile_id, None)
        _save(data)


def bindings() -> dict[str, str]:
    with _LOCK:
        data = _load()
        return {
            profile_id: fingerprint
            for profile_id, fingerprint in data["profiles"].items()
            if isinstance(profile_id, str) and isinstance(fingerprint, str)
        }


def clear_account(account_id: str) -> None:
    with _LOCK:
        data = _load()
        removed = {
            fingerprint
            for fingerprint, entry in data["keys"].items()
            if isinstance(entry, dict) and entry.get("account_id") == account_id
        }
        for fingerprint in removed:
            data["keys"].pop(fingerprint, None)
        for profile_id, bound in list(data["profiles"].items()):
            if bound in removed:
                data["profiles"].pop(profile_id, None)
        _save(data)


def get_profile_fingerprint(profile_id: str) -> str | None:
    with _LOCK:
        bound = _load()["profiles"].get(profile_id)
    return bound if isinstance(bound, str) else None


def bind_profile(profile_id: str, fingerprint: str) -> None:
    with _LOCK:
        data = _load()
        data["profiles"][profile_id] = fingerprint
        _save(data)


def unbind_profile(profile_id: str) -> None:
    with _LOCK:
        data = _load()
        data["profiles"].pop(profile_id, None)
        _save(data)


def is_usable(entry: dict[str, Any] | None, now_ms: int | None = None) -> bool:
    if not entry or not entry.get("http"):
        return False
    expires = entry.get("proxy_expires_at_ms")
    if expires is None:
        expires = _to_ms(entry.get("expires_at"))
    if expires is not None and expires <= (now_ms or _now_ms()):
        return False
    return True


def cooldown_until(entry: dict[str, Any] | None) -> int | None:
    return _deadline_ms(entry) if entry else None


def cooldown_remaining(entry: dict[str, Any] | None, now_ms: int | None = None) -> int:
    deadline = cooldown_until(entry)
    if deadline is None:
        return 0
    return max(0, int((deadline - (now_ms or _now_ms())) / 1000))


def info(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": entry.get("provider"),
        "account_id": entry.get("account_id"),
        "key_id": entry.get("key_id"),
        "key_masked": entry.get("key_masked"),
        "http": entry.get("http"),
        "socks5": entry.get("socks5"),
        "host": entry.get("host"),
        "real_ip": entry.get("real_ip"),
        "location": entry.get("location"),
        "isp": entry.get("isp"),
        "expires_at": entry.get("expires_at"),
        "ttl": entry.get("ttl"),
        "ttc": entry.get("ttc"),
        "next_request_at": entry.get("next_request_at"),
    }
