"""Per-profile file artifacts — files pushed into a profile, and files its browser downloaded.

Artifacts live OUTSIDE the profile's ``user_data_dir``: duplicating or exporting a profile's
browser state must not carry merchant documents with it, and an upload arriving mid-copy must
not mutate a directory that a snapshot is reading.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import AsyncIterable, Callable

from . import database as db


def _env_int(name: str, default: int) -> int:
    """An integer setting; unset or blank means the default (compose renders ``${X:-}`` as "")."""
    return int(os.environ.get(name) or default)


# Caps are enforced as bytes arrive. Content-Length is client-supplied and proves nothing.
MAX_ARTIFACT_BYTES = _env_int("ARTIFACT_MAX_BYTES", 256 * 1024 * 1024)
MAX_ARTIFACTS_PER_PROFILE = _env_int("ARTIFACT_MAX_COUNT", 200)
MAX_PROFILE_ARTIFACT_BYTES = _env_int("ARTIFACT_MAX_TOTAL_BYTES", 2 * 1024 * 1024 * 1024)

_INCOMING_PREFIX = ".incoming-"


class ArtifactTooLarge(Exception):
    """A single artifact exceeded MAX_ARTIFACT_BYTES."""


class ArtifactQuotaExceeded(Exception):
    """The profile's artifact count or total size cap would be exceeded."""


def artifacts_root() -> Path:
    return Path(db.DATA_DIR) / "artifacts"


def artifact_dir(profile_id: str) -> Path:
    return artifacts_root() / profile_id


def artifact_item_dir(profile_id: str, artifact_id: str) -> Path:
    """Each artifact owns a directory named by its id — that id, not the filename, is the
    trust boundary, so a crafted name cannot escape it."""
    return artifact_dir(profile_id) / artifact_id


def artifact_path(profile_id: str, artifact_id: str, name: str) -> Path:
    """The stored file keeps its real (sanitized) name inside the artifact's own directory,
    so a page receiving it by CDP sees the filename and extension a portal expects.

    On Windows the whole path must also fit 260 characters unless the machine has long paths
    enabled, and two UUID segments already spend ~80 of them. Writer and reader both come
    through here, so any extra trimming is applied consistently.
    """
    directory = artifact_item_dir(profile_id, artifact_id)
    stored = safe_filename(name)
    if os.name == "nt":
        budget = _MAX_WINDOWS_PATH - len(str(directory)) - 1
        if budget < len(stored):
            stem, dot, ext = stored.rpartition(".")
            stored = _fit_bytes(stem or stored, f"{dot}{ext}" if dot else "", max(1, budget))
    return directory / stored


# A path component is capped at 255 BYTES on most Linux filesystems, so a legitimate
# non-ASCII name can be well under 255 characters and still be rejected.
_MAX_NAME_BYTES = 255
# Leaves headroom under Windows' 260-character MAX_PATH for the file name itself.
_MAX_WINDOWS_PATH = 250

# Illegal in a Windows path component. Replaced on every platform so a profile directory
# created under Docker still opens on a Windows install of the Manager.
_WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*]')
# Windows reserves the superscript forms too, not just COM1..COM9.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
    *(f"COM{n}" for n in "\u00b9\u00b2\u00b3"),
    *(f"LPT{n}" for n in "\u00b9\u00b2\u00b3"),
}


def _fit_bytes(stem: str, suffix: str, budget: int) -> str:
    """Trim so ``stem + suffix`` fits ``budget`` bytes, keeping as much suffix as it can.

    The suffix is bounded FIRST: an extension can be longer than the whole budget on its own,
    and a caller appending a disambiguating marker needs that marker to survive — otherwise
    the trimmed result equals its input and a rename loop never terminates.
    """
    while suffix and len(suffix.encode("utf-8")) > budget - 1:
        suffix = suffix[:-1]
    room = budget - len(suffix.encode("utf-8"))
    while len(stem.encode("utf-8")) > room:
        stem = stem[:-1]
    return f"{stem}{suffix}" or suffix or stem


