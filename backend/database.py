"""SQLite database operations for browser profiles."""

from __future__ import annotations

import datetime
import json
import random
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .runtime import resolve_runtime

RUNTIME = resolve_runtime()
DATA_DIR = RUNTIME.data_dir
DB_PATH = DATA_DIR / "profiles.db"

_PROFILE_COLUMNS = (
    "id", "name", "fingerprint_seed", "proxy", "timezone", "locale",
    "screen_width", "screen_height", "gpu_family", "humanize", "human_preset",
    "geoip", "clipboard_sync", "auto_launch", "color_scheme", "launch_args",
    "extension_paths", "allow_3p_cookies", "set_google_default", "capture_preview",
    "restore_session", "notes", "user_data_dir", "created_at", "updated_at", "sort_order",
)

_PROFILE_SCHEMA = """
CREATE TABLE profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    fingerprint_seed INTEGER NOT NULL,
    proxy TEXT,
    timezone TEXT,
    locale TEXT,
    screen_width INTEGER DEFAULT 1920,
    screen_height INTEGER DEFAULT 1080,
    gpu_family TEXT NOT NULL DEFAULT 'auto',
    humanize BOOLEAN DEFAULT 0,
    human_preset TEXT DEFAULT 'default',
    geoip BOOLEAN DEFAULT 1,
    clipboard_sync BOOLEAN DEFAULT 1,
    auto_launch BOOLEAN DEFAULT 0,
    color_scheme TEXT,
    launch_args TEXT NOT NULL DEFAULT '[]',
    extension_paths TEXT NOT NULL DEFAULT '[]',
    allow_3p_cookies BOOLEAN DEFAULT 1,
    set_google_default BOOLEAN DEFAULT 1,
    capture_preview BOOLEAN DEFAULT 1,
    restore_session BOOLEAN DEFAULT 1,
    notes TEXT,
    user_data_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
)
"""


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def _create_tags_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profile_tags (
            profile_id TEXT REFERENCES profiles(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            color TEXT,
            PRIMARY KEY (profile_id, tag)
        )
    """)


def _create_artifacts_table(conn: sqlite3.Connection) -> None:
    """Files attached to a profile. Rows are metadata; the bytes live under DATA_DIR/artifacts."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT 'upload',
            state TEXT NOT NULL DEFAULT 'ready',
            content_type TEXT,
            created_at TEXT NOT NULL,
            picker_name TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_profile ON artifacts(profile_id)")


def _rebuild_profiles(conn: sqlite3.Connection, old_columns: set[str]) -> None:
    """Rebuild the table in one transaction, retaining supported data and tags.

    SQLite cannot drop columns on older supported versions.  Copying tags through a
    temporary table avoids a foreign-key reference to the old parent table while it
    is replaced.
    """
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        has_tags = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='profile_tags'"
        ).fetchone() is not None
        if has_tags:
            conn.execute("CREATE TEMP TABLE _profile_tags_backup AS SELECT * FROM profile_tags")
        conn.execute("DROP TABLE IF EXISTS profile_tags")
        conn.execute(_PROFILE_SCHEMA.replace("CREATE TABLE profiles", "CREATE TABLE profiles_new"))
        copied = [column for column in _PROFILE_COLUMNS if column in old_columns]
        if copied:
            cols = ", ".join(copied)
            conn.execute(f"INSERT INTO profiles_new ({cols}) SELECT {cols} FROM profiles")

        # Preserve the user's old broad GPU preference while discarding the
        # brittle free-text vendor/renderer fields. Explicit gpu_family values
        # from newer schemas always win.
        if "gpu_family" not in old_columns:
            legacy_gpu_parts = [
                column for column in ("gpu_vendor", "gpu_renderer")
                if column in old_columns
            ]
            if legacy_gpu_parts:
                expression = " || ' ' || ".join(
                    f"lower(coalesce({column}, ''))" for column in legacy_gpu_parts
                )
                conn.execute(
                    f"""
                    UPDATE profiles_new
                    SET gpu_family = CASE
                        WHEN id IN (SELECT id FROM profiles WHERE {expression} LIKE '%nvidia%') THEN 'nvidia'
                        WHEN id IN (SELECT id FROM profiles WHERE {expression} LIKE '%intel%') THEN 'intel'
                        ELSE 'auto'
                    END
                    """
                )
        conn.execute("DROP TABLE profiles")
        conn.execute("ALTER TABLE profiles_new RENAME TO profiles")
        _create_tags_table(conn)
        if has_tags:
            conn.execute("""
                INSERT OR IGNORE INTO profile_tags (profile_id, tag, color)
                SELECT profile_id, tag, color FROM _profile_tags_backup
            """)
        conn.execute("DROP TABLE IF EXISTS _profile_tags_backup")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='profiles'").fetchone()
        if not exists:
            conn.execute(_PROFILE_SCHEMA)
            _create_tags_table(conn)
            _create_artifacts_table(conn)
            conn.commit()
            return
        old_columns = {row[1] for row in conn.execute("PRAGMA table_info(profiles)").fetchall()}
        if old_columns != set(_PROFILE_COLUMNS):
            _rebuild_profiles(conn, old_columns)
        else:
            _create_tags_table(conn)
        _create_artifacts_table(conn)
        conn.commit()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value or []
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def new_profile_id() -> str:
    return str(uuid.uuid4())


