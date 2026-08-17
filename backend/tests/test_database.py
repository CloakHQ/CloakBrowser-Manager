"""Tests for SQLite CRUD operations."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from backend import database as db


# ── init_db ──────────────────────────────────────────────────────────────────


def test_init_db_creates_tables(tmp_db: Path):
    with db.get_db() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r["name"] for r in tables}
    assert "profiles" in names
    assert "profile_tags" in names


def test_init_db_idempotent(tmp_db: Path):
    # Second call should not crash
    db.init_db()
    with db.get_db() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    assert len(tables) >= 2


def test_init_db_preserves_existing_profile_directory(tmp_db: Path):
    profile = db.create_profile("Existing")
    original_path = profile["user_data_dir"]
    db.init_db()
    assert db.get_profile(profile["id"])["user_data_dir"] == original_path


def test_init_db_rebuilds_old_schema_preserving_profile_and_tags(tmp_db: Path):
    with db.get_db() as conn:
        conn.executescript("""
            DROP TABLE profile_tags; DROP TABLE profiles;
            CREATE TABLE profiles (id TEXT PRIMARY KEY, name TEXT NOT NULL, fingerprint_seed INTEGER NOT NULL, proxy TEXT, platform TEXT, user_agent TEXT, gpu_vendor TEXT, gpu_renderer TEXT, hardware_concurrency INTEGER, headless BOOLEAN, user_data_dir TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE profile_tags (profile_id TEXT REFERENCES profiles(id) ON DELETE CASCADE, tag TEXT NOT NULL, color TEXT, PRIMARY KEY(profile_id, tag));
            INSERT INTO profiles VALUES ('old', 'Retained', 42, 'http://proxy:1', 'macos', 'Legacy UA', 'NVIDIA', 'Legacy GPU', 16, 1, '/data/old', 'created', 'updated');
            INSERT INTO profile_tags VALUES ('old', 'keep', '#abc');
        """)
        conn.commit()
    db.init_db()
    profile = db.get_profile("old")
    assert profile["name"] == "Retained"
    assert profile["proxy"] == "http://proxy:1"
    assert profile["tags"] == [{"tag": "keep", "color": "#abc"}]
    assert profile["gpu_family"] == "nvidia"
    assert profile["geoip"] == 1
    with db.get_db() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(profiles)")}
    assert {
        "platform", "user_agent", "gpu_vendor", "gpu_renderer",
        "hardware_concurrency", "headless",
    }.isdisjoint(columns)
    assert {"launch_args", "extension_paths", "allow_3p_cookies"}.issubset(columns)


# ── create_profile ───────────────────────────────────────────────────────────


def test_create_profile_minimal(tmp_db: Path):
    p = db.create_profile("Test")
    assert p["name"] == "Test"
    assert isinstance(p["id"], str) and len(p["id"]) == 36  # UUID
    assert 10000 <= p["fingerprint_seed"] <= 99999  # random default
    assert p["user_data_dir"].startswith(str(tmp_db))
    assert p["gpu_family"] == "auto"
    assert p["created_at"] is not None
    assert p["updated_at"] is not None


def test_create_profile_with_seed(tmp_db: Path):
    p = db.create_profile("Seeded", fingerprint_seed=42)
    assert p["fingerprint_seed"] == 42


def test_create_profile_all_fields(tmp_db: Path):
    p = db.create_profile(
        "Full",
        fingerprint_seed=99999,
        proxy="http://host:8080",
        timezone="America/New_York",
        locale="en-US",
        screen_width=2560,
        screen_height=1440,
        gpu_family="nvidia",
        humanize=True,
        human_preset="careful",
        geoip=True,
        color_scheme="dark",
        notes="test note",
    )
    assert p["proxy"] == "http://host:8080"
    assert p["gpu_family"] == "nvidia"
    assert p["humanize"] == 1  # SQLite stores bool as int
    assert p["human_preset"] == "careful"
    assert p["color_scheme"] == "dark"


def test_create_profile_with_tags(tmp_db: Path):
    p = db.create_profile(
        "Tagged",
        tags=[
            {"tag": "work", "color": "#ff0000"},
            {"tag": "dev", "color": "#00ff00"},
        ],
    )
    assert len(p["tags"]) == 2
    tag_names = {t["tag"] for t in p["tags"]}
    assert tag_names == {"work", "dev"}


def test_create_profile_defaults(tmp_db: Path):
    p = db.create_profile("Defaults")
    assert p["gpu_family"] == "auto"
    assert p["screen_width"] == 1920
    assert p["screen_height"] == 1080
    assert p["humanize"] == 0
    assert p["geoip"] == 1
    assert p["extension_paths"] == []
    assert p["allow_3p_cookies"] == 0
    assert p["human_preset"] == "default"
    assert p["launch_args"] == []


def test_create_profile_with_launch_args(tmp_db: Path):
    p = db.create_profile("WithArgs", launch_args=["--load-extension=/tmp/ext", "--disable-features=Foo"])
    assert p["launch_args"] == ["--load-extension=/tmp/ext", "--disable-features=Foo"]


def test_get_profile_launch_args_roundtrip(tmp_db: Path):
    p = db.create_profile("Args", launch_args=["--flag1", "--flag2"])
    fetched = db.get_profile(p["id"])
    assert fetched["launch_args"] == ["--flag1", "--flag2"]


def test_profile_extension_paths_and_cookie_setting_roundtrip(tmp_db: Path):
    profile = db.create_profile(
        "Compatibility",
        extension_paths=["/tmp/one", "/tmp/two"],
        allow_3p_cookies=True,
    )
    assert profile["extension_paths"] == ["/tmp/one", "/tmp/two"]
    assert profile["allow_3p_cookies"] == 1

    updated = db.update_profile(profile["id"], extension_paths=["/tmp/three"])
    assert updated["extension_paths"] == ["/tmp/three"]


def test_update_profile_launch_args(tmp_db: Path):
    p = db.create_profile("Args")
    assert p["launch_args"] == []
    updated = db.update_profile(p["id"], launch_args=["--new-flag"])
    assert updated["launch_args"] == ["--new-flag"]


def test_update_profile_launch_args_none_becomes_empty(tmp_db: Path):
    p = db.create_profile("Args", launch_args=["--flag"])
    updated = db.update_profile(p["id"], launch_args=None)
    assert updated["launch_args"] == []


def test_list_profiles_includes_launch_args(tmp_db: Path):
    db.create_profile("A", launch_args=["--arg1"])
    db.create_profile("B")
    profiles = db.list_profiles()
    args_by_name = {p["name"]: p["launch_args"] for p in profiles}
    assert args_by_name["A"] == ["--arg1"]
    assert args_by_name["B"] == []


# ── get_profile ──────────────────────────────────────────────────────────────


def test_get_profile_exists(sample_profile: dict):
    p = db.get_profile(sample_profile["id"])
    assert p is not None
    assert p["name"] == "Test Profile"
    assert p["fingerprint_seed"] == 12345


def test_get_profile_not_found(tmp_db: Path):
    assert db.get_profile("nonexistent") is None


def test_get_profile_includes_tags(tmp_db: Path):
    p = db.create_profile("Tagged", tags=[{"tag": "test", "color": "#aaa"}])
    fetched = db.get_profile(p["id"])
    assert len(fetched["tags"]) == 1
    assert fetched["tags"][0]["tag"] == "test"


# ── list_profiles ────────────────────────────────────────────────────────────


def test_list_profiles_empty(tmp_db: Path):
    assert db.list_profiles() == []


def test_list_profiles_ordered(tmp_db: Path):
    db.create_profile("First")
    time.sleep(0.01)  # ensure different timestamps
    db.create_profile("Second")
    profiles = db.list_profiles()
    assert len(profiles) == 2
    assert profiles[0]["name"] == "Second"  # newest first


def test_list_profiles_includes_tags(tmp_db: Path):
    db.create_profile("Tagged", tags=[{"tag": "x"}])
    profiles = db.list_profiles()
    assert len(profiles[0]["tags"]) == 1


# ── update_profile ───────────────────────────────────────────────────────────


def test_update_profile_partial(sample_profile: dict):
    updated = db.update_profile(sample_profile["id"], name="Renamed")
    assert updated["name"] == "Renamed"
    assert updated["fingerprint_seed"] == 12345  # unchanged


def test_update_profile_tags_replace(tmp_db: Path):
    p = db.create_profile("Tagged", tags=[{"tag": "old"}])
    updated = db.update_profile(p["id"], tags=[{"tag": "new", "color": "#fff"}])
    assert len(updated["tags"]) == 1
    assert updated["tags"][0]["tag"] == "new"


def test_update_profile_not_found(tmp_db: Path):
    assert db.update_profile("nonexistent", name="x") is None


def test_update_profile_no_fields(sample_profile: dict):
    # No-op update — profile should be unchanged
    updated = db.update_profile(sample_profile["id"])
    assert updated["name"] == sample_profile["name"]


def test_update_profile_updates_timestamp(sample_profile: dict):
    time.sleep(0.01)
    updated = db.update_profile(sample_profile["id"], name="New")
    assert updated["updated_at"] > sample_profile["created_at"]


# ── delete_profile ───────────────────────────────────────────────────────────


def test_delete_profile_exists(sample_profile: dict):
    assert db.delete_profile(sample_profile["id"]) is True
    assert db.get_profile(sample_profile["id"]) is None


def test_delete_profile_not_found(tmp_db: Path):
    assert db.delete_profile("nonexistent") is False


def test_delete_profile_cascades_tags(tmp_db: Path):
    p = db.create_profile("Tagged", tags=[{"tag": "a"}, {"tag": "b"}])
    db.delete_profile(p["id"])
    # Verify tags are gone
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM profile_tags WHERE profile_id = ?", (p["id"],)
        ).fetchall()
    assert len(rows) == 0