def safe_filename(name: str | None, fallback: str = "file") -> str:
    """Sanitize a filename so it is a valid single path component on every supported platform.

    Applies the strictest rules of every platform the Manager runs on, so the same name is
    valid whether the profile directory was written on Linux, macOS or Windows.
    """
    cleaned = unicodedata.normalize("NFC", (name or "").strip()).replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", cleaned)
    cleaned = _WINDOWS_ILLEGAL.sub("_", cleaned)
    # Windows silently drops a trailing dot or space, which would leave two different
    # artifacts fighting over one on-disk name.
    cleaned = cleaned.rstrip(". ")
    if cleaned in ("", ".", ".."):
        return fallback
    stem, dot, ext = cleaned.partition(".")
    if stem.upper() in _WINDOWS_RESERVED:
        # A file called NUL or COM1 cannot be created on Windows at all.
        cleaned = f"_{cleaned}"
    if len(cleaned.encode("utf-8")) <= _MAX_NAME_BYTES:
        return cleaned
    stem, dot, ext = cleaned.rpartition(".")
    fitted = _fit_bytes(stem or cleaned, f"{dot}{ext}" if dot else "", _MAX_NAME_BYTES)
    return fitted or fallback


def check_quota(profile_id: str, incoming: int = 0, *, replacing: str | None = None) -> None:
    """Raise if another artifact of ``incoming`` bytes would breach the profile's caps.

    ``replacing`` names an artifact whose slot is ALREADY held — a pending download being
    published. Its row is excluded from both counts, and the count cap is not re-applied,
    because that slot was claimed when the transfer began.
    """
    rows = [row for row in db.list_artifacts(profile_id) if row["id"] != replacing]
    if replacing is None and len(rows) >= MAX_ARTIFACTS_PER_PROFILE:
        raise ArtifactQuotaExceeded(
            f"profile already holds {len(rows)} artifacts (max {MAX_ARTIFACTS_PER_PROFILE})"
        )
    # Only published artifacts have bytes on disk. A pending row is zero, and a failed one
    # never landed, so counting either would charge a profile for storage it is not using.
    used = sum(int(row["size"] or 0) for row in rows if row["state"] == "ready")
    if used + incoming > MAX_PROFILE_ARTIFACT_BYTES:
        raise ArtifactQuotaExceeded(
            f"profile artifact storage would reach {used + incoming} bytes "
            f"(max {MAX_PROFILE_ARTIFACT_BYTES})"
        )


def _flush_to_disk(handle) -> None:
    handle.flush()
    os.fsync(handle.fileno())


async def save_stream(
    profile_id: str, artifact_id: str, name: str, chunks: AsyncIterable[bytes]
) -> int:
    """Stream a body to a temp file, then publish it atomically under ``artifact_id``.

    Chunks go to disk as they arrive, so the upload costs one copy and nothing waits for the
    whole body. The cap is enforced while receiving, and any failure removes the partial
    file, so a half-written upload is never visible as an artifact.
    """
    directory = artifact_item_dir(profile_id, artifact_id)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{_INCOMING_PREFIX}{artifact_id}"
    total = 0
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_ARTIFACT_BYTES:
                    raise ArtifactTooLarge(
                        f"upload exceeds {MAX_ARTIFACT_BYTES} bytes"
                    )
                # Off the loop: slow container storage must not stall the rest of the Manager.
                await asyncio.to_thread(handle.write, chunk)
            await asyncio.to_thread(_flush_to_disk, handle)
        await asyncio.to_thread(os.replace, tmp, artifact_path(profile_id, artifact_id, name))
        return total
    except BaseException:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(directory, ignore_errors=True)
        raise


# A page's "Choose File" opens the container's own GTK dialog, which starts in an empty $HOME
# and cannot reach an id-keyed path a human would ever type. These two helpers give that dialog
# a flat, real-named folder per profile and a sidebar shortcut to it. Cosmetic: never fatal.
PICKER_VIEW_DIRNAME = "files"
# Mirrors downloads.STAGING_DIRNAME; imported there would be circular.
STAGING_DIRNAME = ".downloads"


def picker_view_enabled() -> bool:
    """The flat view and its chooser bookmark exist ONLY to work around containerisation.

    Under Docker the browser cannot see the operator's filesystem, so an uploaded file needs
    somewhere reachable in the guest's own file dialog. On a native macOS or Windows install
    the browser already shares the user's disk, so the view would be noise — and Windows
    reserves symlink creation to privileged accounts anyway.
    """
    return db.RUNTIME.runtime_mode == "docker"


def _gtk_bookmarks_path() -> Path | None:
    """Elsewhere ``$HOME`` belongs to a real user, and writing there would clobber their
    own GTK bookmarks."""
    if not picker_view_enabled():
        return None
    return Path.home() / ".config" / "gtk-3.0" / "bookmarks"


def picker_dir(profile_id: str) -> Path:
    return artifact_dir(profile_id) / PICKER_VIEW_DIRNAME


