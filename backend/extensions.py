"""Discovers unpacked Chrome extensions the operator drops on the host.

Scanned ONCE, the first time list_available_extensions() is called (which in
practice is at backend startup — main.py's lifespan calls it directly), and
cached for the rest of the process's life. Deliberately not re-scanned on
every request or watched for changes:

  1. Predictability. A profile's enabled-extensions checkboxes reference
     extension ids by directory name. If the list changed mid-session, a
     profile already running could have an extension vanish out from under a
     checkbox the user is looking at, or a newly-added one could silently
     start applying to profiles that never opted in.
  2. Cost. Reading every extension's manifest.json (and resolving any
     __MSG_..__ localized name/description through _locales/) on every
     GET /api/extensions or every launch is needless I/O for something that,
     by design, never changes without a container restart anyway.

Adding, removing, or editing an extension under EXTENSIONS_DIR requires a
`docker compose restart` (or recreate) to be picked up automatically — OR an
operator can force it via rescan_extensions() (POST /api/extensions/rescan,
a "Rescan" button in the UI). That stays consistent with the reasoning
above: a human explicitly asking for the list to change right now is not
the same problem as it changing silently out from under them mid-session.
See the volumes: comment in docker-compose.yml.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import struct
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("cloakbrowser.manager.extensions")

EXTENSIONS_DIR = Path("/data/extensions")

# Real extensions are a few MB at most; this is a generous ceiling against a
# mistaken or malicious upload, not a real-world size.
MAX_EXTENSION_UPLOAD_BYTES = 50 * 1024 * 1024

_cache: list[dict[str, Any]] | None = None


def _resolve_message(value: str, messages: dict[str, Any]) -> str:
    """Resolve a manifest string that's a `__MSG_key__` placeholder via a
    _locales/<default_locale>/messages.json dict. Returns `value` unchanged
    if it isn't a placeholder, or if the key isn't found (better a raw
    __MSG_..__ string in the UI than a crash on a malformed extension).
    """
    if not (value.startswith("__MSG_") and value.endswith("__")):
        return value
    key = value[len("__MSG_"):-len("__")]
    entry = messages.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("message"), str):
        return entry["message"]
    return value


def _load_messages(ext_dir: Path, default_locale: str | None) -> dict[str, Any]:
    if not default_locale:
        return {}
    messages_path = ext_dir / "_locales" / default_locale / "messages.json"
    try:
        return json.loads(messages_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _read_extension(ext_dir: Path) -> dict[str, Any] | None:
    """Read one extension's manifest.json into a listing entry, or None if
    ext_dir isn't a usable unpacked extension (no readable manifest.json —
    Chromium's own requirement for --load-extension)."""
    manifest_path = ext_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping %s: unreadable manifest.json (%s)", ext_dir, exc)
        return None

    messages = _load_messages(ext_dir, manifest.get("default_locale"))
    name = _resolve_message(manifest.get("name", ext_dir.name), messages)
    description = manifest.get("description")
    if isinstance(description, str):
        description = _resolve_message(description, messages)

    return {
        "id": ext_dir.name,
        "name": name,
        "description": description,
        "version": manifest.get("version"),
        "path": str(ext_dir.resolve()),
    }


def _scan() -> list[dict[str, Any]]:
    if not EXTENSIONS_DIR.is_dir():
        return []
    found = []
    for entry in sorted(EXTENSIONS_DIR.iterdir()):
        # Leading-dot directories (.gitkeep-style placeholders, editor swap
        # dirs) are conventionally not content, so skip them rather than log
        # a warning about every one missing a manifest.json.
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        parsed = _read_extension(entry)
        if parsed is not None:
            found.append(parsed)
    logger.info("Discovered %d extension(s) under %s", len(found), EXTENSIONS_DIR)
    return found


def list_available_extensions() -> list[dict[str, Any]]:
    """All usable extensions under EXTENSIONS_DIR, scanned once and cached."""
    global _cache
    if _cache is None:
        _cache = _scan()
    return _cache


def rescan_extensions() -> list[dict[str, Any]]:
    """Force a fresh scan, replacing the cached list.

    Manual and explicit only — an operator-triggered escape hatch (a UI
    "Rescan" button, or a fresh upload that needs to show up immediately),
    not a background watcher. The module docstring's case against
    auto-refreshing still holds; a human asking for it right now doesn't
    hit it, since nothing changes without them choosing that exact moment.
    """
    global _cache
    _cache = _scan()
    return _cache


def extension_paths_for(enabled_ids: list[str]) -> list[str]:
    """Filesystem paths for the subset of `enabled_ids` that are still
    available. Silently drops ids that no longer exist (a since-removed
    extension, or the container hasn't been restarted since one was added) —
    the whole point of the once-per-start cache is that a stale reference
    here is expected, not an error to surface at launch time."""
    available = {e["id"]: e["path"] for e in list_available_extensions()}
    return [available[eid] for eid in enabled_ids if eid in available]


