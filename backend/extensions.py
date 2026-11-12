"""Global extension library — CRX/ZIP sources unpacked under the data directory.

An unpacked extension's ID is derived from its folder path unless the manifest carries
a ``key``. Every version therefore gets the entry's public key injected into its
manifest, so the ID (and with it the extension's own storage, e.g. uBO Lite's filter
settings) survives updates that land in a new version folder.

Layout: ``<DATA_DIR>/extensions/<chrome id>/<version>/`` with ``manifest.json`` at the
version folder's root. Folders are writable: Chromium writes
``_metadata/generated_indexed_rulesets`` into MV3 extensions and hangs on launch if it
cannot.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import struct
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterable, Iterable
from urllib.parse import parse_qsl, urlparse

import httpx

from . import database as db

logger = logging.getLogger("cloakbrowser.manager.extensions")

# Chrome Web Store's update endpoint. It answers 200 + CRX3 for a listed extension and
# 204 (empty) for one it no longer distributes (MV2 builds, e.g. uBlock Origin).
CWS_DOWNLOAD_URL = (
    "https://clients2.google.com/service/update2/crx?response=redirect"
    "&prodversion={version}&acceptformat=crx2,crx3&x=id%3D{extension_id}%26uc"
)
# The oldest engine the Manager launches (binary_cache's keyless default), so the store
# never serves a build that engine is too old to load.
CWS_PROD_VERSION = "145.0.7632.109"

# Caps. Downloads are capped while streaming; archives are capped again on unpack.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024

CHROME_ID_ALPHABET = "abcdefghijklmnop"

DELISTED_MESSAGE = (
    "The Chrome Web Store does not serve this extension (delisted; MV2 extensions "
    "such as uBlock Origin are no longer distributed there)"
)

_CHROME_ID_RE = re.compile(r"^[a-p]{32}$")
# Manifest versions are free-form strings, but they name a directory — keep them to a
# plain path component.
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
_MSG_RE = re.compile(r"__MSG_([A-Za-z0-9_@]+?)__", re.IGNORECASE)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:$")
_WEBSTORE_HOSTS = {"chromewebstore.google.com", "chrome.google.com"}

# One library mutation at a time: two installs of the same extension would otherwise
# both publish a folder and race on the row.
_LOCK = threading.Lock()


# ── Errors ───────────────────────────────────────────────────────────────────


class ExtensionError(Exception):
    """A library operation the caller can act on; ``status`` is the HTTP status."""

    status = 400


class ExtensionNotFound(ExtensionError):
    status = 404


class ExtensionExists(ExtensionError):
    status = 409


class ExtensionInUse(ExtensionError):
    status = 409


class ExtensionDownloadError(ExtensionError):
    status = 502


# ── Storage layout ───────────────────────────────────────────────────────────


def library_root() -> Path:
    # Not `extensions/`: that is where users have long kept hand-unpacked folders for
    # custom paths, and reclaim() deletes what the library does not own.
    return Path(db.DATA_DIR) / "extension-library"


def extension_dir(extension_id: str) -> Path:
    return library_root() / extension_id


def version_dir(extension_id: str, version: str) -> Path:
    return extension_dir(extension_id) / version


def staging_path() -> Path:
    """A fresh temp file inside the library, so unpack and rename stay on one filesystem."""
    root = library_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / f".incoming-{uuid.uuid4().hex}"


def _check_version(value: str) -> str:
    if not _VERSION_RE.fullmatch(value):
        raise ExtensionError(f"manifest.json has an unusable version: {value!r}")
    return value


# ── Chrome IDs and keys ──────────────────────────────────────────────────────


def chrome_id_from_public_key(public_key: bytes) -> str:
    """Chrome's extension ID: the first 16 bytes of sha256(DER key), each hex digit
    mapped 0-f onto a-p."""
    digest = hashlib.sha256(public_key).hexdigest()[:32]
    return "".join(CHROME_ID_ALPHABET[int(nibble, 16)] for nibble in digest)


def encode_public_key(public_key: bytes) -> str:
    return base64.b64encode(public_key).decode("ascii")


def decode_public_key(text: str) -> bytes:
    try:
        public_key = base64.b64decode(text.strip(), validate=True)
    except Exception as exc:  # binascii.Error, ValueError
        raise ExtensionError("The extension's public key is not valid base64") from exc
    if not public_key:
        raise ExtensionError("The extension's public key is empty")
    return public_key


def generate_public_key() -> bytes:
    """A fresh RSA-2048 public key (DER SubjectPublicKeyInfo) for an archive with no
    key of its own. Only the public half is kept: it is all an unpacked extension
    needs to hold a stable ID."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


