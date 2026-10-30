"""Shared pool of CloakBrowser license keys, leased one per running profile.

The Manager used to hold exactly one app-wide key, so every profile shared a
single seat pool: a free key allows one concurrent session, and a second launch
was denied by the server. This module backs a pool instead — N keys, each
leased exclusively to one running profile — so N profiles run concurrently
without two of them fighting over the same free-tier seat.

Keys live in ``<data_dir>/keys.json``. The raw key never leaves the process
through the API: entries are addressed by a short hash, and listed masked.
The pool is deliberately *not* the authority on entitlement — the binary still
enforces seats. The pool only decides which key a launch is handed, so an
already-leased key is never handed out twice.

States are advisory and only ever *remove* a key from rotation:

    unknown      added, never validated
    valid        the license server accepted it
    invalid      the server rejected it (or the binary exited 77 for it)
    unreachable  validation could not reach the server; still usable
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

from . import database as db

# Only "invalid" removes a key from rotation. An unreachable server is a
# transient condition, and treating it as dead would strand a healthy key.
STATES = ("unknown", "valid", "invalid", "unreachable")

# Every mutation is a read-modify-write of one JSON file, and callers reach this
# module from request handlers *and* worker threads (validation is blocking).
# Re-entrant because validate() calls set_state() per key.
_LOCK = threading.RLock()
_UNUSABLE = frozenset({"invalid"})


def pool_path() -> Path:
    """Resolved at call time so tests (and a moved data dir) are honoured."""
    return db.DATA_DIR / "keys.json"


def key_id(key: str) -> str:
    """Stable public handle for a key — never the key itself."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def mask_key(key: str | None) -> str | None:
    """Show enough of a key to recognise it, never the whole thing."""
    if not key:
        return None
    if len(key) <= 9:
        return "…"
    return f"{key[:5]}…{key[-4:]}"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _normalize_entry(raw: object) -> dict | None:
    """Coerce one stored row into a known shape, or drop it."""
    if not isinstance(raw, dict):
        return None
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        return None
    key = key.strip()
    state = raw.get("state")
    return {
        "id": key_id(key),
        "key": key,
        "label": raw.get("label") if isinstance(raw.get("label"), str) else None,
        "added_at": raw.get("added_at") if isinstance(raw.get("added_at"), str) else _now(),
        "state": state if state in STATES else "unknown",
        "plan": raw.get("plan") if isinstance(raw.get("plan"), str) else None,
        "expires": raw.get("expires") if isinstance(raw.get("expires"), str) else None,
        "checked_at": raw.get("checked_at") if isinstance(raw.get("checked_at"), str) else None,
    }


def load() -> dict:
    """Return ``{"keys": [...]}``, tolerating a missing or corrupt file."""
    try:
        data = json.loads(pool_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"keys": []}
    rows = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {"keys": []}
    seen: set[str] = set()
    keys: list[dict] = []
    for raw in rows:
        entry = _normalize_entry(raw)
        if entry is None or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        keys.append(entry)
    return {"keys": keys}


def save(data: dict) -> None:
    """Atomically persist the pool (temp file + os.replace)."""
    path = pool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def entries() -> list[dict]:
    return load()["keys"]


def add(text: str, label: str | None = None) -> dict[str, int]:
    """Add one key per line. Blank lines and ``#`` comments are ignored.

    Returns counts so the UI can say what happened instead of guessing.
    """
    with _LOCK:
        data = load()
        existing = {entry["id"] for entry in data["keys"]}
        added = duplicates = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            entry_id = key_id(line)
            if entry_id in existing:
                duplicates += 1
                continue
            existing.add(entry_id)
            data["keys"].append(
                {
                    "id": entry_id,
                    "key": line,
                    "label": label,
                    "added_at": _now(),
                    "state": "unknown",
                    "plan": None,
                    "expires": None,
                    "checked_at": None,
                }
            )
            added += 1
        if added:
            save(data)
    return {"added": added, "duplicates": duplicates}


def remove(entry_id: str) -> bool:
    """Drop one key from the pool. False if it was not there."""
    with _LOCK:
        data = load()
        kept = [entry for entry in data["keys"] if entry["id"] != entry_id]
        if len(kept) == len(data["keys"]):
            return False
        save({"keys": kept})
    return True


def find(key: str) -> dict | None:
    wanted = key_id(key)
    for entry in entries():
        if entry["id"] == wanted:
            return entry
    return None


def usable_keys() -> list[str]:
    """Keys a launch may lease, in insertion order. Invalid ones are skipped."""
    return [entry["key"] for entry in entries() if entry["state"] not in _UNUSABLE]


def set_state(
    key: str,
    state: str,
    *,
    plan: str | None = None,
    expires: str | None = None,
) -> bool:
    """Record a validation outcome for a key already in the pool.

    A key that is not pooled is ignored: the pool is not a place to accumulate
    keys the user never added.
    """
    return _set_state(key_id(key), state, plan=plan, expires=expires)


def set_state_by_id(entry_id: str, state: str) -> bool:
    """Record an outcome for a key addressed by id — used when the caller holds
    the id (a lease) rather than the key itself."""
    return _set_state(entry_id, state)


def _set_state(
    entry_id: str,
    state: str,
    *,
    plan: str | None = None,
    expires: str | None = None,
) -> bool:
    if state not in STATES:
        raise ValueError(f"unknown key state: {state!r}")
    with _LOCK:
        data = load()
        changed = False
        for entry in data["keys"]:
            if entry["id"] != entry_id:
                continue
            entry["state"] = state
            entry["checked_at"] = _now()
            if plan is not None:
                entry["plan"] = plan
            if expires is not None:
                entry["expires"] = expires
            changed = True
            break
        if changed:
            save(data)
    return changed


def validate(keys: list[str] | None = None, retries: int = 1) -> list[dict]:
    """Validate pooled keys against the license server and record the outcome.

    Blocking (network) — call it from a worker thread. ``keys`` limits the run
    to a subset; by default every pooled key is checked. A transient failure is
    retried once, then recorded as ``unreachable`` so a flaky network never
    marks a healthy key dead.
    """
    import time

    from cloakbrowser.license import validate_license

    targets = keys if keys is not None else [entry["key"] for entry in entries()]
    results: list[dict] = []
    for key in targets:
        info = None
        for attempt in range(retries + 1):
            try:
                info = validate_license(key)
            except Exception:  # noqa: BLE001 — a raise here means "no answer"
                info = None
            if info is not None:
                break
            if attempt < retries:
                time.sleep(1.0)
        if info is None:
            state, plan, expires = "unreachable", None, None
        elif info.valid:
            state, plan, expires = "valid", info.plan, info.expires
        else:
            state, plan, expires = "invalid", info.plan, info.expires
        set_state(key, state, plan=plan, expires=expires)
        results.append(
            {
                "id": key_id(key),
                "key_masked": mask_key(key),
                "state": state,
                "plan": plan,
                "expires": expires,
            }
        )
    return results
