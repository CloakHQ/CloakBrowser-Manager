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
import time
from dataclasses import dataclass
from typing import Any

from . import proxy_accounts, proxy_providers, proxy_state

RANDOM_KEY = "random"


@dataclass(frozen=True)
class ProxyKeySelection:
    provider: str
    key: str
    account_id: str | None = None
    key_id: str | None = None
    label: str | None = None
    key_masked: str | None = None

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.provider, self.key)



class NoAvailableProxyKey(ValueError):
    """The profile asked for a saved key but none can serve this launch."""


class ProxyRotationCooldown(NoAvailableProxyKey):
    """Every eligible key is inside the provider's rotation cooldown."""

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = max(0, int(retry_after))


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
        "fingerprint": None,
        "status": None,
        "expiration_at": None,
        "cooldown_remaining": 0,
        "eligible": False,
        "in_use": False,
        "error": message,
    }


def _option(
    account: dict[str, Any],
    *,
    key_id: str | None,
    key_masked: str | None,
    fingerprint: str,
    status: str | None,
    expiration_at: int | str | None,
    in_use: bool,
) -> dict[str, Any]:
    cooldown = proxy_state.cooldown_remaining(proxy_state.get(fingerprint))
    return {
        "account_id": account["id"],
        "account_label": _account_label(account),
        "account_masked": proxy_accounts.mask_secret(account.get("secret")),
        "key_id": key_id,
        "key_masked": key_masked,
        "fingerprint": fingerprint,
        "status": status,
        "expiration_at": expiration_at,
        "cooldown_remaining": cooldown,
        "eligible": bool(key_id) and not in_use and cooldown == 0,
        "in_use": in_use,
        "error": None,
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
                    _option(
                        account,
                        key_id=key.get("id"),
                        key_masked=key.get("value_masked"),
                        fingerprint=fingerprint,
                        status=key.get("status"),
                        expiration_at=key.get("expiration_at"),
                        in_use=fingerprint in used,
                    )
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
            _option(
                account,
                key_id=account["id"],
                key_masked=proxy_accounts.mask_secret(account["secret"]),
                fingerprint=fingerprint,
                status=stats.get("plan"),
                expiration_at=expiration,
                in_use=fingerprint in used,
            )
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
    require_rotation_ready: bool = False,
) -> ProxyKeySelection:
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
        fingerprint = key_fingerprint("kiotproxy", selected["value"])
        if fingerprint in exclude:
            raise NoAvailableProxyKey(
                "The selected KiotProxy key is already in use by another running profile"
            )
        remaining = proxy_state.cooldown_remaining(proxy_state.get(fingerprint))
        if require_rotation_ready and remaining > 0:
            raise ProxyRotationCooldown(
                f"The selected KiotProxy key can get a new IP in {remaining}s",
                remaining,
            )
        return ProxyKeySelection(
            provider="kiotproxy",
            key=selected["value"],
            account_id=account["id"],
            key_id=selected.get("id"),
            label=_account_label(account),
            key_masked=selected.get("value_masked"),
        )

    candidates = [
        ProxyKeySelection(
            provider="kiotproxy",
            key=key["value"],
            account_id=account["id"],
            key_id=key.get("id"),
            label=_account_label(account),
            key_masked=key.get("value_masked"),
        )
        for key in usable
        if key_fingerprint("kiotproxy", key["value"]) not in exclude
    ]
    return _ready_candidate(candidates, require_rotation_ready, "KiotProxy")


async def _pick_tm(
    accounts: list[dict[str, Any]],
    key_id: str,
    *,
    exclude: set[str],
    require_rotation_ready: bool = False,
) -> ProxyKeySelection:
    now_ms = int(time.time() * 1000)
    candidates: list[ProxyKeySelection] = []
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
        fingerprint = key_fingerprint("tmproxy", account["secret"])
        if fingerprint in exclude:
            if key_id != RANDOM_KEY:
                raise NoAvailableProxyKey(
                    "The selected TMProxy key is already in use by another running profile"
                )
            continue
        candidates.append(
            ProxyKeySelection(
                provider="tmproxy",
                key=account["secret"],
                account_id=account["id"],
                key_id=account["id"],
                label=_account_label(account),
                key_masked=proxy_accounts.mask_secret(account["secret"]),
            )
        )

    if key_id != RANDOM_KEY:
        if candidates:
            selection = candidates[0]
            remaining = proxy_state.cooldown_remaining(
                proxy_state.get(selection.fingerprint)
            )
            if require_rotation_ready and remaining > 0:
                raise ProxyRotationCooldown(
                    f"The selected TMProxy key can get a new IP in {remaining}s",
                    remaining,
                )
            return selection
        detail = f" ({errors[0]})" if errors else ""
        raise NoAvailableProxyKey(
            f"The selected TMProxy key is missing or expired{detail}"
        )
    return _ready_candidate(candidates, require_rotation_ready, "TMProxy")


