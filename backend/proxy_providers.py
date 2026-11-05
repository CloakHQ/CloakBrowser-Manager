"""KiotProxy and TMProxy API clients.

Both providers hand out one rotating proxy per API key. A profile can name a
provider and a key; the Manager asks the provider for a fresh proxy on launch and
(the KiotProxy case) releases it again when the browser stops.

Everything here is provider-specific, so this module's job is to hide the
differences behind one normalised dictionary::

    {
        "provider": "kiotproxy" | "tmproxy",
        "http": "http://user:pass@host:port" | "http://host:port" | None,
        "socks5": "socks5://..." | None,
        "host": "1.2.3.4",
        "real_ip": "1.2.3.4",
        "location": "Phú Thọ",
        "isp": "Viettel" | None,
        "expires_at": 1718030731927 | "..." | None,
        "ttl": 1200 | 0 | None,
        "ttc": 59 | None,
        "next_request_at": 1718029591927 | None,
        "raw": {...},
    }

Credentials are deliberately kept in ``http``/``socks5`` because that is what
the browser needs. Callers that expose a proxy to the UI must sanitise it first
(see :func:`public_info`).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

KIOTPROXY_API_BASE = "https://api.kiotproxy.com/api/v1"
TMPROXY_API_BASE = "https://tmproxy.com/api/proxy"

# KiotProxy regions, exactly as the API spells them.
KIOTPROXY_REGIONS = ("bac", "trung", "nam", "random")

REQUEST_TIMEOUT = 20.0


class ProxyProviderError(RuntimeError):
    """A provider request failed, or the provider answered with an error code.

    ``status_code`` is the HTTP status the provider returned when there was one.
    Routes map this to 502 (upstream failure) and include the message as detail.
    """

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        code: int | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.status_code = status_code


def _provider_error(provider: str, payload: dict, *, status_code: int | None = None) -> ProxyProviderError:
    message = (
        payload.get("message")
        or payload.get("msg")
        or payload.get("error")
        or f"{provider} returned an error"
    )
    code = payload.get("code")
    return ProxyProviderError(
        provider,
        f"{provider}: {message}",
        code=code if isinstance(code, int) else None,
        status_code=status_code,
    )


async def _request_json(
    method: str,
    url: str,
    *,
    provider: str,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call a provider endpoint and return its decoded JSON object.

    Network errors, non-2xx responses and non-object bodies all become a
    :class:`ProxyProviderError`, so callers have one failure path.
    """
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        ) as client:
            response = await client.request(method, url, params=params, json=json)
    except httpx.HTTPError as exc:
        raise ProxyProviderError(
            provider, f"{provider}: could not reach the API ({exc})"
        ) from exc

    if response.status_code >= 400:
        payload: dict[str, Any] = {}
        try:
            body = response.json()
            if isinstance(body, dict):
                payload = body
        except ValueError:
            pass
        raise _provider_error(provider, payload, status_code=response.status_code)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProxyProviderError(
            provider, f"{provider}: API returned a non-JSON response"
        ) from exc
    if not isinstance(payload, dict):
        raise ProxyProviderError(provider, f"{provider}: API returned an unexpected response")
    return payload


def _with_auth(proxy: str | None, username: str | None, password: str | None) -> str | None:
    """Inject ``user:pass`` into a ``scheme://host:port`` proxy URL."""
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    if not username or not password:
        return proxy
    parts = urlsplit(proxy)
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _host_from_proxy(proxy: str | None) -> str | None:
    if not proxy:
        return None
    try:
        return urlsplit(proxy if "://" in proxy else f"http://{proxy}").hostname
    except ValueError:
        return None


