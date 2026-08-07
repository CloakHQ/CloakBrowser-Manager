"""Tests for the viewer token store (KasmVNC native client authorization)."""

from __future__ import annotations

import time

from backend.viewer_tokens import ViewerTokenStore


# ── issue / validate ─────────────────────────────────────────────────────────


def test_issue_returns_unique_fresh_tokens():
    """Each issue() call returns a fresh token; old ones stay valid."""
    store = ViewerTokenStore()
    t1 = store.issue("p1", 6100)
    t2 = store.issue("p1", 6100)
    assert t1 != t2
    assert len(t1) > 20
    assert store.active_count == 2


def test_validate_returns_session():
    store = ViewerTokenStore()
    token = store.issue("p1", 6100)
    session = store.validate(token)
    assert session is not None
    assert session.token == token
    assert session.profile_id == "p1"
    assert session.ws_port == 6100
    assert session.expires_at > session.issued_at


def test_validate_unknown_token():
    store = ViewerTokenStore()
    assert store.validate("no-such-token") is None


# ── expiry ────────────────────────────────────────────────────────────────────


def test_validate_expired_token_immediate_ttl():
    """A zero TTL token is already expired and gets lazy-purged."""
    store = ViewerTokenStore()
    token = store.issue("p1", 6100, ttl=0)
    assert store.validate(token) is None
    assert store.active_count == 0


def test_validate_expired_token_monkeypatched_time(monkeypatch):
    """Token valid at issue time expires once the TTL has passed."""
    store = ViewerTokenStore()
    token = store.issue("p1", 6100, ttl=300)
    assert store.validate(token) is not None

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 301)
    assert store.validate(token) is None
    assert store.active_count == 0  # lazy-purged


# ── revoke_profile ────────────────────────────────────────────────────────────


def test_revoke_profile_removes_only_that_profile():
    store = ViewerTokenStore()
    t1 = store.issue("p1", 6100)
    t2 = store.issue("p1", 6100)
    t3 = store.issue("p2", 6101)

    store.revoke_profile("p1")

    assert store.validate(t1) is None
    assert store.validate(t2) is None
    assert store.validate(t3) is not None
    assert store.active_count == 1


def test_revoke_profile_unknown_is_noop():
    store = ViewerTokenStore()
    store.revoke_profile("nope")  # should not raise
    assert store.active_count == 0


def test_issue_sweeps_abandoned_expired_tokens(monkeypatch):
    """Tokens never presented again must not accumulate for their whole TTL."""
    store = ViewerTokenStore()
    for _ in range(5):
        store.issue("p1", 6100, ttl=300)
    assert store.active_count == 5

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 301)
    fresh = store.issue("p1", 6100, ttl=300)

    # only the new one survives, and it is usable
    assert store.active_count == 1
    assert store.validate(fresh) is not None


def test_issue_keeps_unexpired_tokens_of_other_profiles():
    """The sweep is by expiry, not by profile."""
    store = ViewerTokenStore()
    keep = store.issue("p1", 6100, ttl=300)
    store.issue("p2", 6101, ttl=0)  # already expired
    store.issue("p3", 6102, ttl=300)
    assert store.active_count == 2
    assert store.validate(keep) is not None