# ── CRX headers ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CrxHeader:
    version: int
    archive_offset: int
    public_key: bytes | None
    crx_id: str | None


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ExtensionError("CRX header is truncated")


def _protobuf_fields(data: bytes) -> list[tuple[int, bytes | int]]:
    """A minimal protobuf reader: enough for CRX3's CrxFileHeader and SignedData."""
    fields: list[tuple[int, bytes | int]] = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field_number, wire_type = key >> 3, key & 0x7
        if wire_type == 2:  # length-delimited
            length, offset = _read_varint(data, offset)
            if offset + length > len(data):
                raise ExtensionError("CRX header is truncated")
            fields.append((field_number, data[offset:offset + length]))
            offset += length
        elif wire_type == 0:  # varint
            value, offset = _read_varint(data, offset)
            fields.append((field_number, value))
        else:
            raise ExtensionError("CRX header uses an unsupported protobuf encoding")
    return fields


def _signed_crx_id(signed_header_data: bytes) -> str | None:
    """The ID a CRX3 header claims, spelled the way Chrome spells IDs."""
    crx_id = dict(_protobuf_fields(signed_header_data)).get(1)
    if not isinstance(crx_id, bytes) or len(crx_id) != 16:
        return None
    return "".join(
        CHROME_ID_ALPHABET[byte >> 4] + CHROME_ID_ALPHABET[byte & 0x0F] for byte in crx_id
    )


def read_crx_header(stream) -> CrxHeader:
    """Parse a CRX2/CRX3 header from an open binary stream positioned at 0.

    CRX3 carries several AsymmetricKeyProofs; the one to trust is the sha256_with_rsa
    proof whose derived ID matches the header's own crx_id (the others sign the same
    archive under a different key).
    """
    if stream.read(4) != b"Cr24":
        raise ExtensionError("That file is not a CRX archive")
    raw = stream.read(4)
    if len(raw) < 4:
        raise ExtensionError("CRX header is truncated")
    version = struct.unpack("<I", raw)[0]

    if version == 2:
        raw = stream.read(8)
        if len(raw) < 8:
            raise ExtensionError("CRX header is truncated")
        key_length, signature_length = struct.unpack("<II", raw)
        if not 0 < key_length <= 1_000_000 or signature_length > 1_000_000:
            raise ExtensionError("CRX header is malformed")
        public_key = stream.read(key_length)
        if len(public_key) != key_length:
            raise ExtensionError("CRX header is truncated")
        if len(stream.read(signature_length)) != signature_length:
            raise ExtensionError("CRX header is truncated")
        return CrxHeader(
            version=2,
            archive_offset=16 + key_length + signature_length,
            public_key=public_key,
            crx_id=chrome_id_from_public_key(public_key),
        )

    if version != 3:
        raise ExtensionError(f"Unsupported CRX version {version}")

    raw = stream.read(4)
    if len(raw) < 4:
        raise ExtensionError("CRX header is truncated")
    header_size = struct.unpack("<I", raw)[0]
    if not 0 < header_size <= 16 * 1024 * 1024:
        raise ExtensionError("CRX header is malformed")
    header = stream.read(header_size)
    if len(header) != header_size:
        raise ExtensionError("CRX header is truncated")

    rsa_keys: list[bytes] = []
    other_keys: list[bytes] = []
    crx_id: str | None = None
    for field_number, value in _protobuf_fields(header):
        if field_number in (2, 3) and isinstance(value, bytes):
            public_key = dict(_protobuf_fields(value)).get(1)
            if isinstance(public_key, bytes) and public_key:
                (rsa_keys if field_number == 2 else other_keys).append(public_key)
        elif field_number == 10000 and isinstance(value, bytes):
            crx_id = _signed_crx_id(value) or crx_id

    candidates = [*rsa_keys, *other_keys]
    public_key: bytes | None = None
    if crx_id is not None:
        public_key = next(
            (key for key in candidates if chrome_id_from_public_key(key) == crx_id), None
        )
        if public_key is None and candidates:
            # A header whose proofs disagree with its own crx_id is corrupt. Taking any
            # proof would install an extension other than the one it claims to be.
            raise ExtensionError("The CRX signature does not match its extension ID")
    elif candidates:
        public_key = candidates[0]

    return CrxHeader(
        version=3,
        archive_offset=12 + header_size,
        public_key=public_key,
        crx_id=crx_id or (chrome_id_from_public_key(public_key) if public_key else None),
    )