def user_data_dir_for(profile_id: str) -> str:
    """Where a profile keeps its Chrome user data. The one place this layout lives."""
    return str(DATA_DIR / "profiles" / profile_id)


def create_profile(
    name: str, fingerprint_seed: int | None = None, *, profile_id: str | None = None, **fields: Any
) -> dict[str, Any]:
    """Insert a new profile. ``profile_id`` lets a caller mint the id (and so the
    user_data_dir) ahead of the row — used to fill a clone's directory BEFORE it
    becomes visible, so a half-built profile is never listed."""
    profile_id = profile_id or new_profile_id()
    seed = fingerprint_seed if fingerprint_seed is not None else random.randint(10000, 99999)
    user_data_dir = user_data_dir_for(profile_id)
    now = _now()
    tags = fields.pop("tags", None) or []
    values = {
        "id": profile_id, "name": name, "fingerprint_seed": seed,
        "proxy": fields.get("proxy"), "timezone": fields.get("timezone"), "locale": fields.get("locale"),
        "screen_width": fields.get("screen_width", 1920), "screen_height": fields.get("screen_height", 1080),
        "gpu_family": fields.get("gpu_family", "auto"), "humanize": fields.get("humanize", False),
        "human_preset": fields.get("human_preset", "default"), "geoip": fields.get("geoip", True),
        "clipboard_sync": fields.get("clipboard_sync", True), "auto_launch": fields.get("auto_launch", False),
        "color_scheme": fields.get("color_scheme"), "launch_args": json.dumps(fields.get("launch_args") or []),
        "extension_paths": json.dumps(fields.get("extension_paths") or []),
        "allow_3p_cookies": fields.get("allow_3p_cookies", True),
        "set_google_default": fields.get("set_google_default", True),
        "capture_preview": fields.get("capture_preview", True),
        "restore_session": fields.get("restore_session", True), "notes": fields.get("notes"),
        "user_data_dir": user_data_dir, "created_at": now, "updated_at": now,
    }
    with get_db() as conn:
        # New profiles land on top of the manual order (smallest sort_order).
        min_order = conn.execute("SELECT MIN(sort_order) FROM profiles").fetchone()[0]
        values["sort_order"] = (min_order - 1) if min_order is not None else 0
        cols = ", ".join(_PROFILE_COLUMNS)
        placeholders = ", ".join("?" for _ in _PROFILE_COLUMNS)
        conn.execute(
            f"INSERT INTO profiles ({cols}) VALUES ({placeholders})",
            [values[column] for column in _PROFILE_COLUMNS],
        )
        for tag in tags:
            conn.execute(
                "INSERT INTO profile_tags (profile_id, tag, color) VALUES (?, ?, ?)",
                (profile_id, tag["tag"], tag.get("color")),
            )
        conn.commit()

    profile = get_profile(profile_id)
    if profile is None:
        raise RuntimeError(f"Created profile {profile_id} could not be reloaded")
    return profile