def reserve_picker_name(profile_id: str, name: str) -> str:
    """The name this artifact will keep in the file-chooser view, decided once.

    Deriving it at rebuild time would let an existing picker path start resolving to a
    DIFFERENT document once a sibling with the same name is added or removed.
    """
    taken = {row["picker_name"] or row["name"] for row in db.list_artifacts(profile_id)}
    if name not in taken:
        return name
    stem, dot, ext = name.partition(".")
    extension = f"{dot}{ext}" if dot else ""
    # The marker is part of the suffix so it survives truncation of a maximum-length name.
    # Trimming it away instead would regenerate the taken name forever.
    for counter in range(1, len(taken) + 2):
        candidate = _fit_bytes(stem, f" ({counter}){extension}", _MAX_NAME_BYTES)
        if candidate not in taken:
            return candidate
    return _fit_bytes(stem, f" ({uuid.uuid4().hex[:8]}){extension}", _MAX_NAME_BYTES)


def _refresh_gtk_bookmarks() -> None:
    """One sidebar entry per profile that has files, labelled with the profile name."""
    path = _gtk_bookmarks_path()
    if path is None:
        return
    lines = [
        f"file://{picker_dir(profile_id)} Uploads \u2014 {name}"
        for profile_id, name in db.profile_names()
        if picker_dir(profile_id).is_dir()
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines))


def refresh_picker_bookmarks() -> None:
    """Rebuild the chooser sidebar. Call after a profile is renamed or removed."""
    try:
        _refresh_gtk_bookmarks()
    except OSError:
        pass


def sync_picker_view(profile_id: str, *, refresh_bookmarks: bool = True) -> None:
    """Rebuild the profile's flat, real-named view and the file-chooser sidebar shortcut.

    Symlinks, so the bytes are stored once and the canonical id-keyed path stays the only
    place they live.
    """
    if not picker_view_enabled():
        return
    try:
        view = picker_dir(profile_id)
        view.mkdir(parents=True, exist_ok=True)
        wanted: dict[str, Path] = {}
        for row in db.list_artifacts(profile_id):
            target = artifact_path(profile_id, row["id"], row["name"])
            if target.is_file():
                wanted[row["picker_name"] or row["name"]] = target
        for existing in view.iterdir():
            if not existing.is_symlink():
                continue
            target = wanted.get(existing.name)
            if target is None or existing.resolve() != target.resolve():
                existing.unlink()
        for name, target in wanted.items():
            link = view / name
            if not link.is_symlink():
                link.symlink_to(target)
        if refresh_bookmarks:
            _refresh_gtk_bookmarks()
    except OSError:
        # A convenience for the human-facing dialog; an upload must never fail over it.
        pass


def reclaim_unpublished(profile_id: str) -> int:
    """Delete artifact bytes that no row references, and any half-written upload.

    A container restart never runs the endpoint's cleanup, so a kill during or just after
    a write can leave a ``.incoming-*`` file or a complete directory with no row: invisible
    to the list, impossible to delete individually, and outside the profile's quota.
    """
    directory = artifact_dir(profile_id)
    if not directory.is_dir():
        return 0
    known = {row["id"] for row in db.list_artifacts(profile_id)}
    reserved = {PICKER_VIEW_DIRNAME, STAGING_DIRNAME}
    reclaimed = 0
    for entry in directory.iterdir():
        if entry.name in reserved:
            continue
        try:
            if entry.is_dir():
                if entry.name in known and not any(
                    child.name.startswith(_INCOMING_PREFIX) for child in entry.iterdir()
                ):
                    continue
                if entry.name in known:
                    # Published row, but a temp file from an interrupted rewrite remains.
                    for child in entry.iterdir():
                        if child.name.startswith(_INCOMING_PREFIX):
                            child.unlink(missing_ok=True)
                            reclaimed += 1
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                reclaimed += 1
            elif entry.name.startswith(_INCOMING_PREFIX):
                entry.unlink(missing_ok=True)
                reclaimed += 1
        except OSError:
            continue
    return reclaimed


def reclaim_all() -> int:
    """Reclaim unpublished bytes across every profile directory, including ones with no rows."""
    root = artifacts_root()
    if not root.is_dir():
        return 0
    total = 0
    for entry in root.iterdir():
        if entry.is_dir():
            total += reclaim_unpublished(entry.name)
    return total


def delete_file(profile_id: str, artifact_id: str) -> None:
    """Drop the artifact's whole directory — it holds exactly one file."""
    shutil.rmtree(artifact_item_dir(profile_id, artifact_id), ignore_errors=True)


def remove_profile_artifacts(profile_id: str) -> None:
    """Drop a profile's whole artifact directory; artifacts must not outlive their profile."""
    shutil.rmtree(artifact_dir(profile_id), ignore_errors=True)
