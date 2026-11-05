"""Launch/stop wiring for KiotProxy and TMProxy profiles."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend import browser_manager as module
from backend import proxy_providers
from backend.browser_manager import BrowserManager, SEARCH_ENGINE_MARKER
from backend.runtime import RuntimeConfig

NATIVE_RUNTIME = RuntimeConfig(
    host_os="windows",
    runtime_mode="native",
    viewer_mode="native-window",
    data_dir=Path("C:/manager-data"),
)


def _profile(tmp_path: Path, name: str, provider: str = "kiotproxy") -> dict:
    user_data_dir = tmp_path / name
    user_data_dir.mkdir(parents=True, exist_ok=True)
    (user_data_dir / SEARCH_ENGINE_MARKER).write_text("google\n")
    return {
        "id": name,
        "name": name,
        "user_data_dir": str(user_data_dir),
        "screen_width": 1920,
        "screen_height": 1080,
        "launch_args": [],
        "set_google_default": False,
        "proxy_provider": provider,
        "proxy_provider_key": f"{provider}-key",
        "proxy_account_id": None,
        "proxy_key_id": None,
        "kiotproxy_region": "bac",
        "tmproxy_location_id": 9,
        "tmproxy_isp_id": 1,
    }


def _fake_context() -> MagicMock:
    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    context.close = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_launch_uses_provider_proxy_and_stop_releases_it(
    tmp_db, monkeypatch, tmp_path: Path
):
    seen: list[dict] = []
    new_proxy = AsyncMock(
        return_value={
            "provider": "kiotproxy",
            "http": "http://171.229.1.2:39008",
            "socks5": "socks5://171.229.1.2:39009",
            "host": "171.229.1.2",
            "real_ip": "171.229.1.2",
            "location": "Phú Thọ",
            "isp": None,
            "expires_at": 1,
            "ttl": 1200,
            "ttc": 59,
            "next_request_at": 2,
            "raw": {},
        }
    )
    release = AsyncMock()
    monkeypatch.setattr(proxy_providers, "new_for", new_proxy)
    monkeypatch.setattr(proxy_providers, "release_for", release)

    async def launch(**kwargs):
        seen.append(kwargs)
        return _fake_context()

    monkeypatch.setattr(module, "launch_persistent_context_async", launch)
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    running = await manager.launch(_profile(tmp_path, "a"))

    assert seen[0]["proxy"] == "http://171.229.1.2:39008"
    assert new_proxy.await_args.kwargs["region"] == "bac"
    assert running.proxy_provider == "kiotproxy"
    status = manager.get_status("a")
    assert status["active_proxy"]["location"] == "Phú Thọ"
    assert "http" not in status["active_proxy"]

    await manager.stop("a")
    release.assert_awaited_once_with("kiotproxy", "kiotproxy-key")


@pytest.mark.asyncio
async def test_two_profiles_cannot_share_one_provider_key(tmp_db, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        proxy_providers,
        "new_for",
        AsyncMock(
            return_value={
                "provider": "kiotproxy",
                "http": "http://1.2.3.4:1",
                "socks5": None,
                "host": "1.2.3.4",
                "real_ip": "1.2.3.4",
                "location": "Hà Nội",
                "isp": None,
                "expires_at": None,
                "ttl": None,
                "ttc": None,
                "next_request_at": None,
                "raw": {},
            }
        ),
    )
    monkeypatch.setattr(proxy_providers, "release_for", AsyncMock())
    monkeypatch.setattr(
        module, "launch_persistent_context_async", AsyncMock(return_value=_fake_context())
    )
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    await manager.launch(_profile(tmp_path, "a"))

    with pytest.raises(ValueError, match="already in use"):
        await manager.launch(_profile(tmp_path, "b"))


@pytest.mark.asyncio
async def test_failed_launch_releases_the_provider_proxy(tmp_db, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        proxy_providers,
        "new_for",
        AsyncMock(
            return_value={
                "provider": "kiotproxy",
                "http": "http://1.2.3.4:1",
                "socks5": None,
                "host": "1.2.3.4",
                "real_ip": "1.2.3.4",
                "location": "Hà Nội",
                "isp": None,
                "expires_at": None,
                "ttl": None,
                "ttc": None,
                "next_request_at": None,
                "raw": {},
            }
        ),
    )
    release = AsyncMock()
    monkeypatch.setattr(proxy_providers, "release_for", release)
    monkeypatch.setattr(
        module,
        "launch_persistent_context_async",
        AsyncMock(side_effect=RuntimeError("browser did not start")),
    )
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    with pytest.raises(RuntimeError):
        await manager.launch(_profile(tmp_path, "a"))

    release.assert_awaited_once_with("kiotproxy", "kiotproxy-key")
    assert manager._proxy_leases == {}


@pytest.mark.asyncio
async def test_launch_resolves_a_random_saved_key(
    tmp_db, monkeypatch, tmp_path: Path
):
    from backend import proxy_accounts, proxy_key_source

    account = proxy_accounts.add_account("kiotproxy", "api-token", "saved")
    keys = [
        {
            "id": "busy",
            "value": "busy-key",
            "value_masked": "busy…-key",
            "status": "ACTIVE",
            "expiration_at": None,
            "deleted": False,
        },
        {
            "id": "free",
            "value": "free-key",
            "value_masked": "free…-key",
            "status": "ACTIVE",
            "expiration_at": None,
            "deleted": False,
        },
    ]
    monkeypatch.setattr(
        proxy_providers, "kiotproxy_keys", AsyncMock(return_value=keys)
    )
    new_proxy = AsyncMock(
        return_value={
            "provider": "kiotproxy",
            "http": "http://1.2.3.4:1",
            "socks5": None,
            "host": "1.2.3.4",
            "real_ip": "1.2.3.4",
            "location": "Hà Nội",
            "isp": None,
            "expires_at": None,
            "ttl": None,
            "ttc": None,
            "next_request_at": None,
            "raw": {},
        }
    )
    monkeypatch.setattr(proxy_providers, "new_for", new_proxy)
    monkeypatch.setattr(proxy_providers, "release_for", AsyncMock())
    monkeypatch.setattr(
        module, "launch_persistent_context_async", AsyncMock(return_value=_fake_context())
    )
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    profile = _profile(tmp_path, "random", provider="kiotproxy")
    profile["proxy_provider_key"] = None
    profile["proxy_account_id"] = account["id"]
    profile["proxy_key_id"] = "random"

    running = await manager.launch(profile)

    selected = new_proxy.await_args.args[1]
    assert selected in {"busy-key", "free-key"}
    assert running.proxy_provider_key == selected
    assert manager._proxy_leases["random"] == proxy_key_source.key_fingerprint(
        "kiotproxy", selected
    )


@pytest.mark.asyncio
async def test_random_launch_skips_a_key_held_by_another_profile(
    tmp_db, monkeypatch, tmp_path: Path
):
    from backend import proxy_accounts

    account = proxy_accounts.add_account("kiotproxy", "api-token", "saved")
    keys = [
        {
            "id": "one",
            "value": "key-one",
            "value_masked": "key…-one",
            "status": "ACTIVE",
            "expiration_at": None,
            "deleted": False,
        }
    ]
    monkeypatch.setattr(
        proxy_providers, "kiotproxy_keys", AsyncMock(return_value=keys)
    )
    monkeypatch.setattr(
        proxy_providers,
        "new_for",
        AsyncMock(
            return_value={
                "provider": "kiotproxy",
                "http": "http://1.2.3.4:1",
                "socks5": None,
                "host": "1.2.3.4",
                "real_ip": "1.2.3.4",
                "location": "Hà Nội",
                "isp": None,
                "expires_at": None,
                "ttl": None,
                "ttc": None,
                "next_request_at": None,
                "raw": {},
            }
        ),
    )
    monkeypatch.setattr(
        module, "launch_persistent_context_async", AsyncMock(return_value=_fake_context())
    )
    manager = BrowserManager(NATIVE_RUNTIME)
    manager._wait_for_cdp = AsyncMock()

    first = _profile(tmp_path, "first")
    first["proxy_provider_key"] = None
    first["proxy_account_id"] = account["id"]
    first["proxy_key_id"] = "one"
    await manager.launch(first)

    second = _profile(tmp_path, "second")
    second["proxy_provider_key"] = None
    second["proxy_account_id"] = account["id"]
    second["proxy_key_id"] = "random"
    with pytest.raises(ValueError, match="No free KiotProxy key"):
        await manager.launch(second)
