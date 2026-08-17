"""Profile-scoped viewer grants for the embedded Cloak Manager viewer."""

from __future__ import annotations

import datetime
import hashlib
import secrets
import uuid
from typing import Any

from . import database as db


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_grant(profile_id: str, session_id: str, expires_in: int) -> dict[str, Any]:
    now = _now()
    expires_at = now + datetime.timedelta(seconds=expires_in)
    grant_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    with db.get_db() as conn:
        revoked_rows = conn.execute(
            """SELECT id FROM viewer_grants
               WHERE session_id = ? AND revoked_at IS NULL""",
            (session_id,),
        ).fetchall()
        conn.execute(
            """UPDATE viewer_grants
               SET revoked_at = ?
               WHERE session_id = ? AND revoked_at IS NULL""",
            (now.isoformat(), session_id),
        )
        conn.execute(
            """INSERT INTO viewer_grants (
                   id, profile_id, session_id, token_hash,
                   expires_at, revoked_at, created_at
               ) VALUES (?, ?, ?, ?, ?, NULL, ?)""",
            (
                grant_id,
                profile_id,
                session_id,
                token_hash,
                expires_at.isoformat(),
                now.isoformat(),
            ),
        )
        conn.commit()
    return {
        "grant_id": grant_id,
        "profile_id": profile_id,
        "session_id": session_id,
        "token": token,
        "expires_at": expires_at,
        "_revoked_grant_ids": [row["id"] for row in revoked_rows],
    }


def validate_grant(profile_id: str, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    token_hash = _hash_token(token)
    with db.get_db() as conn:
        row = conn.execute(
            """SELECT id, profile_id, session_id, expires_at
               FROM viewer_grants
               WHERE token_hash = ? AND profile_id = ? AND revoked_at IS NULL""",
            (token_hash, profile_id),
        ).fetchone()
    if not row:
        return None
    expires_at = datetime.datetime.fromisoformat(row["expires_at"])
    if expires_at <= _now():
        return None
    return {**dict(row), "expires_at": expires_at}


def revoke_grant(profile_id: str, grant_id: str) -> bool:
    with db.get_db() as conn:
        cursor = conn.execute(
            """UPDATE viewer_grants
               SET revoked_at = ?
               WHERE id = ? AND profile_id = ? AND revoked_at IS NULL""",
            (_now().isoformat(), grant_id, profile_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def revoke_session(session_id: str) -> int:
    with db.get_db() as conn:
        cursor = conn.execute(
            """UPDATE viewer_grants
               SET revoked_at = ?
               WHERE session_id = ? AND revoked_at IS NULL""",
            (_now().isoformat(), session_id),
        )
        conn.commit()
        return cursor.rowcount
