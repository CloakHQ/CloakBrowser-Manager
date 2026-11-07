"""Saved account/key selection for profiles (random or pinned keys)."""

from __future__ import annotations

import time

import pytest

from backend import proxy_accounts, proxy_key_source, proxy_providers


def _key(
    key_id: str,
    value: str,
    *,
    offset_ms: int = 86_400_000,
    status: str = "ACTIVE",
    deleted: bool = False,
) -> dict:
    return {
        "id": key_id,
        "value": value,
        "value_masked": f"{value[:5]}…{value[-4:]}",
        "status": status,
        "expiration_at": int(time.time() * 1000) + offset_ms,
        "deleted": deleted,
        "description": None,
        "package_id": None,
        "package_period_id": None,
        "quantity_day": None,
    }


def _account(provider: str = "kiotproxy", secret: str = "api-token") -> dict:
    return proxy_accounts.add_account(provider, secret, "Tài khoản thử")


def _profile(
    account_id: str | None, key_id: str, provider: str = "kiotproxy"
) -> dict:
    return {
        "id": "profile-1",
        "proxy_provider": provider,
        "proxy_account_id": account_id,
        "proxy_key_id": key_id,
        "proxy_provider_key": None,
    }


@pytest.mark.asyncio
async def test_available_options_hides_expired_and_marks_in_use(tmp_db, monkeypatch):
    account = _account()

    async def fake_keys(_token: str):
        return [
            _key("free", "free-key"),
            _key("used", "used-key"),
            _key("expired", "expired-key", offset_ms=-1),
            _key("deleted", "deleted-key", deleted=True),
        ]

    monkeypatch.setattr(proxy_providers, "kiotproxy_keys", fake_keys)
    used = {proxy_key_source.key_fingerprint("kiotproxy", "used-key")}

    options = await proxy_key_source.available_options("kiotproxy", in_use=used)

    by_id = {option["key_id"]: option for option in options}
    assert set(by_id) == {"free", "used"}
    assert by_id["free"]["in_use"] is False
    assert by_id["used"]["in_use"] is True
    assert by_id["free"]["account_id"] == account["id"]
    assert by_id["free"]["key_masked"] == "free-…-key"


@pytest.mark.asyncio
async def test_random_selection_skips_a_key_already_in_use(tmp_db, monkeypatch):
    account = _account()

    async def fake_keys(_token: str):
        return [_key("busy", "busy-key"), _key("free", "free-key")]

    monkeypatch.setattr(proxy_providers, "kiotproxy_keys", fake_keys)
    used = {proxy_key_source.key_fingerprint("kiotproxy", "busy-key")}

    key = await proxy_key_source.resolve_profile_key(
        _profile(account["id"], "random"), exclude_fingerprints=used
    )

    assert key == "free-key"


@pytest.mark.asyncio
async def test_specific_selection_returns_its_key(tmp_db, monkeypatch):
    account = _account()

    async def fake_keys(_token: str):
        return [_key("one", "key-one"), _key("two", "key-two")]

    monkeypatch.setattr(proxy_providers, "kiotproxy_keys", fake_keys)

    key = await proxy_key_source.resolve_profile_key(_profile(account["id"], "two"))

    assert key == "key-two"


@pytest.mark.asyncio
async def test_random_selection_fails_when_every_key_is_in_use(tmp_db, monkeypatch):
    account = _account()

    async def fake_keys(_token: str):
        return [_key("one", "key-one")]

    monkeypatch.setattr(proxy_providers, "kiotproxy_keys", fake_keys)
    used = {proxy_key_source.key_fingerprint("kiotproxy", "key-one")}

    with pytest.raises(proxy_key_source.NoAvailableProxyKey, match="No free"):
        await proxy_key_source.resolve_profile_key(
            _profile(account["id"], "random"), exclude_fingerprints=used
        )


@pytest.mark.asyncio
async def test_specific_selection_rejects_a_key_already_in_use(tmp_db, monkeypatch):
    account = _account()

    async def fake_keys(_token: str):
        return [_key("one", "key-one")]

    monkeypatch.setattr(proxy_providers, "kiotproxy_keys", fake_keys)
    used = {proxy_key_source.key_fingerprint("kiotproxy", "key-one")}

    with pytest.raises(proxy_key_source.NoAvailableProxyKey, match="already in use"):
        await proxy_key_source.resolve_profile_key(
            _profile(account["id"], "one"), exclude_fingerprints=used
        )


@pytest.mark.asyncio
async def test_tmproxy_random_skips_an_expired_account(tmp_db, monkeypatch):
    expired = _account("tmproxy", "expired-token")
    live = _account("tmproxy", "live-token")

    async def fake_stats(token: str):
        return {
            "expired_at": int(time.time() * 1000) + (-1000 if token == "expired-token" else 86_400_000),
            "plan": "VIP",
        }

    monkeypatch.setattr(proxy_providers, "tmproxy_stats", fake_stats)

    key = await proxy_key_source.resolve_profile_key(
        _profile(None, "random", provider="tmproxy")
    )

    assert key == "live-token"