def _hydrate_profile(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    profile = dict(row)
    profile["launch_args"] = _json_list(profile.get("launch_args"))
    profile["extension_paths"] = _json_list(profile.get("extension_paths"))
    tags = conn.execute(
        "SELECT tag, color FROM profile_tags WHERE profile_id = ?",
        (profile["id"],),
    ).fetchall()
    profile["tags"] = [dict(tag) for tag in tags]
    return profile


def get_profile(profile_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return _hydrate_profile(conn, row) if row else None


def list_profiles() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM profiles ORDER BY sort_order ASC, created_at DESC"
        ).fetchall()
        return [_hydrate_profile(conn, row) for row in rows]


def reorder_profiles(ordered_ids: list[str]) -> None:
    """Persist a manual profile order: sort_order = position in ordered_ids."""
    with get_db() as conn:
        conn.executemany(
            "UPDATE profiles SET sort_order = ? WHERE id = ?",
            [(index, profile_id) for index, profile_id in enumerate(ordered_ids)],
        )
        conn.commit()


def update_profile(profile_id: str, **fields: Any) -> dict[str, Any] | None:
    if not get_profile(profile_id):
        return None
    tags = fields.pop("tags", None)
    for key in ("launch_args", "extension_paths"):
        if key in fields:
            fields[key] = json.dumps(fields[key] or [])
    update_cols = []
    update_vals = []
    for col in _PROFILE_COLUMNS:
        if col not in {"id", "user_data_dir", "created_at", "updated_at"} and col in fields:
            update_cols.append(f"{col} = ?")
            update_vals.append(fields[col])
    with get_db() as conn:
        if update_cols:
            conn.execute(
                f"UPDATE profiles SET {', '.join(update_cols)}, updated_at = ? WHERE id = ?",
                [*update_vals, _now(), profile_id],
            )
        if tags is not None:
            conn.execute("DELETE FROM profile_tags WHERE profile_id = ?", (profile_id,))
            for tag in tags:
                conn.execute(
                    "INSERT INTO profile_tags (profile_id, tag, color) VALUES (?, ?, ?)",
                    (profile_id, tag["tag"], tag.get("color")),
                )
        conn.commit()
    return get_profile(profile_id)


def delete_profile(profile_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return cursor.rowcount > 0


def reset_profile(profile_id: str) -> dict[str, Any] | None:
    """Re-roll fingerprint_seed and bump updated_at. Returns the updated profile or None.

    A fresh seed is the point of a reset: the profile keeps its config (name,
    proxy, locale, tags) but takes on a new identity. Seed range mirrors
    create_profile.
    """
    if not get_profile(profile_id):
        return None
    with get_db() as conn:
        conn.execute(
            "UPDATE profiles SET fingerprint_seed = ?, updated_at = ? WHERE id = ?",
            (random.randint(10000, 99999), _now(), profile_id),
        )
        conn.commit()
    return get_profile(profile_id)


def duplicate_profile(profile_id: str, *, new_id: str | None = None) -> dict[str, Any] | None:
    """Clone a profile's config into a brand-new profile. Returns it or None.

    Config-only clone: every setting, the tags, notes, and the SAME
    fingerprint_seed are carried over, but no on-disk browser state is copied.
    create_profile mints a fresh uuid, user_data_dir and sort_order (or takes
    ``new_id``), so the clone launches with an empty profile dir built fresh on
    first use — unless the caller filled that dir beforehand.
    """
    src = get_profile(profile_id)
    if src is None:
        return None
    fields = {
        key: value
        for key, value in src.items()
        if key not in {"id", "name", "fingerprint_seed", "tags",
                       "user_data_dir", "created_at", "updated_at", "sort_order"}
    }
    return create_profile(
        name=f"{src['name']} (copy)",
        fingerprint_seed=src["fingerprint_seed"],
        profile_id=new_id,
        tags=src.get("tags"),
        **fields,
    )


# ── Artifacts ────────────────────────────────────────────────────────────────


_ARTIFACT_COLUMNS = (
    "id", "profile_id", "name", "size", "kind", "state", "content_type", "created_at", "picker_name",
)


def new_artifact_id() -> str:
    return str(uuid.uuid4())


def create_artifact(
    artifact_id: str,
    profile_id: str,
    name: str,
    size: int,
    *,
    kind: str = "upload",
    state: str = "ready",
    content_type: str | None = None,
    picker_name: str | None = None,
) -> dict[str, Any]:
    """Record an artifact. An upload publishes its bytes first; a download starts at zero
    bytes in the ``pending`` state and is updated when the transfer finishes."""
    values = {
        "id": artifact_id, "profile_id": profile_id, "name": name, "size": size,
        "kind": kind, "state": state, "content_type": content_type, "created_at": _now(),
        "picker_name": picker_name or name,
    }
    with get_db() as conn:
        cols = ", ".join(_ARTIFACT_COLUMNS)
        placeholders = ", ".join("?" for _ in _ARTIFACT_COLUMNS)
        conn.execute(
            f"INSERT INTO artifacts ({cols}) VALUES ({placeholders})",
            [values[c] for c in _ARTIFACT_COLUMNS],
        )
        conn.commit()
    return values


def get_artifact(profile_id: str, artifact_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE id = ? AND profile_id = ?", (artifact_id, profile_id)
        ).fetchone()
    return dict(row) if row else None


def list_artifacts(profile_id: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE profile_id = ? ORDER BY created_at DESC, id",
            (profile_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_artifact(profile_id: str, artifact_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {
        k: v for k, v in fields.items()
        if k in ("name", "size", "state", "content_type", "picker_name")
    }
    if not allowed:
        return get_artifact(profile_id, artifact_id)
    assignments = ", ".join(f"{column} = ?" for column in allowed)
    with get_db() as conn:
        conn.execute(
            f"UPDATE artifacts SET {assignments} WHERE id = ? AND profile_id = ?",
            [*allowed.values(), artifact_id, profile_id],
        )
        conn.commit()
    return get_artifact(profile_id, artifact_id)


def profile_names() -> list[tuple[str, str]]:
    """``(id, name)`` for every profile, without hydrating tags.

    ``list_profiles`` runs a tag query per profile; the file-chooser bookmarks only need
    these two columns and are rebuilt on every file mutation.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name FROM profiles ORDER BY sort_order, created_at"
        ).fetchall()
    return [(row["id"], row["name"]) for row in rows]


def profile_ids_with_artifacts() -> list[str]:
    """Only these profiles have a file-chooser view worth reconciling at startup."""
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT profile_id FROM artifacts").fetchall()
    return [row["profile_id"] for row in rows]


def delete_artifact(profile_id: str, artifact_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM artifacts WHERE id = ? AND profile_id = ?", (artifact_id, profile_id)
        )
        conn.commit()
    return cur.rowcount > 0
