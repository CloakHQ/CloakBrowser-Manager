"""Tests for backend/extensions.py — the unpacked-extension scanner."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from backend import extensions


@pytest.fixture(autouse=True)
def _reset_cache():
    """_cache is process-global; leaking it between tests would make
    whichever test runs first decide what every later test sees."""
    extensions._cache = None
    yield
    extensions._cache = None


def _write_extension(base: Path, dir_name: str, manifest: dict) -> Path:
    ext_dir = base / dir_name
    ext_dir.mkdir(parents=True)
    (ext_dir / "manifest.json").write_text(json.dumps(manifest))
    return ext_dir


def test_scan_returns_empty_list_when_dir_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path / "does-not-exist")
    assert extensions.list_available_extensions() == []


def test_scan_finds_a_valid_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    _write_extension(tmp_path, "ublock", {
        "name": "uBlock", "version": "1.0", "description": "Ad blocker",
    })

    result = extensions.list_available_extensions()

    assert len(result) == 1
    assert result[0]["id"] == "ublock"
    assert result[0]["name"] == "uBlock"
    assert result[0]["version"] == "1.0"
    assert result[0]["description"] == "Ad blocker"


def test_scan_skips_hidden_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    (tmp_path / ".gitkeep").mkdir()
    _write_extension(tmp_path, "real-ext", {"name": "Real"})

    result = extensions.list_available_extensions()

    assert [e["id"] for e in result] == ["real-ext"]


def test_scan_skips_a_directory_with_no_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    (tmp_path / "broken").mkdir()
    _write_extension(tmp_path, "good", {"name": "Good"})

    result = extensions.list_available_extensions()

    assert [e["id"] for e in result] == ["good"]


def test_scan_resolves_msg_placeholders_via_locales(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    ext_dir = _write_extension(tmp_path, "localized", {
        "name": "__MSG_extName__", "default_locale": "en",
        "description": "__MSG_extDesc__",
    })
    locale_dir = ext_dir / "_locales" / "en"
    locale_dir.mkdir(parents=True)
    (locale_dir / "messages.json").write_text(json.dumps({
        "extName": {"message": "Localized Name"},
        "extDesc": {"message": "Localized Description"},
    }))

    result = extensions.list_available_extensions()

    assert result[0]["name"] == "Localized Name"
    assert result[0]["description"] == "Localized Description"


def test_list_available_extensions_is_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    _write_extension(tmp_path, "first", {"name": "First"})

    first_call = extensions.list_available_extensions()
    _write_extension(tmp_path, "second", {"name": "Second"})
    second_call = extensions.list_available_extensions()

    assert first_call == second_call  # the new one is invisible until rescan


# ── rescan_extensions ────────────────────────────────────────────────────────


def test_rescan_extensions_picks_up_new_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    _write_extension(tmp_path, "first", {"name": "First"})
    extensions.list_available_extensions()

    _write_extension(tmp_path, "second", {"name": "Second"})
    result = extensions.rescan_extensions()

    assert {e["id"] for e in result} == {"first", "second"}
    # The cache itself was replaced, not just the return value of this call.
    assert {e["id"] for e in extensions.list_available_extensions()} == {"first", "second"}


def test_rescan_extensions_drops_removed_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    ext_dir = _write_extension(tmp_path, "gone", {"name": "Gone"})
    extensions.list_available_extensions()

    shutil.rmtree(ext_dir)
    result = extensions.rescan_extensions()

    assert result == []


def test_extension_paths_for_drops_stale_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    _write_extension(tmp_path, "kept", {"name": "Kept"})

    paths = extensions.extension_paths_for(["kept", "stale-id-that-no-longer-exists"])

    assert len(paths) == 1
    assert paths[0].endswith("kept")