# ── Archives ─────────────────────────────────────────────────────────────────


def sniff_archive(path: Path) -> str:
    """``"crx"``/``"zip"`` from the file's magic; anything else is a bad archive."""
    with path.open("rb") as handle:
        magic = handle.read(4)
    if magic == b"Cr24":
        return "crx"
    if magic[:2] == b"PK":
        return "zip"
    raise ExtensionError("That file is not a CRX or ZIP archive")


def _member_parts(name: str) -> tuple[str, ...] | None:
    """Path components of a zip member, or None if it may not be written."""
    normalised = name.replace("\\", "/")
    if not normalised or normalised.startswith("/"):
        return None
    parts = PurePosixPath(normalised).parts
    if not parts:
        return None
    if any(part in ("..", ".") for part in parts):
        return None
    if _WINDOWS_DRIVE_RE.match(parts[0]):
        return None
    return parts


def _extract_zip(archive: Path, destination: Path) -> None:
    """Unpack an archive (a CRX's zip, or a plain zip) into ``destination``.

    Refuses absolute paths, ``..`` components and symlinks, strips one wrapping
    top-level folder (a GitHub release zip has its manifest one level down), and caps
    the bytes actually written rather than trusting the declared sizes.
    """
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = [info for info in bundle.infolist() if not info.is_dir()]
            members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
            declared = 0
            for info in infos:
                parts = _member_parts(info.filename)
                if parts is None:
                    raise ExtensionError(f"The archive contains an unsafe path: {info.filename!r}")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ExtensionError(f"The archive contains a symlink: {info.filename!r}")
                declared += info.file_size
                if declared > MAX_UNPACKED_BYTES:
                    raise ExtensionError(
                        f"The unpacked archive would exceed {MAX_UNPACKED_BYTES} bytes"
                    )
                members.append((info, parts))

            prefix = _wrapping_prefix([parts for _, parts in members])
            written = 0
            for info, parts in members:
                relative = parts[len(prefix):]
                if not relative:
                    continue
                target = destination.joinpath(*relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("wb") as sink:
                    while chunk := source.read(1 << 20):
                        written += len(chunk)
                        if written > MAX_UNPACKED_BYTES:
                            raise ExtensionError(
                                f"The unpacked archive would exceed {MAX_UNPACKED_BYTES} bytes"
                            )
                        sink.write(chunk)
    except zipfile.BadZipFile as exc:
        raise ExtensionError("The archive is not a valid ZIP") from exc
    except OSError as exc:
        raise ExtensionError(f"Could not unpack the archive: {exc}") from exc

    if not (destination / "manifest.json").is_file():
        raise ExtensionError("The archive has no manifest.json")


def _wrapping_prefix(all_parts: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    """The single top-level folder every member sits in, or ``()`` when the manifest is
    already at the archive root."""
    parts = list(all_parts)
    if any(len(item) == 1 and item[0] == "manifest.json" for item in parts):
        return ()
    roots = {item[0] for item in parts}
    if len(roots) != 1:
        return ()
    root = roots.pop()
    inside = [item for item in parts if item[0] == root]
    if any(len(item) < 2 for item in inside):
        return ()
    if not any(len(item) == 2 and item[1] == "manifest.json" for item in inside):
        return ()
    return (root,)


# ── Manifests ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ManifestInfo:
    name: str
    description: str | None
    version: str
    manifest_version: int
    icon: str | None


def read_manifest(folder: Path) -> dict[str, Any]:
    """Load and validate a version folder's manifest.json."""
    path = folder / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExtensionError("The archive has no manifest.json") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ExtensionError("manifest.json could not be read") from exc
    except json.JSONDecodeError as exc:
        raise ExtensionError("manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ExtensionError("manifest.json must contain a JSON object")
    if manifest.get("manifest_version") not in (2, 3):
        raise ExtensionError("manifest.json must declare manifest_version 2 or 3")
    for field in ("name", "version"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise ExtensionError(f"manifest.json is missing a {field}")
    _check_version(manifest["version"].strip())
    return manifest


def _locale_messages(folder: Path, default_locale: str | None) -> dict[str, str]:
    """Messages of the manifest's default locale, keyed case-insensitively (Chrome's
    message keys are)."""
    if not default_locale:
        return {}
    locales = folder / "_locales"
    candidate = locales / default_locale
    if not candidate.is_dir() and locales.is_dir():
        wanted = default_locale.casefold()
        candidate = next(
            (entry for entry in locales.iterdir() if entry.name.casefold() == wanted),
            candidate,
        )
    path = candidate / "messages.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).casefold(): str(value["message"])
        for key, value in raw.items()
        if isinstance(value, dict) and isinstance(value.get("message"), str)
    }


def _localise(value: str, messages: dict[str, str]) -> str:
    """Resolve ``__MSG_name__`` placeholders, leaving unknown ones alone (as Chrome does)."""
    if not messages or "__MSG_" not in value.upper():
        return value

    def replace(match: re.Match[str]) -> str:
        return messages.get(match.group(1).casefold(), match.group(0))

    return _MSG_RE.sub(replace, value)


def _icon_relpath(folder: Path, manifest: dict[str, Any]) -> str | None:
    """Relative path of the largest icon no larger than 128px (Chrome's toolbar size)."""
    candidates: list[tuple[int, str]] = []
    icons = manifest.get("icons")
    if isinstance(icons, dict):
        for size, relative in icons.items():
            try:
                pixels = int(size)
            except (TypeError, ValueError):
                continue
            if isinstance(relative, str) and relative.strip():
                candidates.append((pixels, relative.strip()))
    if not candidates:
        for key in ("action", "browser_action"):
            action = manifest.get(key)
            default_icon = action.get("default_icon") if isinstance(action, dict) else None
            if isinstance(default_icon, str) and default_icon.strip():
                candidates.append((0, default_icon.strip()))
            elif isinstance(default_icon, dict):
                for size, relative in default_icon.items():
                    try:
                        pixels = int(size)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(relative, str) and relative.strip():
                        candidates.append((pixels, relative.strip()))
    if not candidates:
        return None
    within = [item for item in candidates if item[0] <= 128]
    _, relative = max(within, key=lambda item: item[0]) if within else min(candidates)
    parts = _member_parts(relative)
    if parts is None:
        return None
    if not folder.joinpath(*parts).is_file():
        return None
    return PurePosixPath(*parts).as_posix()


def read_manifest_info(folder: Path) -> ManifestInfo:
    manifest = read_manifest(folder)
    default_locale = manifest.get("default_locale")
    messages = _locale_messages(
        folder, default_locale if isinstance(default_locale, str) else None
    )
    description = manifest.get("description")
    return ManifestInfo(
        name=_localise(manifest["name"].strip(), messages) or "Unnamed extension",
        description=(
            _localise(description.strip(), messages)
            if isinstance(description, str) and description.strip()
            else None
        ),
        version=manifest["version"].strip(),
        manifest_version=int(manifest["manifest_version"]),
        icon=_icon_relpath(folder, manifest),
    )


def inject_key(folder: Path, public_key: bytes) -> None:
    """Write the entry's public key into the manifest so the unpacked ID never changes.

    Without a key Chromium derives the ID from the folder path, so every new version
    folder would present itself as a different extension and lose its storage.
    """
    path = folder / "manifest.json"
    manifest = read_manifest(folder)
    manifest["key"] = encode_public_key(public_key)
    temporary = folder / ".manifest.json.tmp"
    temporary.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


# ── Sources ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArchiveSource:
    """Where an archive came from. ``url`` is refined to ``crx``/``zip`` when it is
    unpacked; ``webstore`` and ``upload`` are recorded as given."""

    kind: str
    source_url: str | None = None
    webstore_id: str | None = None


def webstore_url(extension_id: str) -> str:
    return CWS_DOWNLOAD_URL.format(version=CWS_PROD_VERSION, extension_id=extension_id)


def webstore_id_from_url(parsed) -> str | None:
    host = (parsed.netloc or "").lower().split(":")[0]
    if host not in _WEBSTORE_HOSTS:
        return None
    if host == "chrome.google.com" and not parsed.path.startswith("/webstore"):
        return None
    candidates = [part for part in parsed.path.split("/") if part]
    candidates += [value for key, value in parse_qsl(parsed.query) if key == "id"]
    for candidate in reversed(candidates):
        if _CHROME_ID_RE.fullmatch(candidate):
            return candidate
    return None


def parse_source(source: str) -> ArchiveSource:
    """Interpret what the user pasted: an ID, a Web Store link, or a CRX/ZIP URL."""
    text = (source or "").strip()
    if not text:
        raise ExtensionError(
            "Enter a Chrome Web Store link, an extension ID, or a CRX/ZIP URL"
        )
    if _CHROME_ID_RE.fullmatch(text):
        return ArchiveSource("webstore", webstore_url(text), text)
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ExtensionError(
            "Enter a Chrome Web Store link, an extension ID, or an http(s) CRX/ZIP URL"
        )
    if (parsed.netloc or "").lower().split(":")[0] in _WEBSTORE_HOSTS:
        webstore_id = webstore_id_from_url(parsed)
        if webstore_id is None:
            raise ExtensionError(
                "That Chrome Web Store link does not contain a 32-character extension ID"
            )
        return ArchiveSource("webstore", webstore_url(webstore_id), webstore_id)
    return ArchiveSource("url", text, None)


def stored_source(row: dict[str, Any]) -> str:
    """The value to re-download for ``row``; an upload has none to repeat."""
    webstore_id = row.get("webstore_id")
    if webstore_id:
        return webstore_id
    source_url = row.get("source_url")
    if source_url:
        return source_url
    raise ExtensionError("This extension was uploaded; paste a URL or upload a new CRX/ZIP file")


def download_archive(source: ArchiveSource) -> Path:
    """Fetch a CRX/ZIP into a temp file inside the library. Blocking: call in a thread."""
    url = source.source_url
    if not url:
        raise ExtensionError("There is no URL to download")
    destination = staging_path()
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120.0)) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 204:
                    if source.kind == "webstore":
                        raise ExtensionError(DELISTED_MESSAGE)
                    raise ExtensionError("The URL returned an empty response")
                if response.status_code >= 400:
                    raise ExtensionDownloadError(
                        f"The extension URL returned HTTP {response.status_code}"
                    )
                total = 0
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            raise ExtensionError(
                                f"The download exceeds {MAX_ARCHIVE_BYTES} bytes"
                            )
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
        if destination.stat().st_size == 0:
            raise ExtensionError("The URL returned an empty file")
        return destination
    except ExtensionError:
        destination.unlink(missing_ok=True)
        raise
    except (httpx.HTTPError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise ExtensionDownloadError(f"Could not download the extension: {exc}") from exc


async def save_upload(chunks: AsyncIterable[bytes]) -> Path:
    """Stream a request body into a temp file inside the library, capped as it arrives."""
    destination = staging_path()
    total = 0
    try:
        with destination.open("wb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ExtensionError(f"The upload exceeds {MAX_ARCHIVE_BYTES} bytes")
                await asyncio.to_thread(handle.write, chunk)
            await asyncio.to_thread(handle.flush)
            await asyncio.to_thread(os.fsync, handle.fileno())
        if total == 0:
            raise ExtensionError("The uploaded file is empty")
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


# ── Install / update / rollback ──────────────────────────────────────────────


def install(
    archive: Path,
    source: ArchiveSource,
    *,
    extension_id: str | None = None,
    in_use: Iterable[str] = (),
) -> tuple[dict[str, Any], bool]:
    """Unpack ``archive`` into the library and record it.

    ``extension_id`` marks an update of that entry: the stored public key is injected
    into the new version, and an archive carrying a different extension is refused.
    Returns ``(row, updated)``; ``updated`` is False when the version is unchanged.
    The caller owns ``archive`` and should delete it afterwards.
    """
    kind = sniff_archive(archive)
    source_kind = source.kind if source.kind in ("webstore", "upload") else kind

    with _LOCK:
        stored = db.get_extension(extension_id) if extension_id else None
        if extension_id and stored is None:
            raise ExtensionNotFound("Extension not found")

        root = library_root()
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".unpack-", dir=root))
        try:
            archive_key: bytes | None = None
            if kind == "crx":
                with archive.open("rb") as handle:
                    archive_key = read_crx_header(handle).public_key
            _extract_zip(archive, staging)

            manifest = read_manifest(staging)
            manifest_key = manifest.get("key")
            if isinstance(manifest_key, str) and manifest_key.strip():
                archive_key = decode_public_key(manifest_key)

            if stored is not None:
                if archive_key is not None and (
                    chrome_id_from_public_key(archive_key) != stored["id"]
                ):
                    raise ExtensionError(
                        "This archive is a different extension than the one being updated"
                    )
                public_key = decode_public_key(stored["public_key"])
            else:
                public_key = archive_key or generate_public_key()
            extension_id_ = chrome_id_from_public_key(public_key)

            if stored is None and db.get_extension(extension_id_) is not None:
                raise ExtensionExists(
                    "That extension is already in the library; use Update"
                )

            inject_key(staging, public_key)
            info = read_manifest_info(staging)

            if stored is not None and stored["version"] == info.version:
                return stored, False

            _publish(staging, version_dir(extension_id_, info.version))
            fields = {
                "name": info.name,
                "description": info.description,
                "version": info.version,
                "manifest_version": info.manifest_version,
                "icon": info.icon,
                "public_key": encode_public_key(public_key),
                "source_kind": source_kind,
                "source_url": source.source_url,
                "webstore_id": source.webstore_id,
            }
            if stored is None:
                row = db.create_extension(id=extension_id_, **fields)
            else:
                fields["previous_version"] = stored["version"]
                row = db.update_extension(extension_id_, **fields)
                if row is None:
                    raise ExtensionNotFound("Extension not found")
            prune(extension_id_, row, in_use)
            return row, True
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _publish(staging: Path, target: Path) -> None:
    """Move an unpacked tree into its version folder in one rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Left behind by an interrupted install; the rename needs the name free.
        shutil.rmtree(target, ignore_errors=True)
    os.replace(staging, target)


def import_source(
    source: str,
    *,
    extension_id: str | None = None,
    in_use: Iterable[str] = (),
) -> tuple[dict[str, Any], bool]:
    """Download what ``source`` names and install it. Blocking: call in a thread."""
    parsed = parse_source(source)
    archive = download_archive(parsed)
    try:
        return install(archive, parsed, extension_id=extension_id, in_use=in_use)
    finally:
        archive.unlink(missing_ok=True)


def install_upload(
    archive: Path,
    *,
    extension_id: str | None = None,
    in_use: Iterable[str] = (),
) -> tuple[dict[str, Any], bool]:
    """Install an uploaded archive and always remove its temp file."""
    try:
        return install(
            archive, ArchiveSource("upload"), extension_id=extension_id, in_use=in_use
        )
    finally:
        archive.unlink(missing_ok=True)


def prune(extension_id: str, row: dict[str, Any], in_use: Iterable[str] = ()) -> None:
    """Keep only the current and previous version folders — plus any folder a running
    browser loaded, which is in use until that browser closes."""
    keep = {row["version"]}
    if row.get("previous_version"):
        keep.add(row["previous_version"])
    busy = {str(Path(path).resolve()) for path in in_use}
    directory = extension_dir(extension_id)
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.name in keep:
            continue
        if str(entry.resolve()) in busy:
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def reclaim(in_use: Iterable[str] = ()) -> int:
    """Repair pass: drop half-written staging entries and version folders no row keeps.

    A crash mid-install leaves a temp tree the library never published; an interrupted
    update can leave a third version folder behind. Both are removed here, at startup,
    when no browser can be holding one. Returns how many paths were removed.
    """
    root = library_root()
    if not root.is_dir():
        return 0
    removed = 0
    for entry in list(root.iterdir()):
        if entry.name.startswith((".incoming-", ".unpack-")):
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
            continue
        if not entry.is_dir() or not _CHROME_ID_RE.fullmatch(entry.name):
            # Only folders named by a Chrome ID can be library entries; leave anything
            # else (a user's own folder put here by hand) alone.
            continue
        row = db.get_extension(entry.name)
        if row is None:
            # No row names this folder: nothing can ever load it.
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            continue
        before = set(entry.iterdir())
        prune(entry.name, row, in_use)
        removed += len(before - set(entry.iterdir()))
    return removed


def rollback(extension_id: str, *, in_use: Iterable[str] = ()) -> dict[str, Any]:
    """Swap ``version`` and ``previous_version``. Blocking: call in a thread."""
    with _LOCK:
        row = db.get_extension(extension_id)
        if row is None:
            raise ExtensionNotFound("Extension not found")
        previous = row.get("previous_version")
        if not previous:
            raise ExtensionError("There is no previous version to roll back to")
        folder = version_dir(extension_id, previous)
        if not folder.is_dir():
            raise ExtensionError("The previous version's files are no longer available")
        info = read_manifest_info(folder)
        updated = db.update_extension(
            extension_id,
            version=previous,
            previous_version=row["version"],
            name=info.name,
            description=info.description,
            manifest_version=info.manifest_version,
            icon=info.icon,
        )
        if updated is None:
            raise ExtensionNotFound("Extension not found")
        prune(extension_id, updated, in_use)
        return updated


def delete(extension_id: str) -> None:
    """Drop an entry and its files. Refused while a profile still attaches it."""
    with _LOCK:
        row = db.get_extension(extension_id)
        if row is None:
            raise ExtensionNotFound("Extension not found")
        names = db.extension_usage().get(extension_id) or []
        if names:
            raise ExtensionInUse(
                "Used by " + ", ".join(sorted(names)) + "; detach it from those profiles first"
            )
        db.delete_extension(extension_id)
        shutil.rmtree(extension_dir(extension_id), ignore_errors=True)


# ── Profile integration ──────────────────────────────────────────────────────


def profile_extension_paths(profile: dict[str, Any]) -> list[str]:
    """Version folders to load for a profile's library extensions, in its own order."""
    paths: list[str] = []
    for extension_id in profile.get("extension_ids") or []:
        row = db.get_extension(extension_id)
        if row is None:
            logger.warning(
                "Profile %s references unknown extension %s", profile.get("id"), extension_id
            )
            continue
        folder = version_dir(row["id"], row["version"])
        if not folder.is_dir():
            logger.warning(
                "Extension %s has no folder for version %s", extension_id, row["version"]
            )
            continue
        paths.append(str(folder))
    return paths


def icon_path(row: dict[str, Any]) -> Path | None:
    """The entry's icon inside its current version folder, if it still exists."""
    relative = row.get("icon")
    if not relative:
        return None
    folder = version_dir(row["id"], row["version"])
    path = folder / relative
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None
