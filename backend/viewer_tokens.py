"""Opaque, short-lived viewer session tokens for the KasmVNC native client path."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

# Tokens are scoped to one running profile's viewer session: page assets and
# the WebSocket all share one token. The TTL must comfortably outlive typical
# sessions because the KasmVNC client's own (seamless, frame-preserving)
# reconnect re-uses the same WebSocket URL — an expired token forces a full
# iframe reload instead. Revocation on profile stop is the real guard.
VIEWER_TOKEN_TTL = 3600  # seconds


@dataclass
class ViewerSession:
    token: str
    profile_id: str
    ws_port: int
    issued_at: float
    expires_at: float
    # Which launch this token was minted for. ws_port alone cannot answer that:
    # VNCManager.allocate() gap-fills from BASE_DISPLAY upward, so a
    # stop+relaunch of the same profile deterministically returns the identical
    # display and ws_port and the staleness check can never fire. A nonce
    # generated per launch cannot be recycled. Empty means "minted without an
    # epoch", which never matches a live session.
    session_epoch: str = ""


class ViewerTokenStore:
    """In-memory store for viewer tokens.

    Single event loop, plain dict, no awaits inside methods — safe enough
    for async use without locking.
    """

    def __init__(self):
        self._sessions: dict[str, ViewerSession] = {}

    def issue(
        self, profile_id: str, ws_port: int, ttl: int = VIEWER_TOKEN_TTL,
        session_epoch: str = "",
    ) -> str:
        """Issue a fresh token for a profile. Old tokens stay valid until expiry."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        # Sweep here, not only in validate(): once the iframe is re-pointed its
        # old token is never presented again, so the lazy per-token purge never
        # runs for it. Every reconnect mints one, and revoke_profile() only
        # bounds the map when the profile eventually stops — which for an
        # auto-launch profile may be never.
        for stale in [t for t, sess in self._sessions.items() if sess.expires_at <= now]:
            del self._sessions[stale]
        self._sessions[token] = ViewerSession(
            token=token,
            profile_id=profile_id,
            ws_port=ws_port,
            issued_at=now,
            expires_at=now + ttl,
            session_epoch=session_epoch,
        )
        return token

    def validate(self, token: str) -> ViewerSession | None:
        """Return the session for a valid token, else None (unknown or expired)."""
        session = self._sessions.get(token)
        if session is None:
            return None
        if time.time() >= session.expires_at:
            # Lazy purge on expiry
            del self._sessions[token]
            return None
        return session

    def revoke_profile(self, profile_id: str) -> None:
        """Revoke all tokens for a profile (called on stop/delete)."""
        doomed = [t for t, s in self._sessions.items() if s.profile_id == profile_id]
        for t in doomed:
            del self._sessions[t]

    @property
    def active_count(self) -> int:
        """Number of sessions currently held (for tests/diagnostics)."""
        return len(self._sessions)


viewer_tokens = ViewerTokenStore()