# ── Installing an extension (upload, or fetched from the Chrome Web Store) ───

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Chrome extension ids are always exactly 32 lowercase letters a-p — each
# byte of a SHA-256 digest nibble-mapped into that alphabet (see
# docker/fetch-widevine.py's _crx_appid for the same encoding used the other
# direction). True whether the id is typed in bare or embedded in a
# chromewebstore.google.com/detail/<name>/<id> URL.
_EXTENSION_ID_RE = re.compile(r"[a-p]{32}")


def _slugify(text: str) -> str:
    """A safe, readable directory name from an extension's manifest name.

    Not fed through _resolve_message first — a __MSG_..__ placeholder just
    slugifies into something uglier (e.g. "msg-extname"), not something
    wrong: this is an internal directory NAME, not the display name shown
    in the UI, which _read_extension() resolves correctly once this
    directory has its own _locales/ on disk to resolve against.
    """
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "extension"


def _dedupe_extension_dir_name(base: str) -> str:
    """"my-ext" -> "my-ext-2" -> "my-ext-3" ... so installing the same
    extension twice (or two different ones that slugify the same) doesn't
    silently overwrite one with the other."""
    if not (EXTENSIONS_DIR / base).exists():
        return base
    n = 2
    while (EXTENSIONS_DIR / f"{base}-{n}").exists():
        n += 1
    return f"{base}-{n}"


def _is_crx(data: bytes) -> bool:
    return data[:4] == b"Cr24"


def _strip_crx_header(data: bytes) -> bytes:
    """A CRX (2 or 3) is a small header wrapped around a plain zip — strip it
    and return the zip payload. Header length is a little-endian uint32 at
    bytes[8:12]; same technique README.md's manual fetch instructions and
    docker/fetch-widevine.py's extraction already use."""
    header_len = struct.unpack("<I", data[8:12])[0]
    return data[12 + header_len:]


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """Refuse to extract anything (a "../../etc/passwd"-style member, or an
    absolute path) that would land outside `dest` — zipfile.extractall()
    does not check this itself."""
    dest_resolved = dest.resolve()
    for member in zf.namelist():
        member_path = (dest / member).resolve()
        if member_path != dest_resolved and dest_resolved not in member_path.parents:
            raise ValueError(f"Refusing to extract unsafe path in archive: {member!r}")
    zf.extractall(dest)


def install_extension_from_bytes(data: bytes, suggested_name: str) -> dict[str, Any]:
    """Install an uploaded or downloaded extension archive (.zip or .crx)
    under EXTENSIONS_DIR and rescan so it's immediately visible.

    Synchronous and pure I/O — callers touching a network or an ASGI request
    body run this in an executor. Raises ValueError (never a raw exception
    from zipfile/json/struct) for anything that isn't a usable unpacked
    extension: too big, not a zip/crx, no manifest.json, unparseable
    manifest.json, or an archive that tries to write outside its own
    destination directory.
    """
    if len(data) > MAX_EXTENSION_UPLOAD_BYTES:
        raise ValueError(
            f"Extension archive is {len(data) / 1_048_576:.1f}MB, over the "
            f"{MAX_EXTENSION_UPLOAD_BYTES // 1_048_576}MB limit"
        )
    if _is_crx(data):
        data = _strip_crx_header(data)

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Not a valid .zip or .crx archive: {exc}") from exc

    if "manifest.json" not in zf.namelist():
        raise ValueError("Archive has no manifest.json at its root")
    try:
        manifest = json.loads(zf.read("manifest.json"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"manifest.json is not valid JSON: {exc}") from exc

    base_name = _slugify(manifest.get("name") or Path(suggested_name).stem)
    dir_name = _dedupe_extension_dir_name(base_name)
    dest = EXTENSIONS_DIR / dir_name

    EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
    # Extract into a temp dir first and rename into place: a failure partway
    # through extraction must not leave a half-written directory that the
    # next scan picks up as a real (but broken) extension.
    tmp_dir = EXTENSIONS_DIR / f".upload-{dir_name}-tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    try:
        _safe_extract_zip(zf, tmp_dir)
        tmp_dir.rename(dest)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    rescan_extensions()
    installed = next((e for e in list_available_extensions() if e["id"] == dir_name), None)
    if installed is None:
        # Extracted fine but _read_extension() rejected it on rescan (e.g. a
        # manifest.json readable here but not valid UTF-8 on disk somehow).
        shutil.rmtree(dest, ignore_errors=True)
        rescan_extensions()
        raise ValueError("Extracted extension has no readable manifest.json after install")
    logger.info("Installed extension %r -> %s", dir_name, dest)
    return installed


def extract_extension_id(url_or_id: str) -> str | None:
    """Pull a 32-character Chrome extension id out of a Chrome Web Store URL,
    or accept a bare id directly — same shape either way, see _EXTENSION_ID_RE.
    """
    match = _EXTENSION_ID_RE.search(url_or_id.strip().lower())
    return match.group(0) if match else None


def chrome_web_store_crx_url(extension_id: str) -> str:
    """Google's own component-update download URL for an extension id — the
    same one Chrome itself uses, and the one README.md's manual `curl`
    instructions already documented before this existed."""
    return (
        "https://clients2.google.com/service/update2/crx"
        f"?response=redirect&prodversion=120.0.0.0&acceptformat=crx2,crx3&x=id%3D{extension_id}%26uc"
    )
