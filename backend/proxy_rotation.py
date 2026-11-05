"""Acquire and release provider proxies according to a profile's rotation mode.

The provider is the source of truth for what a key currently holds; this module
keeps a local mirror so it can avoid an unnecessary rotation, respect the
provider's cooldown, and keep a sticky profile on the same exit IP.
"""

from __future__ import annotations

import time
from typing import Any

from . import proxy_key_source, proxy_providers, proxy_state

PER_LAUNCH = "per_launch"
STICKY = "sticky"
MANUAL = "manual"

MODES = (PER_LAUNCH, STICKY, MANUAL)

# Kept as a name because callers/tests already refer to it; the actual signal
# now carries the provider's retry window.
ProxyRotationError = proxy_key_source.ProxyRotationCooldown


def mode_for(profile: dict[str, Any]) -> str:
    mode = (profile.get("proxy_rotation_mode") or PER_LAUNCH).strip().lower()
    return mode if mode in MODES else PER_LAUNCH


def _raw_selection(profile: dict[str, Any]) -> proxy_key_source.ProxyKeySelection:
    provider = (profile.get("proxy_provider") or "none").strip().lower()
    key = (profile.get("proxy_provider_key") or "").strip()
    if not key:
        raise proxy_key_source.NoAvailableProxyKey(
            f"{provider} is selected but no API key is configured"
        )
    return proxy_key_source.ProxyKeySelection(provider=provider, key=key)