def _prefixed(scheme: str, value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    return value if "://" in value else f"{scheme}://{value}"


def _kiotproxy_info(data: dict[str, Any]) -> dict[str, Any]:
    http = _prefixed("http", data.get("http"))
    socks5 = _prefixed("socks5", data.get("socks5"))
    return {
        "provider": "kiotproxy",
        "http": http,
        "socks5": socks5,
        "host": data.get("host") or _host_from_proxy(http),
        "real_ip": data.get("realIpAddress"),
        "location": data.get("location"),
        "isp": None,
        "expires_at": data.get("expirationAt"),
        "ttl": data.get("ttl"),
        "ttc": data.get("ttc"),
        "next_request_at": data.get("nextRequestAt"),
        "raw": data,
    }


def _tmproxy_info(data: dict[str, Any]) -> dict[str, Any]:
    username = data.get("username")
    password = data.get("password")
    https = _with_auth(_prefixed("http", data.get("https")), username, password)
    socks5 = _with_auth(_prefixed("socks5", data.get("socks5")), username, password)
    return {
        "provider": "tmproxy",
        "http": https,
        "socks5": socks5,
        "host": _host_from_proxy(https) or data.get("public_ip"),
        "real_ip": data.get("public_ip"),
        "location": data.get("location_name"),
        "isp": data.get("isp_name"),
        "expires_at": data.get("expired_at"),
        "ttl": data.get("timeout"),
        "ttc": None,
        "next_request_at": data.get("next_request"),
        "raw": data,
    }


def public_info(info: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip credentials/raw payload before handing a proxy to the UI."""
    if not info:
        return None
    return {
        "provider": info.get("provider"),
        "host": info.get("host"),
        "real_ip": info.get("real_ip"),
        "location": info.get("location"),
        "isp": info.get("isp"),
        "expires_at": info.get("expires_at"),
        "ttl": info.get("ttl"),
        "ttc": info.get("ttc"),
        "next_request_at": info.get("next_request_at"),
    }


async def kiotproxy_new(key: str, region: str = "random") -> dict[str, Any]:
    """GET /api/v1/proxies/new — allocate or rotate the proxy for ``key``."""
    region = (region or "random").strip().lower()
    if region not in KIOTPROXY_REGIONS:
        raise ValueError(
            f"Invalid KiotProxy region '{region}'. Must be one of: {', '.join(KIOTPROXY_REGIONS)}"
        )
    payload = await _request_json(
        "GET",
        f"{KIOTPROXY_API_BASE}/proxies/new",
        provider="kiotproxy",
        params={"key": key, "region": region},
    )
    if payload.get("success") is not True:
        raise _provider_error("kiotproxy", payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _provider_error("kiotproxy", payload)
    return _kiotproxy_info(data)


async def kiotproxy_current(key: str) -> dict[str, Any]:
    """GET /api/v1/proxies/current — the proxy currently assigned to ``key``."""
    payload = await _request_json(
        "GET",
        f"{KIOTPROXY_API_BASE}/proxies/current",
        provider="kiotproxy",
        params={"key": key},
    )
    if payload.get("success") is not True:
        raise _provider_error("kiotproxy", payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _provider_error("kiotproxy", payload)
    return _kiotproxy_info(data)


async def kiotproxy_out(key: str) -> bool:
    """GET /api/v1/proxies/out — detach the proxy from ``key``."""
    payload = await _request_json(
        "GET",
        f"{KIOTPROXY_API_BASE}/proxies/out",
        provider="kiotproxy",
        params={"key": key},
    )
    if payload.get("success") is not True:
        raise _provider_error("kiotproxy", payload)
    return True


async def tmproxy_new(
    api_key: str, location_id: int = 0, isp_id: int = 0
) -> dict[str, Any]:
    """POST /api/proxy/get-new-proxy — allocate or rotate the proxy."""
    payload = await _request_json(
        "POST",
        f"{TMPROXY_API_BASE}/get-new-proxy",
        provider="tmproxy",
        json={
            "api_key": api_key,
            "id_location": int(location_id or 0),
            "id_isp": int(isp_id or 0),
        },
    )
    if payload.get("code") != 0:
        raise _provider_error("tmproxy", payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _provider_error("tmproxy", payload)
    return _tmproxy_info(data)


async def tmproxy_current(api_key: str) -> dict[str, Any]:
    """POST /api/proxy/get-current-proxy — the proxy assigned to ``api_key``."""
    payload = await _request_json(
        "POST",
        f"{TMPROXY_API_BASE}/get-current-proxy",
        provider="tmproxy",
        json={"api_key": api_key},
    )
    if payload.get("code") != 0:
        raise _provider_error("tmproxy", payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _provider_error("tmproxy", payload)
    return _tmproxy_info(data)


async def tmproxy_locations() -> list[dict[str, Any]]:
    """POST /api/proxy/location — selectable TMProxy locations."""
    payload = await _request_json(
        "POST", f"{TMPROXY_API_BASE}/location", provider="tmproxy", json={}
    )
    if payload.get("code") != 0:
        raise _provider_error("tmproxy", payload)
    data = payload.get("data") or {}
    locations = data.get("locations") if isinstance(data, dict) else None
    if not isinstance(locations, list):
        return []
    return [
        {"id_location": item.get("id_location"), "name": item.get("name")}
        for item in locations
        if isinstance(item, dict)
    ]


async def tmproxy_isps() -> list[dict[str, Any]]:
    """POST /api/proxy/isp — selectable TMProxy ISPs."""
    payload = await _request_json(
        "POST", f"{TMPROXY_API_BASE}/isp", provider="tmproxy", json={}
    )
    if payload.get("code") != 0:
        raise _provider_error("tmproxy", payload)
    data = payload.get("data") or {}
    isps = data.get("list_isp") if isinstance(data, dict) else None
    if not isinstance(isps, list):
        return []
    return [
        {"id_isp": item.get("id_isp"), "name": item.get("name")}
        for item in isps
        if isinstance(item, dict)
    ]


async def current_for(provider: str, key: str) -> dict[str, Any]:
    """Dispatch ``get-current`` to the right provider."""
    if provider == "kiotproxy":
        return await kiotproxy_current(key)
    if provider == "tmproxy":
        return await tmproxy_current(key)
    raise ValueError(f"Unknown proxy provider: {provider}")


async def new_for(
    provider: str,
    key: str,
    *,
    region: str = "random",
    location_id: int = 0,
    isp_id: int = 0,
) -> dict[str, Any]:
    """Dispatch ``get-new`` to the right provider."""
    if provider == "kiotproxy":
        return await kiotproxy_new(key, region)
    if provider == "tmproxy":
        return await tmproxy_new(key, location_id, isp_id)
    raise ValueError(f"Unknown proxy provider: {provider}")


async def release_for(provider: str, key: str) -> None:
    """Release a provider key, when the provider supports it.

    KiotProxy has an explicit detach endpoint. TMProxy has none: its key simply
    keeps its current proxy, and the next ``get-new-proxy`` rotates it.
    """
    if provider == "kiotproxy":
        await kiotproxy_out(key)
    elif provider == "tmproxy":
        return
    else:
        raise ValueError(f"Unknown proxy provider: {provider}")

