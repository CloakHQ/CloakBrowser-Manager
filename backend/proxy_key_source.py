"""Resolve the provider key a profile should use for one launch.

Profiles can name a saved account/key instead of storing the raw key:

* ``proxy_key_id="random"`` — pick a free, active key from the saved accounts.
* ``proxy_key_id=<id>`` — pin one key from ``proxy_account_id``.

The raw key still wins when no saved selection is present, which keeps old
profiles working. Leases are in-memory only: a key that a running profile is
already using is never handed out again.
"""

from __future__ import annotations

import datetime
import hashlib
import random
import time
from typing import Any

from . import proxy_accounts, proxy_providers

RANDOM_KEY = "random"


class NoAvailableProxyKey(ValueError):
    """The profile asked for a saved key but none can serve this launch."""


def key_fingerprint(provider: str, key: str) -> str:
    """Mirror ``BrowserManager._proxy_fingerprint`` without importing the manager."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{provider}:{digest}"


def is_random_selection(profile: dict[str, Any]) -> bool:
    return (profile.get("proxy_key_id") or "").strip().lower() == RANDOM_KEY


def uses_saved_selection(profile: dict[str, Any]) -> bool:
    return bool(profile.get("proxy_account_id")) or is_random_selection(profile)


def _to_epoch_ms(value: Any) -> int | None:
    """Provider timestamps arrive as epoch seconds, epoch millis, or ISO text."""
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
        return _to_epoch_ms(int(text))
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _expired(value: Any, now_ms: int) -> bool:
    expires = _to_epoch_ms(value)
    return expires is not None and expires <= now_ms


def _kiot_key_usable(key: dict[str, Any], now_ms: int) -> bool:
    if key.get("deleted") or not key.get("value"):
        return False
    status = str(key.get("status") or "").strip().upper()
    if status and status != "ACTIVE":
        return False
    return not _expired(key.get("expiration_at"), now_ms)


def _account_label(account: dict[str, Any]) -> str | None:
    label = str(account.get("label") or "").strip()
    return label or None


def _error_option(account: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "account_id": account["id"],
        "account_label": _account_label(account),
        "account_masked": proxy_accounts.mask_secret(account.get("secret")),
        "key_id": None,
        "key_masked": None,
        "status": None,
        "expiration_at": None,
        "in_use": False,
        "error": message,
    }


async def available_options(
    provider: str, *, in_use: set[str] | None = None
) -> list[dict[str, Any]]:
    """Every selectable key, with account-level failures kept as error rows."""
    provider = (provider or "").strip().lower()
    if provider not in proxy_accounts.PROVIDERS:
        raise ValueError(f"Unknown proxy provider: {provider}")
    accounts = [a for a in proxy_accounts.list_accounts() if a["provider"] == provider]
    if not accounts:
        return []

    used = in_use or set()
    now_ms = int(time.time() * 1000)
    options: list[dict[str, Any]] = []

    for account in accounts:
        if provider == "kiotproxy":
            try:
                keys = await proxy_providers.kiotproxy_keys(account["secret"])
            except proxy_providers.ProxyProviderError as exc:
                options.append(_error_option(account, str(exc)))
                continue
            usable = [key for key in keys if _kiot_key_usable(key, now_ms)]
            if not usable:
                options.append(_error_option(account, "Tài khoản không có key còn hạn"))
                continue
            for key in usable:
                fingerprint = key_fingerprint(provider, key["value"])
                options.append(
                    {
                        "account_id": account["id"],
                        "account_label": _account_label(account),
                        "account_masked": proxy_accounts.mask_secret(account.get("secret")),
                        "key_id": key.get("id"),
                        "key_masked": key.get("value_masked"),
                        "status": key.get("status"),
                        "expiration_at": key.get("expiration_at"),
                        "in_use": fingerprint in used,
                        "error": None,
                    }
                )
            continue

        try:
            stats = await proxy_providers.tmproxy_stats(account["secret"])
        except proxy_providers.ProxyProviderError as exc:
            options.append(_error_option(account, str(exc)))
            continue
        expiration = stats.get("expired_at")
        if _expired(expiration, now_ms):
            options.append(_error_option(account, "Key TMProxy đã hết hạn"))
            continue
        fingerprint = key_fingerprint(provider, account["secret"])
        options.append(
            {
                "account_id": account["id"],
                "account_label": _account_label(account),
                "account_masked": proxy_accounts.mask_secret(account.get("secret")),
                "key_id": account["id"],
                "key_masked": proxy_accounts.mask_secret(account.get("secret")),
                "status": stats.get("plan"),
                "expiration_at": expiration,
                "in_use": fingerprint in used,
                "error": None,
            }
        )

    return options


def _resolve_account(provider: str, account_id: str | None) -> dict[str, Any]:
    if not account_id:
        raise NoAvailableProxyKey("No saved proxy account was selected")
    account = proxy_accounts.get_account(account_id)
    if account is None:
        raise NoAvailableProxyKey("The selected proxy account no longer exists")
    if account["provider"] != provider:
        raise NoAvailableProxyKey("The selected proxy account belongs to another provider")
    return account


async def _pick_kiot(
    account: dict[str, Any],
    key_id: str,
    *,
    exclude: set[str],
) -> str:
    try:
        keys = await proxy_providers.kiotproxy_keys(account["secret"])
    except proxy_providers.ProxyProviderError as exc:
        raise NoAvailableProxyKey(str(exc)) from exc

    now_ms = int(time.time() * 1000)
    usable = [key for key in keys if _kiot_key_usable(key, now_ms)]

    if key_id != RANDOM_KEY:
        selected = next((key for key in usable if key.get("id") == key_id), None)
        if selected is None:
            raise NoAvailableProxyKey(
                "The selected KiotProxy key is missing, inactive, or expired"
            )
        if key_fingerprint("kiotproxy", selected["value"]) in exclude:
            raise NoAvailableProxyKey(
                "The selected KiotProxy key is already in use by another running profile"
            )
        return selected["value"]

    candidates = [
        key
        for key in usable
        if key_fingerprint("kiotproxy", key["value"]) not in exclude
    ]
    if not candidates:
        raise NoAvailableProxyKey(
            "No free KiotProxy key is available. Every key is expired or already "
            "used by another running profile."
        )
    return random.choice(candidates)["value"]


async def _pick_tm(
    accounts: list[dict[str, Any]],
    key_id: str,
    *,
    exclude: set[str],
) -> str:
    now_ms = int(time.time() * 1000)
    candidates: list[tuple[str, dict[str, Any]]] = []
    errors: list[str] = []

    for account in accounts:
        if key_id != RANDOM_KEY and key_id != account["id"]:
            continue
        try:
            stats = await proxy_providers.tmproxy_stats(account["secret"])
        except proxy_providers.ProxyProviderError as exc:
            errors.append(str(exc))
            continue
        if _expired(stats.get("expired_at"), now_ms):
            continue
        if key_fingerprint("tmproxy", account["secret"]) in exclude:
            if key_id != RANDOM_KEY:
                raise NoAvailableProxyKey(
                    "The selected TMProxy key is already in use by another running profile"
                )
            continue
        candidates.append((account["secret"], account))

    if key_id != RANDOM_KEY:
        if candidates:
            return candidates[0][0]
        detail = f" ({errors[0]})" if errors else ""
        raise NoAvailableProxyKey(
            f"The selected TMProxy key is missing or expired{detail}"
        )
    if not candidates:
        detail = f" Last error: {errors[0]}" if errors else ""
        raise NoAvailableProxyKey(
            "No free TMProxy key is available. Every key is expired or already "
            f"used by another running profile.{detail}"
        )
    return random.choice(candidates)[0]


async def resolve_profile_key(
    profile: dict[str, Any],
    *,
    exclude_fingerprints: set[str] | None = None,
) -> str:
    """Return the raw provider key for this launch (never persisted for saved keys)."""
    provider = (profile.get("proxy_provider") or "none").strip().lower()
    if provider not in proxy_accounts.PROVIDERS:
        raise NoAvailableProxyKey(f"Unknown proxy provider: {provider}")

    exclude = exclude_fingerprints or set()
    account_id = (profile.get("proxy_account_id") or "").strip()
    key_id = (profile.get("proxy_key_id") or "").strip() or RANDOM_KEY

    if account_id:
        account = _resolve_account(provider, account_id)
        if provider == "kiotproxy":
            return await _pick_kiot(account, key_id, exclude=exclude)
        return await _pick_tm([account], key_id, exclude=exclude)

    if key_id == RANDOM_KEY:
        accounts = [
            account
            for account in proxy_accounts.list_accounts()
            if account["provider"] == provider
        ]
        if not accounts:
            raise NoAvailableProxyKey(
                f"No saved {provider} accounts. Add one on the Providers page "
                "or enter a key manually."
            )
        if provider == "kiotproxy":
            candidates: list[str] = []
            errors: list[str] = []
            now_ms = int(time.time() * 1000)
            for account in accounts:
                try:
                    keys = await proxy_providers.kiotproxy_keys(account["secret"])
                except proxy_providers.ProxyProviderError as exc:
                    errors.append(str(exc))
                    continue
                candidates.extend(
                    key["value"]
                    for key in keys
                    if _kiot_key_usable(key, now_ms)
                    and key_fingerprint(provider, key["value"]) not in exclude
                )
            if not candidates:
                detail = f" Last error: {errors[0]}" if errors else ""
                raise NoAvailableProxyKey(
                    "No free KiotProxy key is available. Every key is expired or "
                    f"already used by another running profile.{detail}"
                )
            return random.choice(candidates)
        return await _pick_tm(accounts, RANDOM_KEY, exclude=exclude)

    raise NoAvailableProxyKey(
        "Choose a saved proxy key or enter the provider key manually"
    )