def _binding_matches(profile: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Reject a bound key after the profile's provider/account/key selection changes."""
    provider = (profile.get("proxy_provider") or "none").strip().lower()
    if entry.get("provider") != provider:
        return False
    if proxy_key_source.is_random_selection(profile):
        return True
    account_id = (profile.get("proxy_account_id") or "").strip()
    key_id = (profile.get("proxy_key_id") or "").strip()
    if account_id and entry.get("account_id") != account_id:
        return False
    if key_id and entry.get("key_id") != key_id:
        return False
    return True


async def _select(
    profile: dict[str, Any],
    *,
    exclude: set[str],
) -> proxy_key_source.ProxyKeySelection:
    if not proxy_key_source.uses_saved_selection(profile):
        return _raw_selection(profile)

    mode = mode_for(profile)
    bound_fingerprint = proxy_state.get_profile_fingerprint(profile["id"])
    bound = proxy_state.get(bound_fingerprint) if bound_fingerprint else None
    if bound and not _binding_matches(profile, bound):
        proxy_state.unbind_profile(profile["id"])
        bound = None
    if mode in (STICKY, MANUAL) and bound:
        try:
            return await proxy_key_source.select_profile_key(
                profile,
                exclude_fingerprints=exclude,
                account_id=bound.get("account_id"),
                key_id=bound.get("key_id"),
            )
        except proxy_key_source.NoAvailableProxyKey:
            # The bound key disappeared; fall through to a fresh selection.
            proxy_state.unbind_profile(profile["id"])

    return await proxy_key_source.select_profile_key(
        profile, exclude_fingerprints=exclude
    )


async def choose(
    profile: dict[str, Any],
    *,
    exclude: set[str] | None = None,
    force_rotate: bool = False,
) -> proxy_key_source.ProxyKeySelection:
    """Select the key a launch/rotation will use, before any provider call.

    For ``per_launch`` (and an explicit rotation) this prefers a key that is
    outside its provider cooldown, so a pool of several keys keeps rotating
    instead of stalling on the least-recently-used one.
    """
    excluded = exclude or set()
    if not proxy_key_source.uses_saved_selection(profile):
        return _raw_selection(profile)

    mode = mode_for(profile)
    if not force_rotate and mode in (STICKY, MANUAL):
        return await _select(profile, exclude=excluded)

    try:
        return await proxy_key_source.select_profile_key(
            profile,
            exclude_fingerprints=excluded,
            require_rotation_ready=True,
        )
    except proxy_key_source.ProxyRotationCooldown:
        if force_rotate:
            raise
        # No key can rotate right now. If the bound key still has a valid IP,
        # reuse it for this launch rather than refusing to start the browser.
        bound_fingerprint = proxy_state.get_profile_fingerprint(profile["id"])
        bound = proxy_state.get(bound_fingerprint) if bound_fingerprint else None
        if bound and _binding_matches(profile, bound) and proxy_state.is_usable(bound):
            try:
                return await proxy_key_source.select_profile_key(
                    profile,
                    exclude_fingerprints=excluded,
                    account_id=bound.get("account_id"),
                    key_id=bound.get("key_id"),
                )
            except proxy_key_source.NoAvailableProxyKey:
                proxy_state.unbind_profile(profile["id"])
        raise


async def _rotate(
    selection: proxy_key_source.ProxyKeySelection,
    profile: dict[str, Any],
) -> dict[str, Any]:
    info = await proxy_providers.new_for(
        selection.provider,
        selection.key,
        region=profile.get("kiotproxy_region") or "random",
        location_id=int(profile.get("tmproxy_location_id") or 0),
        isp_id=int(profile.get("tmproxy_isp_id") or 0),
    )
    proxy_state.set_current(
        selection.fingerprint,
        selection.provider,
        account_id=selection.account_id,
        key_id=selection.key_id,
        key_masked=selection.key_masked,
        info=info,
    )
    return info


async def acquire(
    profile: dict[str, Any],
    *,
    exclude_fingerprints: set[str] | None = None,
    force_rotate: bool = False,
    selection: proxy_key_source.ProxyKeySelection | None = None,
) -> tuple[
    dict[str, Any], proxy_key_source.ProxyKeySelection, str, bool
]:
    """Return ``(proxy info, selected key, mode, rotated)``.

    ``rotated=False`` means the provider was not called and the key's existing
    (still valid) proxy was reused. ``force_rotate`` is the explicit user action;
    it never silently reuses an IP.
    """
    provider = (profile.get("proxy_provider") or "none").strip().lower()
    if provider not in ("kiotproxy", "tmproxy"):
        raise ValueError("Profile has no proxy provider")

    mode = mode_for(profile)
    exclude = exclude_fingerprints or set()
    if selection is None:
        selection = await choose(
            profile, exclude=exclude, force_rotate=force_rotate
        )
    entry = proxy_state.get(selection.fingerprint)
    remaining = proxy_state.cooldown_remaining(entry)

    if force_rotate:
        if remaining > 0:
            raise proxy_key_source.ProxyRotationCooldown(
                f"The provider allows a new IP in {remaining}s.", remaining
            )
        info = await _rotate(selection, profile)
        proxy_state.bind_profile(profile["id"], selection.fingerprint)
        return info, selection, mode, True

    if mode in (STICKY, MANUAL):
        if proxy_state.is_usable(entry):
            proxy_state.bind_profile(profile["id"], selection.fingerprint)
            proxy_state.touch_used(selection.fingerprint)
            return proxy_state.info(entry), selection, mode, False
        if remaining > 0:
            raise proxy_key_source.ProxyRotationCooldown(
                f"The provider allows a new IP in {remaining}s and the current "
                "proxy has expired.",
                remaining,
            )
    else:
        if remaining > 0:
            if proxy_state.is_usable(entry):
                proxy_state.bind_profile(profile["id"], selection.fingerprint)
                proxy_state.touch_used(selection.fingerprint)
                return proxy_state.info(entry), selection, mode, False
            raise proxy_key_source.ProxyRotationCooldown(
                f"The provider allows a new IP in {remaining}s and the current "
                "proxy has expired.",
                remaining,
            )

    info = await _rotate(selection, profile)
    proxy_state.bind_profile(profile["id"], selection.fingerprint)
    return info, selection, mode, True


async def release(
    profile_id: str,
    provider: str | None,
    key: str | None,
    mode: str | None,
    *,
    clear: bool = False,
) -> None:
    """Free the in-use lease and decide whether the provider keeps the IP.

    sticky/manual profiles keep the same exit IP across launches, so a normal
    stop must not call KiotProxy's detach endpoint. Deleting a profile clears it.
    """
    if not provider or not key:
        proxy_state.unbind_profile(profile_id)
        return
    fingerprint = proxy_key_source.key_fingerprint(provider, key)
    rotate_mode = (mode or PER_LAUNCH).strip().lower()
    keep_ip = not clear and rotate_mode in (STICKY, MANUAL)
    try:
        if not keep_ip:
            await proxy_providers.release_for(provider, key)
    finally:
        if clear or (not keep_ip and provider == "kiotproxy"):
            proxy_state.clear(fingerprint)
            proxy_state.unbind_profile(profile_id)
        elif keep_ip:
            # Sticky/manual: keep both the IP entry and the profile->key binding
            # so the next launch lands on the same exit IP and key.
            proxy_state.mark_released(fingerprint)
        else:
            proxy_state.mark_released(fingerprint)
            proxy_state.unbind_profile(profile_id)


async def state_for(profile: dict[str, Any]) -> dict[str, Any] | None:
    """The locally known current proxy for this profile, without a provider call."""
    fingerprint = proxy_state.get_profile_fingerprint(profile["id"])
    if not fingerprint:
        if proxy_key_source.uses_saved_selection(profile):
            return None
        key = (profile.get("proxy_provider_key") or "").strip()
        if not key:
            return None
        fingerprint = proxy_key_source.key_fingerprint(
            (profile.get("proxy_provider") or "").strip().lower(), key
        )
    entry = proxy_state.get(fingerprint)
    if not entry:
        return None
    return {
        **proxy_state.info(entry),
        "cooldown_remaining": proxy_state.cooldown_remaining(entry),
        "usable": proxy_state.is_usable(entry),
    }


def clear_profile(profile_id: str) -> None:
    proxy_state.unbind_profile(profile_id)


async def release_profile(profile: dict[str, Any]) -> None:
    """Detach the provider proxy and forget local state when a profile is deleted."""
    provider = (profile.get("proxy_provider") or "none").strip().lower()
    if provider not in ("kiotproxy", "tmproxy"):
        proxy_state.unbind_profile(profile["id"])
        return

    # A stopped profile keeps its binding in sticky/manual mode, but a deleted
    # profile must not leave that binding pointing at a key it no longer owns.
    bound_fingerprint = proxy_state.get_profile_fingerprint(profile["id"])
    for other_id, fingerprint in proxy_state.bindings().items():
        if fingerprint == bound_fingerprint and other_id != profile["id"]:
            bound_fingerprint = None
            break

    selection: proxy_key_source.ProxyKeySelection | None = None
    bound = proxy_state.get(bound_fingerprint) if bound_fingerprint else None
    if proxy_key_source.uses_saved_selection(profile):
        # Only the key this profile actually held is safe to detach. A fresh
        # random selection here could detach another profile's key.
        if bound:
            try:
                selection = await proxy_key_source.select_profile_key(
                    profile,
                    account_id=bound.get("account_id"),
                    key_id=bound.get("key_id"),
                )
            except (
                proxy_key_source.NoAvailableProxyKey,
                proxy_providers.ProxyProviderError,
            ):
                selection = None
    else:
        key = (profile.get("proxy_provider_key") or "").strip()
        if key:
            selection = proxy_key_source.ProxyKeySelection(provider=provider, key=key)

    if selection is not None:
        try:
            await proxy_providers.release_for(provider, selection.key)
        except Exception:  # noqa: BLE001 - deleting a profile must not fail on this
            pass
        proxy_state.clear(selection.fingerprint)
    proxy_state.unbind_profile(profile["id"])

