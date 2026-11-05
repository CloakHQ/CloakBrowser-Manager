"""Tests for the KiotProxy and TMProxy API clients."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend import proxy_providers as pp


@pytest.mark.asyncio
async def test_kiotproxy_new_normalises_protocols_and_region(monkeypatch):
    request = AsyncMock(
        return_value={
            "data": {
                "realIpAddress": "171.229.1.2",
                "http": "171.229.1.2:39008",
                "socks5": "171.229.1.2:39009",
                "host": "171.229.1.2",
                "location": "Phú Thọ",
                "expirationAt": 1718030731927,
                "ttl": 1200,
                "ttc": 59,
                "nextRequestAt": 1718029591927,
            },
            "success": True,
            "code": 200,
        }
    )
    monkeypatch.setattr(pp, "_request_json", request)

    info = await pp.kiotproxy_new("key-1", "bac")

    assert request.await_args.kwargs["params"] == {"key": "key-1", "region": "bac"}
    assert info["http"] == "http://171.229.1.2:39008"
    assert info["socks5"] == "socks5://171.229.1.2:39009"
    assert info["real_ip"] == "171.229.1.2"
    assert info["location"] == "Phú Thọ"
    assert info["ttl"] == 1200


@pytest.mark.asyncio
async def test_kiotproxy_rejects_unknown_region_before_calling_api(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(pp, "_request_json", request)

    with pytest.raises(ValueError, match="Invalid KiotProxy region"):
        await pp.kiotproxy_new("key-1", "mien-tay")
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_kiotproxy_business_error_becomes_provider_error(monkeypatch):
    monkeypatch.setattr(
        pp,
        "_request_json",
        AsyncMock(
            return_value={
                "success": False,
                "code": 40400006,
                "message": "Key not found",
                "error": "KEY_NOT_FOUND",
            }
        ),
    )

    with pytest.raises(pp.ProxyProviderError) as raised:
        await pp.kiotproxy_new("missing", "random")
    assert "Key not found" in str(raised.value)
    assert raised.value.code == 40400006


@pytest.mark.asyncio
async def test_tmproxy_new_injects_username_and_password(monkeypatch):
    request = AsyncMock(
        return_value={
            "code": 0,
            "message": "",
            "data": {
                "username": "user1",
                "password": "p@ss word",
                "public_ip": "113.161.1.2",
                "location_name": "TP Ho Chi Minh",
                "isp_name": "Viettel",
                "https": "113.161.1.2:12345",
                "socks5": "113.161.1.2:12346",
                "timeout": 0,
                "next_request": 0,
                "expired_at": "",
            },
        }
    )
    monkeypatch.setattr(pp, "_request_json", request)

    info = await pp.tmproxy_new("api-key", 9, 1)

    assert request.await_args.kwargs["json"] == {
        "api_key": "api-key",
        "id_location": 9,
        "id_isp": 1,
    }
    assert info["http"] == "http://user1:p%40ss%20word@113.161.1.2:12345"
    assert info["socks5"] == "socks5://user1:p%40ss%20word@113.161.1.2:12346"
    assert info["real_ip"] == "113.161.1.2"
    assert info["location"] == "TP Ho Chi Minh"
    assert info["isp"] == "Viettel"


@pytest.mark.asyncio
async def test_tmproxy_error_code_becomes_provider_error(monkeypatch):
    monkeypatch.setattr(
        pp,
        "_request_json",
        AsyncMock(return_value={"code": 1, "message": "Invalid api key", "data": None}),
    )

    with pytest.raises(pp.ProxyProviderError, match="Invalid api key"):
        await pp.tmproxy_new("bad")


@pytest.mark.asyncio
async def test_tmproxy_locations_and_isps_parse_lists(monkeypatch):
    monkeypatch.setattr(
        pp,
        "_request_json",
        AsyncMock(
            side_effect=[
                {
                    "code": 0,
                    "data": {"locations": [{"id_location": 1, "name": "Random"}]},
                },
                {
                    "code": 0,
                    "data": {"list_isp": [{"id_isp": 0, "name": "Random"}]},
                },
            ]
        ),
    )

    assert await pp.tmproxy_locations() == [{"id_location": 1, "name": "Random"}]
    assert await pp.tmproxy_isps() == [{"id_isp": 0, "name": "Random"}]


@pytest.mark.asyncio
async def test_kiotproxy_account_lists_keys_with_api_token_header(monkeypatch):
    request = AsyncMock(
        return_value={
            "success": True,
            "code": 200,
            "data": [
                {
                    "id": "key-uuid",
                    "value": "Kabcdef1234567890",
                    "status": "ACTIVE",
                    "effectiveAt": "2025-01-01T00:00:00Z",
                    "expirationAt": "2025-02-01T00:00:00Z",
                    "packageId": "pkg",
                    "quantityDay": 30,
                }
            ],
        }
    )
    monkeypatch.setattr(pp, "_request_json", request)

    keys = await pp.kiotproxy_keys("account-token")

    assert request.await_args.kwargs["headers"] == {"Api-token": "account-token"}
    assert request.await_args.args[1].endswith("/api/public/keys")
    assert keys[0]["value"] == "Kabcdef1234567890"
    assert keys[0]["value_masked"] == "Kabcd…7890"
    assert pp.public_key_info(keys[0])["key_masked"] == "Kabcd…7890"
    assert "value" not in pp.public_key_info(keys[0])


@pytest.mark.asyncio
async def test_kiotproxy_extend_posts_periods_by_key_value(monkeypatch):
    request = AsyncMock(return_value={"success": True, "data": True})
    monkeypatch.setattr(pp, "_request_json", request)

    assert await pp.kiotproxy_extend_key("token", "K/with space", 2) is True

    assert request.await_args.kwargs["json"] == {"numberOfPeriods": 2}
    assert request.await_args.kwargs["headers"] == {"Api-token": "token"}
    assert "K%2Fwith%20space" in request.await_args.args[1]


@pytest.mark.asyncio
async def test_tmproxy_stats_returns_key_expiry(monkeypatch):
    monkeypatch.setattr(
        pp,
        "_request_json",
        AsyncMock(
            return_value={
                "code": 0,
                "data": {
                    "api_key": "secret",
                    "expired_at": "2026-01-01T00:00:00Z",
                    "plan": "VIP",
                    "price_per_day": 12000,
                    "timeout": 1800,
                    "max_ip_per_day": 100,
                    "ip_used_today": 4,
                },
            }
        ),
    )

    stats = await pp.tmproxy_stats("secret")

    assert stats["expired_at"] == "2026-01-01T00:00:00Z"
    assert stats["plan"] == "VIP"
    assert stats["ip_used_today"] == 4
    assert "api_key" not in stats


@pytest.mark.asyncio
async def test_tmproxy_set_note(monkeypatch):
    request = AsyncMock(return_value={"code": 0, "message": "ok"})
    monkeypatch.setattr(pp, "_request_json", request)

    assert await pp.tmproxy_set_note("secret", "nhóm Shopee") is True
    assert request.await_args.kwargs["json"] == {
        "api_key": "secret",
        "note": "nhóm Shopee",
    }


def test_public_info_strips_credentials_and_raw_payload():
    info = {
        "provider": "tmproxy",
        "http": "http://user:pass@host:1",
        "socks5": "socks5://user:pass@host:2",
        "host": "host",
        "real_ip": "1.2.3.4",
        "location": "Hà Nội",
        "isp": "Viettel",
        "expires_at": None,
        "ttl": 0,
        "ttc": None,
        "next_request_at": 0,
        "raw": {"username": "user", "password": "pass"},
    }

    public = pp.public_info(info)

    assert public is not None
    assert "http" not in public
    assert "socks5" not in public
    assert "raw" not in public
    assert public["real_ip"] == "1.2.3.4"