def _ready_candidate(
    candidates: list[ProxyKeySelection],
    require_rotation_ready: bool,
    provider_label: str,
) -> ProxyKeySelection:
    if not candidates:
        raise NoAvailableProxyKey(
            f"No free {provider_label} key is available. Every key is expired or "
            "already used by another running profile."
        )
    if not require_rotation_ready:
        return _least_recently_used(candidates)

    ready = [
        candidate
        for candidate in candidates
        if proxy_state.cooldown_remaining(proxy_state.get(candidate.fingerprint)) == 0
    ]
    if ready:
        return _least_recently_used(ready)

    wait = min(
        proxy_state.cooldown_remaining(proxy_state.get(candidate.fingerprint))
        for candidate in candidates
    )
    raise ProxyRotationCooldown(
        f"Every free {provider_label} key is cooling down; the next one can "
        f"rotate in {wait}s.",
        wait,
    )


async def select_profile_key(
    profile: dict[str, Any],
    *,
    exclude_fingerprints: set[str] | None = None,
    account_id: str | None = None,
    key_id: str | None = None,
    require_rotation_ready: bool = False,
) -> ProxyKeySelection:
    """Pick a key for this launch, preferring the least recently used one.

    ``account_id``/``key_id`` override the profile selection so a sticky binding
    can be resolved again after a restart. ``require_rotation_ready`` skips keys
    that are still inside the provider's rotation cooldown.
    """
    provider = (profile.get("proxy_provider") or "none").strip().lower()
    if provider not in proxy_accounts.PROVIDERS:
        raise NoAvailableProxyKey(f"Unknown proxy provider: {provider}")

    exclude = exclude_fingerprints or set()
    account_id = (account_id or profile.get("proxy_account_id") or "").strip()
    key_id = (key_id or profile.get("proxy_key_id") or "").strip() or RANDOM_KEY

    if account_id:
        account = _resolve_account(provider, account_id)
        if provider == "kiotproxy":
            return await _pick_kiot(
                account,
                key_id,
                exclude=exclude,
                require_rotation_ready=require_rotation_ready,
            )
        return await _pick_tm(
            [account],
            key_id,
            exclude=exclude,
            require_rotation_ready=require_rotation_ready,
        )

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
            candidates: list[ProxyKeySelection] = []
            errors: list[str] = []
            now_ms = int(time.time() * 1000)
            for account in accounts:
                try:
                    keys = await proxy_providers.kiotproxy_keys(account["secret"])
                except proxy_providers.ProxyProviderError as exc:
                    errors.append(str(exc))
                    continue
                candidates.extend(
                    ProxyKeySelection(
                        provider="kiotproxy",
                        key=key["value"],
                        account_id=account["id"],
                        key_id=key.get("id"),
                        label=_account_label(account),
                        key_masked=key.get("value_masked"),
                    )
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
            return _ready_candidate(candidates, require_rotation_ready, "KiotProxy")
        return await _pick_tm(
            accounts,
            RANDOM_KEY,
            exclude=exclude,
            require_rotation_ready=require_rotation_ready,
        )

    raise NoAvailableProxyKey(
        "Choose a saved proxy key or enter the provider key manually"
    )


async def resolve_profile_key(
    profile: dict[str, Any],
    *,
    exclude_fingerprints: set[str] | None = None,
    account_id: str | None = None,
    key_id: str | None = None,
) -> str:
    """Return the raw provider key for this launch (never persisted for saved keys)."""
    selection = await select_profile_key(
        profile,
        exclude_fingerprints=exclude_fingerprints,
        account_id=account_id,
        key_id=key_id,
    )
    return selection.key


def _least_recently_used(
    candidates: list[ProxyKeySelection],
) -> ProxyKeySelection:
    return min(candidates, key=lambda item: proxy_state.last_used_at(item.fingerprint))

