"""Tests for the local proxy-account credential store."""

from __future__ import annotations

from backend import proxy_accounts


def test_add_list_and_mask(tmp_db):
    account = proxy_accounts.add_account(
        "kiotproxy", "super-secret-api-token", "tài khoản chính"
    )

    assert account["provider"] == "kiotproxy"
    assert account["secret"] == "super-secret-api-token"
    assert account["label"] == "tài khoản chính"

    listed = proxy_accounts.list_accounts()
    assert len(listed) == 1
    public = proxy_accounts.public_account(listed[0])
    assert "secret" not in public
    assert public["secret_masked"] == "super…oken"


def test_same_credential_is_not_duplicated(tmp_db):
    first = proxy_accounts.add_account("tmproxy", "same-key", "old")
    second = proxy_accounts.add_account("tmproxy", "same-key", "new")

    assert first["id"] == second["id"]
    assert second["label"] == "new"
    assert len(proxy_accounts.list_accounts()) == 1


def test_different_providers_with_same_secret_are_separate(tmp_db):
    kiot = proxy_accounts.add_account("kiotproxy", "shared")
    tm = proxy_accounts.add_account("tmproxy", "shared")

    assert kiot["id"] != tm["id"]
    assert len(proxy_accounts.list_accounts()) == 2


def test_delete_and_touch(tmp_db):
    account = proxy_accounts.add_account("kiotproxy", "token")
    assert account["checked_at"] is None

    proxy_accounts.touch_account(account["id"])
    assert proxy_accounts.get_account(account["id"])["checked_at"] is not None

    assert proxy_accounts.delete_account(account["id"]) is True
    assert proxy_accounts.get_account(account["id"]) is None
    assert proxy_accounts.delete_account(account["id"]) is False


def test_rejects_unknown_provider(tmp_db):
    try:
        proxy_accounts.add_account("other", "token")
    except ValueError as exc:
        assert "Unknown proxy provider" in str(exc)
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("expected ValueError")
