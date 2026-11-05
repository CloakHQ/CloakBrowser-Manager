"""Stored credentials for proxy providers (KiotProxy / TMProxy).

A profile can already paste a provider key in its own form. This store is the
account-level side of the same providers: one KiotProxy API token can list every
key on the account, and one TMProxy API key reports its own plan/expiry. Keeping
them here means the Manager can show what a key is, when it dies and let the
user extend/rotate/delete it without opening the provider's website.

Secrets never leave the process through the API: entries are addressed by a
short hash and listed masked, exactly like the license key pool. Stored at
``<data_dir>/proxy_accounts.json`` and written atomically.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

from . import database as db

PROVIDERS = ("kiotproxy", "tmproxy")

_LOCK = threading.RLock()


def store_path() -> Path:
    """Resolved at call time so tests (and a moved data dir) are honoured."""
    return db.DATA_DIR / "proxy_accounts.json"


def account_id(provider: str, secret: str) -> str:
    """Stable public handle for a credential — never the secret itself."""
    digest = hashlib.sha256(f"{provider}:{secret}".encode("utf-8")).hexdigest()
    return digest[:12]


def mask_secret(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 9:
        return "•" * len(secret)
    return f"{secret[:5]}…{secret[-4:]}"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _normalize(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    provider = raw.get("provider")
    secret = raw.get("secret")
    if provider not in PROVIDERS:
        return None
    if not isinstance(secret, str) or not secret.strip():
        return None
    secret = secret.strip()
    return {
        "id": account_id(provider, secret),
        "provider": provider,
        "label": raw.get("label") if isinstance(raw.get("label"), str) else None,
        "secret": secret,
        "added_at": raw.get("added_at") if isinstance(raw.get("added_at"), str) else _now(),
        "checked_at": raw.get("checked_at") if isinstance(raw.get("checked_at"), str) else None,
    }


def _load() -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"accounts": []}
    if not isinstance(data, dict):
        return {"accounts": []}
    seen: set[str] = set()
    accounts: list[dict] = []
    for raw in data.get("accounts") or []:
        entry = _normalize(raw)
        if entry and entry["id"] not in seen:
            seen.add(entry["id"])
            accounts.append(entry)
    return {"accounts": accounts}


def _save(data: dict) -> None:
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


def public_account(account: dict) -> dict:
    return {
        "id": account.get("id"),
        "provider": account.get("provider"),
        "label": account.get("label"),
        "secret_masked": mask_secret(account.get("secret")),
        "added_at": account.get("added_at"),
        "checked_at": account.get("checked_at"),
    }


def list_accounts() -> list[dict]:
    with _LOCK:
        return _load()["accounts"]


def get_account(entry_id: str) -> dict | None:
    with _LOCK:
        for account in _load()["accounts"]:
            if account["id"] == entry_id:
                return account
    return None


def add_account(provider: str, secret: str, label: str | None = None) -> dict:
    provider = (provider or "").strip().lower()
    secret = (secret or "").strip()
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown proxy provider: {provider}")
    if not secret:
        raise ValueError("A provider credential is required")
    with _LOCK:
        data = _load()
        entry = _normalize(
            {"provider": provider, "secret": secret, "label": label, "added_at": _now()}
        )
        assert entry is not None
        for index, existing in enumerate(data["accounts"]):
            if existing["id"] == entry["id"]:
                # Same credential again: keep its history, refresh the label.
                existing["label"] = label if label is not None else existing.get("label")
                data["accounts"][index] = existing
                _save(data)
                return existing
        data["accounts"].append(entry)
        _save(data)
        return entry


def delete_account(entry_id: str) -> bool:
    with _LOCK:
        data = _load()
        remaining = [a for a in data["accounts"] if a["id"] != entry_id]
        if len(remaining) == len(data["accounts"]):
            return False
        data["accounts"] = remaining
        _save(data)
        return True


def touch_account(entry_id: str) -> None:
    with _LOCK:
        data = _load()
        for account in data["accounts"]:
            if account["id"] == entry_id:
                account["checked_at"] = _now()
                _save(data)
                return
