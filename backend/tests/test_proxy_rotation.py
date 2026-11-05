"""Rotation modes, provider cooldown and sticky/mutual proxy state."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from backend import (
    proxy_accounts,
    proxy_key_source,
    proxy_providers,
    proxy_rotation,
    proxy_state,
)


def _info(*, next_request_at: int | None = None, expires_at: int | None = None) -> dict:
    return {
        "provider": "kiotproxy",
        "http": "http://user:pass@1.2.3.4:8080",
        "socks5": None,
        "host": "1.2.3.4",
        "real_ip": "1.2.3.4",
        "location": "Hà Nội",
        "isp": None,
        "expires_at": expires_at,
        "ttl": 1200,
        "ttc": None,
        "next_request_at": next_request_at,
        "raw": {},
    }


def _profile(mode: str = "per_launch") -> dict:
    return {
        "id": "profile-1",
        "proxy_provider": "kiotproxy",
        "proxy_provider_key": "raw-key",
        "proxy_account_id": None,
        "proxy_key_id": None,
        "proxy_rotation_mode": mode,
        "kiotproxy_region": "bac",
        "tmproxy_location_id": 0,
        "tmproxy_isp_id": 0,
    }


def _account_key(value: str = "key-one") -> dict:
    return {
        "id": "key-1",
        "value": value,
        "value_masked": "key-…-one",
        "status": "ACTIVE",
        "expiration_at": int(time.time() * 1000) + 86_400_000,
        "deleted": False,
    }


@pytest.mark.asyncio
async def test_sticky_reuses_the_current_proxy(tmp_db, monkeypatch):
    new_proxy = AsyncMock(return_value=_info())
    monkeypatch.setattr(proxy_providers, "new_for", new_proxy)

    await proxy_rotation.acquire(_profile("sticky"))
    await proxy_rotation.acquire(_profile("sticky"))

    assert new_proxy.await_count == 1


@pytest.mark.asyncio
async def test_per_launch_rotates_even_when_the_current_proxy_is_usable(tmp_db, monkeypatch):
    new_proxy = AsyncMock(return_value=_info())
    monkeypatch.setattr(proxy_providers, "new_for", new_proxy)

    await proxy_rotation.acquire(_profile("per_launch"))
    await proxy_rotation.acquire(_profile("per_launch"))

    assert new_proxy.await_count == 2


@pytest.mark.asyncio
async def test_provider_cooldown_blocks_an_explicit_rotation(tmp_db, monkeypatch):
    now_ms = int(time.time() * 1000)
    info = _info(
        next_request_at=now_ms + 60_000,
        expires_at=now_ms + 3_600_000,
    )
    new_proxy = AsyncMock(return_value=info)
    monkeypatch.setattr(proxy_providers, "new_for", new_proxy)

    await proxy_rotation.acquire(_profile("per_launch"))
    # A launch inside the cooldown reuses the still-valid IP instead of failing.
    await proxy_rotation.acquire(_profile("per_launch"))
    assert new_proxy.await_count == 1

    with pytest.raises(proxy_rotation.ProxyRotationError, match="allows a new IP"):
        await proxy_rotation.acquire(_profile("per_launch"), force_rotate=True)


@pytest.mark.asyncio
async def test_sticky_stop_keeps_the_ip_and_the_binding(tmp_db, monkeypatch):
    monkeypatch.setattr(proxy_providers, "new_for", AsyncMock(return_value=_info()))
    release = AsyncMock()
    monkeypatch.setattr(proxy_providers, "release_for", release)

    info, selection, _mode, _rotated = await proxy_rotation.acquire(_profile("sticky"))
    assert info["http"]
    await proxy_rotation.release(
        "profile-1", "kiotproxy", selection.key, "sticky"
    )

    release.assert_not_awaited()
    assert proxy_state.get_profile_fingerprint("profile-1") == selection.fingerprint
    assert proxy_state.get(selection.fingerprint) is not None


@pytest.mark.asyncio
async def test_per_launch_stop_detaches_and_forgets_the_ip(tmp_db, monkeypatch):
    monkeypatch.setattr(proxy_providers, "new_for", AsyncMock(return_value=_info()))
    release = AsyncMock()
    monkeypatch.setattr(proxy_providers, "release_for", release)

    _info_value, selection, _mode, _rotated = await proxy_rotation.acquire(
        _profile("per_launch")
    )
    await proxy_rotation.release(
        "profile-1", "kiotproxy", selection.key, "per_launch"
    )

    release.assert_awaited_once_with("kiotproxy", selection.key)
    assert proxy_state.get_profile_fingerprint("profile-1") is None
    assert proxy_state.get(selection.fingerprint) is None


@pytest.mark.asyncio
async def test_deleting_a_profile_detaches_and_clears_the_ip(tmp_db, monkeypatch):
    monkeypatch.setattr(proxy_providers, "new_for", AsyncMock(return_value=_info()))
    release = AsyncMock()
    monkeypatch.setattr(proxy_providers, "release_for", release)

    _info_value, selection, _mode, _rotated = await proxy_rotation.acquire(
        _profile("sticky")
    )
    await proxy_rotation.release_profile(_profile("sticky"))

    release.assert_awaited_once_with("kiotproxy", selection.key)
    assert proxy_state.get(selection.fingerprint) is None


@pytest.mark.asyncio
async def test_changing_the_pinned_key_drops_the_sticky_binding(tmp_db, monkeypatch):
    account = proxy_accounts.add_account("kiotproxy", "api-token", "saved")

    async def fake_keys(_token: str):
        return [_account_key("key-one")]

    monkeypatch.setattr(proxy_providers, "kiotproxy_keys", fake_keys)
    monkeypatch.setattr(proxy_providers, "new_for", AsyncMock(return_value=_info()))

    profile = {
        **_profile("sticky"),
        "proxy_provider_key": None,
        "proxy_account_id": account["id"],
        "proxy_key_id": "key-1",
    }
    _info_value, selection, _mode, _rotated = await proxy_rotation.acquire(profile)
    assert proxy_state.get_profile_fingerprint("profile-1") == selection.fingerprint

    profile["proxy_account_id"] = "another-account"
    with pytest.raises(proxy_key_source.NoAvailableProxyKey):
        await proxy_rotation.acquire(profile)

    assert proxy_state.get_profile_fingerprint("profile-1") is None


@pytest.mark.asyncio
async def test_random_rotation_skips_cooling_keys(tmp_db, monkeypatch):
    account = proxy_accounts.add_account("kiotproxy", "api-token", "saved")

    async def fake_keys(_token: str):
        return [
            _account_key("key-one"),
            {**_account_key("key-two"), "id": "key-2"},
            {**_account_key("key-three"), "id": "key-3"},
        ]

    monkeypatch.setattr(proxy_providers, "kiotproxy_keys", fake_keys)
    profile = {
        **_profile("per_launch"),
        "proxy_provider_key": None,
        "proxy_account_id": account["id"],
        "proxy_key_id": "random",
    }
    now = int(time.time() * 1000)
    for index, value in enumerate(("key-one", "key-two"), start=1):
        proxy_state.set_current(
            proxy_key_source.key_fingerprint("kiotproxy", value),
            "kiotproxy",
            account_id=account["id"],
            key_id=f"key-{index}",
            info=_info(
                next_request_at=now + 60_000,
                expires_at=now + 3_600_000,
            ),
        )

    selection = await proxy_rotation.choose(profile, force_rotate=True)
    assert selection.key == "key-three"


@pytest.mark.asyncio
async def test_random_rotation_reports_the_shortest_wait_when_all_cooling(
    tmp_db, monkeypatch
):
    account = proxy_accounts.add_account("kiotproxy", "api-token", "saved")

    async def fake_keys(_token: str):
        return [
            _account_key("key-one"),
            {**_account_key("key-two"), "id": "key-2"},
        ]

    monkeypatch.setattr(proxy_providers, "kiotproxy_keys", fake_keys)
    profile = {
        **_profile("per_launch"),
        "proxy_provider_key": None,
        "proxy_account_id": account["id"],
        "proxy_key_id": "random",
    }
    now = int(time.time() * 1000)
    for index, (value, wait) in enumerate(
        (("key-one", 60_000), ("key-two", 30_000)), start=1
    ):
        proxy_state.set_current(
            proxy_key_source.key_fingerprint("kiotproxy", value),
            "kiotproxy",
            account_id=account["id"],
            key_id=f"key-{index}",
            info=_info(
                next_request_at=now + wait,
                expires_at=now + 3_600_000,
            ),
        )

    with pytest.raises(proxy_key_source.ProxyRotationCooldown) as raised:
        await proxy_rotation.choose(profile, force_rotate=True)
    assert 0 < raised.value.retry_after <= 30


@pytest.mark.asyncio
async def test_random_selects_the_least_recently_used_key(tmp_db, monkeypatch):
    account = proxy_accounts.add_account("kiotproxy", "api-token", "saved")

    async def fake_keys(_token: str):
        return [
            _account_key("key-one"),
            {**_account_key("key-two"), "id": "key-2"},
        ]

    monkeypatch.setattr(proxy_providers, "kiotproxy_keys", fake_keys)
    profile = {
        **_profile("per_launch"),
        "proxy_provider_key": None,
        "proxy_account_id": account["id"],
        "proxy_key_id": "random",
    }
    old = proxy_key_source.key_fingerprint("kiotproxy", "key-one")
    proxy_state.set_current(
        old,
        "kiotproxy",
        account_id=account["id"],
        key_id="key-1",
        info=_info(),
    )
    newer = proxy_key_source.key_fingerprint("kiotproxy", "key-two")
    proxy_state.set_current(
        newer,
        "kiotproxy",
        account_id=account["id"],
        key_id="key-2",
        info=_info(),
    )
    # Make key-one look older without hand-editing the store.
    data = proxy_state._load()
    data["keys"][old]["last_used_at_ms"] = 1
    proxy_state._save(data)

    selection = await proxy_rotation.choose(profile)
    assert selection.key == "key-one"
