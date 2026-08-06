"""Tests for backend/extensions.py — the unpacked-extension scanner."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
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


# ── installing (upload or Chrome Web Store fetch) ────────────────────────────


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _wrap_as_crx(zip_bytes: bytes) -> bytes:
    """A minimal CRX3 wrapper: magic + version + zero-length header + zip.
    Sufficient for _strip_crx_header, which only skips bytes — it does not
    parse the header's protobuf contents. Verifying the publisher signature
    on that header (as docker/fetch-widevine.py does) is a different trust
    boundary: a native .so Chromium loads vs. a sandboxed extension fetched
    over TLS from Google's own server, or uploaded by an already-
    authenticated operator.
    """
    return b"Cr24" + (3).to_bytes(4, "little") + (0).to_bytes(4, "little") + zip_bytes


def test_slugify_lowercases_and_dashes():
    assert extensions._slugify("My Cool Extension!") == "my-cool-extension"


def test_slugify_falls_back_when_empty():
    assert extensions._slugify("!!!") == "extension"


def test_dedupe_extension_dir_name_no_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    assert extensions._dedupe_extension_dir_name("foo") == "foo"


def test_dedupe_extension_dir_name_numbers_on_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    (tmp_path / "foo").mkdir()
    assert extensions._dedupe_extension_dir_name("foo") == "foo-2"
    (tmp_path / "foo-2").mkdir()
    assert extensions._dedupe_extension_dir_name("foo") == "foo-3"


def test_is_crx_detects_magic_bytes():
    assert extensions._is_crx(b"Cr24" + b"\x00" * 10) is True
    assert extensions._is_crx(b"PK\x03\x04" + b"\x00" * 10) is False


def test_strip_crx_header_returns_the_zip_payload():
    zip_bytes = _make_zip({"manifest.json": b"{}"})
    crx_bytes = _wrap_as_crx(zip_bytes)
    assert extensions._strip_crx_header(crx_bytes) == zip_bytes


def test_safe_extract_zip_rejects_path_traversal(tmp_path):
    dest = tmp_path / "dest"
    zip_bytes = _make_zip({"../../evil.txt": b"pwned"})
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    with pytest.raises(ValueError, match="[Uu]nsafe"):
        extensions._safe_extract_zip(zf, dest)


def test_safe_extract_zip_allows_normal_members(tmp_path):
    dest = tmp_path / "dest"
    zip_bytes = _make_zip({"manifest.json": b"{}", "icons/icon.png": b"fake"})
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    extensions._safe_extract_zip(zf, dest)
    assert (dest / "manifest.json").is_file()
    assert (dest / "icons" / "icon.png").is_file()


def test_install_extension_from_bytes_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    zip_bytes = _make_zip({
        "manifest.json": json.dumps({"name": "My Cool Ext", "version": "2.0"}).encode(),
    })

    result = extensions.install_extension_from_bytes(zip_bytes, "upload.zip")

    assert result["id"] == "my-cool-ext"
    assert result["name"] == "My Cool Ext"
    assert result["version"] == "2.0"
    assert (tmp_path / "my-cool-ext" / "manifest.json").is_file()


def test_install_extension_from_bytes_crx(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    zip_bytes = _make_zip({"manifest.json": json.dumps({"name": "From CRX"}).encode()})
    crx_bytes = _wrap_as_crx(zip_bytes)

    result = extensions.install_extension_from_bytes(crx_bytes, "ext.crx")

    assert result["id"] == "from-crx"


def test_install_extension_from_bytes_rejects_oversized_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    monkeypatch.setattr(extensions, "MAX_EXTENSION_UPLOAD_BYTES", 10)
    with pytest.raises(ValueError, match="limit"):
        extensions.install_extension_from_bytes(b"x" * 100, "big.zip")


def test_install_extension_from_bytes_rejects_non_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    with pytest.raises(ValueError, match=r"valid \.zip or \.crx"):
        extensions.install_extension_from_bytes(b"not a zip", "junk.zip")


def test_install_extension_from_bytes_rejects_missing_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    zip_bytes = _make_zip({"readme.txt": b"no manifest here"})
    with pytest.raises(ValueError, match="manifest.json"):
        extensions.install_extension_from_bytes(zip_bytes, "bad.zip")


def test_install_extension_from_bytes_dedupes_repeated_installs(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    zip_bytes = _make_zip({"manifest.json": json.dumps({"name": "Dup Ext"}).encode()})

    first = extensions.install_extension_from_bytes(zip_bytes, "a.zip")
    second = extensions.install_extension_from_bytes(zip_bytes, "b.zip")

    assert first["id"] == "dup-ext"
    assert second["id"] == "dup-ext-2"


def test_install_extension_from_bytes_falls_back_to_filename_when_unnamed(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "EXTENSIONS_DIR", tmp_path)
    zip_bytes = _make_zip({"manifest.json": json.dumps({}).encode()})

    result = extensions.install_extension_from_bytes(zip_bytes, "my-upload.zip")

    assert result["id"] == "my-upload"


def test_extract_extension_id_from_store_url():
    url = (
        "https://chromewebstore.google.com/detail/"
        "i-still-dont-care-about-c/edibdbjcniadpccecjdfdjjppcpchdlm"
    )
    assert extensions.extract_extension_id(url) == "edibdbjcniadpccecjdfdjjppcpchdlm"


def test_extract_extension_id_from_bare_id():
    assert (
        extensions.extract_extension_id("edibdbjcniadpccecjdfdjjppcpchdlm")
        == "edibdbjcniadpccecjdfdjjppcpchdlm"
    )


def test_extract_extension_id_returns_none_when_absent():
    assert extensions.extract_extension_id("https://example.com/not-an-extension") is None


def test_extract_extension_id_is_case_insensitive():
    assert (
        extensions.extract_extension_id("EDIBDBJCNIADPCCECJDFDJJPPCPCHDLM")
        == "edibdbjcniadpccecjdfdjjppcpchdlm"
    )


def test_chrome_web_store_crx_url_embeds_the_id():
    url = extensions.chrome_web_store_crx_url("edibdbjcniadpccecjdfdjjppcpchdlm")
    assert "id%3Dedibdbjcniadpccecjdfdjjppcpchdlm" in url
    assert url.startswith("https://clients2.google.com/service/update2/crx")
