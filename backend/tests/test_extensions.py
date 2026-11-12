"""Tests for the global extension library (backend/extensions.py) and its API."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import struct
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from backend import browser_manager as browser_module
from backend import database as db
from backend import extensions
from backend.browser_manager import BrowserManager, SEARCH_ENGINE_MARKER
from backend.runtime import RuntimeConfig

# uBlock Origin Lite's real CRX3 public key, and the ID Chrome derives from it.
UBOL_PUBLIC_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqJE6DLdtfHXfeaGDSKdUuCvvb3pYEpH7"
    "jsuYMdExgIqmKRux2dknlJAnYkamBMcirGBfwCFjqE++TAkCs8AcrYLOGRRMt8oa7DXuFWzgtQpkw"
    "igI1+dnIuYLzERLQx/O02/p//fHdDQfxWKvySnN+VHdINOHsUD6OcoC5I7CXIBSrateRpRGpFxxR6"
    "F4nWvK3aq2j3L6A/qRg+pr0QVqL8p1TOJG+vSh234YHnsO9Dsys+VNbT0k0n8enCm/VsOCMjihgH"
    "pmftd1bSfSr8s1IwBuFdaLVQKghyDn1PvhFNBkuAXu+J/z0/o5Nio+/bIz406ECI9Qtkr9kqVboF"
    "DtzwIDAQAB"
)
UBOL_EXTENSION_ID = "ddkjiahejlhfcafbddmgiahcphecmpfh"

NATIVE_RUNTIME = RuntimeConfig(
    host_os="windows",
    runtime_mode="native",
    viewer_mode="native-window",
    data_dir=Path("C:/manager-data"),
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


# ── Builders ─────────────────────────────────────────────────────────────────


def _manifest(**overrides) -> dict:
    manifest = {
        "manifest_version": 3,
        "name": "Test Extension",
        "version": "1.0.0",
        "description": "A test extension",
        "icons": {"16": "icon16.png", "48": "icon48.png", "128": "icon128.png",
                  "256": "icon256.png"},
    }
    manifest.update(overrides)
    return manifest


def _zip(members: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


def _extension_zip(version: str = "1.0.0", **overrides) -> bytes:
    return _zip({
        "manifest.json": json.dumps(_manifest(version=version, **overrides)),
        "icon16.png": PNG_BYTES,
        "icon48.png": PNG_BYTES,
        "icon128.png": PNG_BYTES,
        "icon256.png": PNG_BYTES,
    })


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _crx3(archive: bytes, public_key: bytes, *, crx_id: bytes | None = None) -> bytes:
    proof = _field(1, public_key) + _field(2, b"\x00" * 256)
    signed = _field(1, crx_id or hashlib.sha256(public_key).digest()[:16])
    header = _field(2, proof) + _field(10000, signed)
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + archive


def _crx2(archive: bytes, public_key: bytes) -> bytes:
    signature = b"\x00" * 256
    return (
        b"Cr24"
        + struct.pack("<I", 2)
        + struct.pack("<II", len(public_key), len(signature))
        + public_key
        + signature
        + archive
    )


def _install(
    tmp_db: Path,
    archive_bytes: bytes,
    source: extensions.ArchiveSource | None = None,
    **kwargs,
) -> tuple[dict, bool]:
    archive = tmp_db / f"archive-{uuid.uuid4().hex}.bin"
    archive.write_bytes(archive_bytes)
    try:
        return extensions.install(
            archive, source or extensions.ArchiveSource("upload"), **kwargs
        )
    finally:
        archive.unlink(missing_ok=True)


def _folder(extension_id: str, version: str) -> Path:
    return extensions.version_dir(extension_id, version)


def _manifest_key(folder: Path) -> str:
    return json.loads((folder / "manifest.json").read_text())["key"]


# ── Chrome IDs and keys ──────────────────────────────────────────────────────


def test_chrome_id_from_a_real_public_key():
    public_key = extensions.decode_public_key(UBOL_PUBLIC_KEY)
    assert extensions.chrome_id_from_public_key(public_key) == UBOL_EXTENSION_ID


def test_public_key_base64_roundtrip():
    public_key = extensions.generate_public_key()
    assert extensions.decode_public_key(extensions.encode_public_key(public_key)) == public_key


def test_decode_public_key_rejects_garbage():
    with pytest.raises(extensions.ExtensionError):
        extensions.decode_public_key("not base64 !!")


# ── Source parsing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("source", [
    UBOL_EXTENSION_ID,
    f"https://chromewebstore.google.com/detail/ublock-origin-lite/{UBOL_EXTENSION_ID}",
    f"https://chrome.google.com/webstore/detail/ublock-origin-lite/{UBOL_EXTENSION_ID}",
    f"https://chromewebstore.google.com/detail/{UBOL_EXTENSION_ID}?hl=en",
])
def test_parse_source_recognises_webstore(source: str):
    parsed = extensions.parse_source(source)
    assert parsed.kind == "webstore"
    assert parsed.webstore_id == UBOL_EXTENSION_ID
    assert parsed.source_url == extensions.webstore_url(UBOL_EXTENSION_ID)


def test_webstore_url_asks_for_the_oldest_launched_engine():
    url = extensions.webstore_url(UBOL_EXTENSION_ID)
    assert f"prodversion={extensions.CWS_PROD_VERSION}" in url
    assert extensions.CWS_PROD_VERSION == "145.0.7632.109"
    assert f"x=id%3D{UBOL_EXTENSION_ID}%26uc" in url


def test_parse_source_keeps_a_plain_url():
    parsed = extensions.parse_source("https://example.com/ext.crx")
    assert parsed.kind == "url"
    assert parsed.source_url == "https://example.com/ext.crx"
    assert parsed.webstore_id is None


@pytest.mark.parametrize("source", ["", "   ", "ftp://example.com/ext.crx", "uBlock Origin"])
def test_parse_source_rejects_bad_input(source: str):
    with pytest.raises(extensions.ExtensionError):
        extensions.parse_source(source)


def test_parse_source_rejects_webstore_link_without_id():
    with pytest.raises(extensions.ExtensionError) as error:
        extensions.parse_source("https://chromewebstore.google.com/detail/ublock")
    assert error.value.status == 400


# ── CRX parsing ──────────────────────────────────────────────────────────────


def test_crx3_import_uses_header_key_and_matching_id(tmp_db: Path):
    public_key = extensions.generate_public_key()
    expected_id = extensions.chrome_id_from_public_key(public_key)
    archive = _crx3(_extension_zip(), public_key)

    row, updated = _install(tmp_db, archive, extensions.ArchiveSource("crx"))

    assert updated is True
    assert row["id"] == expected_id
    assert row["manifest_version"] == 3
    assert row["name"] == "Test Extension"
    assert _manifest_key(_folder(row["id"], "1.0.0")) == extensions.encode_public_key(public_key)


def test_crx3_header_crx_id_matches_derived_id(tmp_db: Path):
    public_key = extensions.generate_public_key()
    header = _crx3(b"", public_key)

    parsed = extensions.read_crx_header(io.BytesIO(header))

    assert parsed.version == 3
    assert parsed.public_key == public_key
    assert parsed.crx_id == extensions.chrome_id_from_public_key(public_key)


def test_crx3_with_a_mismatched_crx_id_is_refused(tmp_db: Path):
    public_key = extensions.generate_public_key()
    other_key = extensions.generate_public_key()
    archive = _crx3(
        _extension_zip(),
        public_key,
        crx_id=hashlib.sha256(other_key).digest()[:16],
    )

    with pytest.raises(extensions.ExtensionError) as error:
        _install(tmp_db, archive, extensions.ArchiveSource("crx"))
    assert "does not match" in str(error.value)


def test_crx2_import_uses_the_header_key(tmp_db: Path):
    public_key = extensions.generate_public_key()
    archive = _crx2(_extension_zip(), public_key)

    row, _ = _install(tmp_db, archive, extensions.ArchiveSource("crx"))

    assert row["id"] == extensions.chrome_id_from_public_key(public_key)
    assert row["source_kind"] == "crx"


def test_non_archive_payload_is_refused(tmp_db: Path):
    with pytest.raises(extensions.ExtensionError):
        _install(tmp_db, b"<html>not an extension</html>")


# ── ZIP archives ─────────────────────────────────────────────────────────────


def test_zip_with_one_wrapping_folder_is_unwrapped(tmp_db: Path):
    archive = _zip({
        "uBlock0.chromium/manifest.json": json.dumps(_manifest(version="1.76.0")),
        "uBlock0.chromium/icon128.png": PNG_BYTES,
    })

    row, _ = _install(tmp_db, archive)

    folder = _folder(row["id"], "1.76.0")
    assert (folder / "manifest.json").is_file()
    assert (folder / "icon128.png").is_file()
    assert not (folder / "uBlock0.chromium").exists()
    assert row["version"] == "1.76.0"


@pytest.mark.parametrize("name", ["../escape.txt", "/etc/escape.txt", "sub/../../escape.txt"])
def test_unsafe_zip_paths_are_refused(tmp_db: Path, name: str):
    archive = _zip({"manifest.json": json.dumps(_manifest()), name: "boom"})

    with pytest.raises(extensions.ExtensionError):
        _install(tmp_db, archive)
    assert not (tmp_db / "escape.txt").exists()


def test_symlink_zip_entries_are_refused(tmp_db: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(_manifest()))
        link = zipfile.ZipInfo("link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(link, "/etc/passwd")

    with pytest.raises(extensions.ExtensionError):
        _install(tmp_db, buffer.getvalue())


def test_zip_without_manifest_is_refused(tmp_db: Path):
    with pytest.raises(extensions.ExtensionError):
        _install(tmp_db, _zip({"README.md": "hello"}))


@pytest.mark.parametrize("manifest", [
    {"name": "No version"},
    {"manifest_version": 3, "version": "1.0"},
    {"manifest_version": 4, "name": "Bad", "version": "1.0"},
    {"manifest_version": 3, "name": "", "version": "1.0"},
])
def test_invalid_manifests_are_refused(tmp_db: Path, manifest: dict):
    archive = _zip({"manifest.json": json.dumps(manifest)})
    with pytest.raises(extensions.ExtensionError):
        _install(tmp_db, archive)


def test_manifest_localisation_uses_default_locale(tmp_db: Path):
    archive = _zip({
        "manifest.json": json.dumps({
            "manifest_version": 3,
            "name": "__MSG_extName__",
            "description": "__MSG_extDesc__",
            "version": "3.1.4",
            "default_locale": "en",
        }),
        "_locales/en/messages.json": json.dumps({
            # Chrome's message keys are case-insensitive.
            "EXTNAME": {"message": "uBlock Origin Lite"},
            "extdesc": {"message": "Blocks ads."},
        }),
    })

    row, _ = _install(tmp_db, archive)

    assert row["name"] == "uBlock Origin Lite"
    assert row["description"] == "Blocks ads."


def test_icon_is_the_largest_at_most_128(tmp_db: Path):
    row, _ = _install(tmp_db, _extension_zip())
    assert row["icon"] == "icon128.png"
    assert extensions.icon_path(row).name == "icon128.png"


def test_icon_is_null_when_the_manifest_has_none(tmp_db: Path):
    archive = _zip({"manifest.json": json.dumps({
        "manifest_version": 3, "name": "No icon", "version": "1.0.0",
    })})

    row, _ = _install(tmp_db, archive)

    assert row["icon"] is None
    assert extensions.icon_path(row) is None


# ── Key injection, versions, rollback ────────────────────────────────────────


def test_injected_key_keeps_the_id_across_versions(tmp_db: Path):
    first, _ = _install(tmp_db, _extension_zip("1.0.0"))
    first_folder = _folder(first["id"], "1.0.0")

    second, updated = _install(
        tmp_db, _extension_zip("2.0.0"), extension_id=first["id"]
    )

    assert updated is True
    assert second["id"] == first["id"]
    assert second["version"] == "2.0.0"
    assert second["previous_version"] == "1.0.0"
    assert _manifest_key(_folder(second["id"], "2.0.0")) == _manifest_key(first_folder)
    # Current + previous survive.
    assert first_folder.is_dir()
    assert _folder(second["id"], "2.0.0").is_dir()


def test_same_version_is_not_updated(tmp_db: Path):
    first, _ = _install(tmp_db, _extension_zip("1.0.0"))

    second, updated = _install(
        tmp_db, _extension_zip("1.0.0"), extension_id=first["id"]
    )

    assert updated is False
    assert second["version"] == "1.0.0"
    assert second["previous_version"] is None


def test_update_refuses_a_different_extension(tmp_db: Path):
    first, _ = _install(tmp_db, _extension_zip("1.0.0", key=(
        extensions.encode_public_key(extensions.generate_public_key())
    )))
    other_key = extensions.encode_public_key(extensions.generate_public_key())

    with pytest.raises(extensions.ExtensionError) as error:
        _install(
            tmp_db,
            _extension_zip("2.0.0", key=other_key),
            extension_id=first["id"],
        )
    assert "different extension" in str(error.value)


def test_rollback_swaps_versions(tmp_db: Path):
    first, _ = _install(tmp_db, _extension_zip("1.0.0"))
    second, _ = _install(tmp_db, _extension_zip("2.0.0"), extension_id=first["id"])

    rolled_back = extensions.rollback(second["id"])

    assert rolled_back["version"] == "1.0.0"
    assert rolled_back["previous_version"] == "2.0.0"


def test_rollback_without_a_previous_version_is_refused(tmp_db: Path):
    row, _ = _install(tmp_db, _extension_zip("1.0.0"))

    with pytest.raises(extensions.ExtensionError) as error:
        extensions.rollback(row["id"])
    assert error.value.status == 400


def test_prune_keeps_a_folder_a_running_browser_loaded(tmp_db: Path):
    first, _ = _install(tmp_db, _extension_zip("1.0.0"))
    in_use = {str(_folder(first["id"], "1.0.0").resolve())}
    _install(tmp_db, _extension_zip("2.0.0"), extension_id=first["id"])
    third, _ = _install(
        tmp_db, _extension_zip("3.0.0"), extension_id=first["id"], in_use=in_use
    )

    assert _folder(third["id"], "1.0.0").is_dir()
    assert _folder(third["id"], "3.0.0").is_dir()

    extensions.prune(third["id"], third)

    assert not _folder(third["id"], "1.0.0").exists()
    assert _folder(third["id"], "3.0.0").is_dir()


def test_delete_drops_the_entry_and_its_files(tmp_db: Path):
    row, _ = _install(tmp_db, _extension_zip())

    extensions.delete(row["id"])

    assert db.get_extension(row["id"]) is None
    assert not extensions.extension_dir(row["id"]).exists()


def test_delete_is_refused_while_a_profile_references_it(tmp_db: Path):
    row, _ = _install(tmp_db, _extension_zip())
    db.create_profile(name="Linked", extension_ids=[row["id"]])

    with pytest.raises(extensions.ExtensionInUse) as error:
        extensions.delete(row["id"])

    assert error.value.status == 409
    assert "Linked" in str(error.value)
    assert db.get_extension(row["id"]) is not None


def test_installing_a_known_extension_is_a_conflict(tmp_db: Path):
    _install(tmp_db, _extension_zip("1.0.0", key=(
        extensions.encode_public_key(extensions.generate_public_key())
    )))
    key = extensions.encode_public_key(extensions.generate_public_key())
    _install(tmp_db, _extension_zip("1.0.0", key=key))

    with pytest.raises(extensions.ExtensionExists) as error:
        _install(tmp_db, _extension_zip("1.0.0", key=key))
    assert error.value.status == 409


def test_stored_source_of_an_upload_is_refused(tmp_db: Path):
    row, _ = _install(tmp_db, _extension_zip())
    with pytest.raises(extensions.ExtensionError):
        extensions.stored_source(row)


def test_reclaim_removes_staging_and_unreferenced_folders(tmp_db: Path):
    row, _ = _install(tmp_db, _extension_zip("1.0.0"))
    _install(tmp_db, _extension_zip("2.0.0"), extension_id=row["id"])
    stale_staging = extensions.library_root() / ".incoming-deadbeef"
    stale_staging.mkdir(parents=True)
    stale_version = _folder(row["id"], "0.9.0")
    stale_version.mkdir(parents=True)
    orphan = extensions.library_root() / ("a" * 32)
    orphan.mkdir(parents=True)
    # Hand-unpacked folders for custom paths: in the library root by mistake, and in the
    # `extensions/` folder users kept them in before the library existed.
    users_own = extensions.library_root() / "ublock"
    (users_own / "js").mkdir(parents=True)
    legacy_custom = tmp_db / "extensions" / "ublock"
    legacy_custom.mkdir(parents=True)

    removed = extensions.reclaim()

    assert removed == 3
    assert not stale_staging.exists()
    assert not stale_version.exists()
    assert not orphan.exists()
    assert _folder(row["id"], "1.0.0").is_dir()
    assert _folder(row["id"], "2.0.0").is_dir()
    assert (users_own / "js").is_dir()
    assert legacy_custom.is_dir()


# ── Profiles ─────────────────────────────────────────────────────────────────


def test_profile_extension_paths_resolve_the_current_version(tmp_db: Path):
    first, _ = _install(tmp_db, _extension_zip("1.0.0"))
    _install(tmp_db, _extension_zip("2.0.0"), extension_id=first["id"])
    profile = {"id": "p1", "extension_ids": [first["id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}

    paths = extensions.profile_extension_paths(profile)

    assert paths == [str(_folder(first["id"], "2.0.0"))]


def test_duplicate_copies_extension_ids(tmp_db: Path):
    row, _ = _install(tmp_db, _extension_zip())
    profile = db.create_profile(name="Source", extension_ids=[row["id"]])

    clone = db.duplicate_profile(profile["id"])

    assert clone["extension_ids"] == [row["id"]]


def test_profile_extension_ids_roundtrip(tmp_db: Path):
    row, _ = _install(tmp_db, _extension_zip())

    profile = db.create_profile(name="Round", extension_ids=[row["id"]])
    assert profile["extension_ids"] == [row["id"]]

    updated = db.update_profile(profile["id"], extension_ids=[])
    assert updated["extension_ids"] == []


# ── Launch integration ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_launch_passes_library_dirs_before_custom_paths(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    row, _ = _install(tmp_db, _extension_zip())
    custom = str(tmp_db / "custom-extension")
    user_data_dir = tmp_db / "profile-1"
    user_data_dir.mkdir()
    (user_data_dir / SEARCH_ENGINE_MARKER).write_text("google\n")

    context = MagicMock(pages=[])
    context.add_init_script = AsyncMock()
    manager = BrowserManager(RuntimeConfig("windows", "native", "native-window", tmp_db))
    manager._wait_for_cdp = AsyncMock()
    launch = AsyncMock(return_value=context)
    monkeypatch.setattr(browser_module, "launch_persistent_context_async", launch)

    running = await manager.launch({
        "id": "profile-1",
        "name": "Launch",
        "license_key": "cb_test_key",
        "user_data_dir": str(user_data_dir),
        "extension_ids": [row["id"]],
        "extension_paths": [custom],
        "launch_args": [],
    })

    library_dir = str(_folder(row["id"], "1.0.0"))
    options = launch.await_args.kwargs
    assert options["extension_paths"] == [library_dir, custom]
    assert running.extension_dirs == (library_dir,)
    assert manager.extension_dirs_in_use() == {str(Path(library_dir).resolve())}


# ── API ──────────────────────────────────────────────────────────────────────


def _upload(client: TestClient, data: bytes, name: str = "ext.zip", **params):
    from urllib.parse import quote

    return client.post(
        "/api/extensions/upload",
        content=data,
        params=params,
        headers={"X-File-Name": quote(name), "Content-Type": "application/zip"},
    )


def test_upload_then_list_and_icon(app_client: TestClient):
    response = _upload(app_client, _extension_zip())

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test Extension"
    assert data["source_kind"] == "upload"
    assert data["source_url"] is None
    assert data["icon_url"] == f"/api/extensions/{data['id']}/icon"
    assert data["profile_count"] == 0

    listed = app_client.get("/api/extensions").json()
    assert [row["id"] for row in listed] == [data["id"]]

    icon = app_client.get(data["icon_url"])
    assert icon.status_code == 200
    assert icon.content == PNG_BYTES


def test_upload_requires_the_file_name_header(app_client: TestClient):
    response = app_client.post("/api/extensions/upload", content=_extension_zip())
    assert response.status_code == 400


def test_upload_into_an_existing_entry_answers_200(app_client: TestClient):
    first = _upload(app_client, _extension_zip("1.0.0")).json()

    response = _upload(app_client, _extension_zip("2.0.0"), extension_id=first["id"])

    assert response.status_code == 200
    assert response.json()["version"] == "2.0.0"
    assert response.json()["previous_version"] == "1.0.0"


def test_upload_of_a_same_version_answers_200(app_client: TestClient):
    first = _upload(app_client, _extension_zip("1.0.0")).json()

    response = _upload(app_client, _extension_zip("1.0.0"), extension_id=first["id"])

    assert response.status_code == 200
    assert response.json()["previous_version"] is None


def test_add_extension_rejects_bad_input(app_client: TestClient):
    response = app_client.post("/api/extensions", json={"source": "not a source"})
    assert response.status_code == 400


def test_update_of_an_upload_without_a_source_is_refused(app_client: TestClient):
    created = _upload(app_client, _extension_zip()).json()

    response = app_client.post(f"/api/extensions/{created['id']}/update", json={"source": None})
    assert response.status_code == 400

    # No body at all must read the same way, not fail validation.
    assert app_client.post(f"/api/extensions/{created['id']}/update").status_code == 400


def test_update_rollback_and_delete_endpoints(app_client: TestClient):
    first = _upload(app_client, _extension_zip("1.0.0")).json()
    updated = _upload(app_client, _extension_zip("2.0.0"), extension_id=first["id"]).json()

    rolled_back = app_client.post(f"/api/extensions/{first['id']}/rollback")
    assert rolled_back.status_code == 200
    assert rolled_back.json()["version"] == "1.0.0"
    assert rolled_back.json()["previous_version"] == "2.0.0"

    assert app_client.delete(f"/api/extensions/{first['id']}").json() == {"ok": True}
    assert app_client.get("/api/extensions").json() == []
    assert app_client.delete(f"/api/extensions/{updated['id']}").status_code == 404


def test_delete_is_a_conflict_while_a_profile_uses_it(app_client: TestClient):
    created = _upload(app_client, _extension_zip()).json()
    app_client.post("/api/profiles", json={"name": "Uses it", "extension_ids": [created["id"]]})

    response = app_client.delete(f"/api/extensions/{created['id']}")

    assert response.status_code == 409
    assert "Uses it" in response.json()["detail"]
    assert app_client.get("/api/extensions").json()[0]["profile_count"] == 1


def test_profile_rejects_an_unknown_extension_id(app_client: TestClient):
    create = app_client.post(
        "/api/profiles", json={"name": "Bad", "extension_ids": ["a" * 32]}
    )
    assert create.status_code == 400

    created = app_client.post("/api/profiles", json={"name": "Good"}).json()
    update = app_client.put(
        f"/api/profiles/{created['id']}", json={"extension_ids": ["a" * 32]}
    )
    assert update.status_code == 400


def test_profile_create_and_update_keep_extension_ids(app_client: TestClient):
    extension = _upload(app_client, _extension_zip()).json()

    created = app_client.post(
        "/api/profiles", json={"name": "With ext", "extension_ids": [extension["id"]]}
    )
    assert created.status_code == 201
    assert created.json()["extension_ids"] == [extension["id"]]

    cleared = app_client.put(
        f"/api/profiles/{created.json()['id']}", json={"extension_ids": []}
    )
    assert cleared.json()["extension_ids"] == []


def test_icon_endpoint_404s_without_an_icon(app_client: TestClient):
    archive = _zip({"manifest.json": json.dumps({
        "manifest_version": 2, "name": "MV2 no icon", "version": "0.9.0",
    })})
    created = _upload(app_client, archive, name="plain.zip").json()

    assert created["icon_url"] is None
    assert app_client.get(f"/api/extensions/{created['id']}/icon").status_code == 404
