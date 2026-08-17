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
    "extension_paths", "allow_3p_cookies", "notes", "user_data_dir", "created_at",
    "updated_at",
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
    allow_3p_cookies BOOLEAN DEFAULT 0,
    notes TEXT,
    user_data_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
            conn.commit()
            return
        old_columns = {row[1] for row in conn.execute("PRAGMA table_info(profiles)").fetchall()}
        if old_columns != set(_PROFILE_COLUMNS):
            _rebuild_profiles(conn, old_columns)
        else:
            _create_tags_table(conn)
            conn.commit()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value or []
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def create_profile(name: str, fingerprint_seed: int | None = None, **fields: Any) -> dict[str, Any]:
    profile_id = str(uuid.uuid4())
    seed = fingerprint_seed if fingerprint_seed is not None else random.randint(10000, 99999)
    user_data_dir = str(DATA_DIR / "profiles" / profile_id)
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
        "allow_3p_cookies": fields.get("allow_3p_cookies", False), "notes": fields.get("notes"),
        "user_data_dir": user_data_dir, "created_at": now, "updated_at": now,
    }
    with get_db() as conn:
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
        rows = conn.execute("SELECT * FROM profiles ORDER BY created_at DESC").fetchall()
        return [_hydrate_profile(conn, row) for row in rows]


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
