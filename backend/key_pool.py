"""Shared pool of CloakBrowser license keys, leased one per running profile.

The Manager used to hold exactly one app-wide key, so every profile shared a
single seat pool: a free key allows one concurrent session, and a second launch
was denied by the server. This module backs a pool instead — N keys, each
leased exclusively to one running profile — so N profiles run concurrently
without two of them fighting over the same free-tier seat.

Keys live in ``<data_dir>/keys.json``, alongside the leases the running profiles
hold: ``{"keys": [...], "leases": {profile_id: {...}}}``. Both live on disk
because a Manager restart must not re-hand a key the license server is still
counting a session for. The raw key never leaves the process through the API:
entries are addressed by a short hash, and listed masked.

The pool is deliberately *not* the authority on entitlement — the binary still
enforces seats. Local "leased" is not the same thing as a server-side seat: the
server only reclaims the seat of an abandoned session after a plan-specific
grace window (four minutes on free). ``cooldown_until`` is what covers that gap,
and the launch path asks the server before re-issuing a cooling key.

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

# How long the license server keeps counting a session after the browser is gone,
# per plan. A key that dies must sit out its plan's window before the pool hands
# it to anyone: re-issuing it sooner is exactly the exit-76 denial. Mirrors the
# wrapper's own numbers.
SEAT_GRACE_BY_PLAN = {
    "free": 240,
    "trial": 240,
    "solo": 240,
    "team": 900,
    "business": 1800,
    "scale": 1800,
}
SEAT_GRACE_DEFAULT = 240  # the free-tier reclaim window; safe when the plan is unknown

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
        # ISO UTC. Until this passes the server may still count the key's last
        # session, so the key must not be handed out.
        "cooldown_until": raw.get("cooldown_until")
        if isinstance(raw.get("cooldown_until"), str)
        else None,
    }


def _normalize_lease(raw: object) -> dict | None:
    """Coerce one stored lease into a known shape, or drop it."""
    if not isinstance(raw, dict):
        return None
    key_id_value = raw.get("key_id")
    if not isinstance(key_id_value, str) or not key_id_value:
        return None
    pid = raw.get("pid")
    return {
        "key_id": key_id_value,
        "pid": pid if isinstance(pid, int) else 0,
        "until": raw.get("until") if isinstance(raw.get("until"), str) else None,
    }


def _parse(stamp: str | None) -> datetime.datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def load() -> dict:
    """Return ``{"keys": [...], "leases": {...}}``, tolerating a corrupt file.

    ``keys`` is the shape every older caller expects; ``leases`` is extra and
    simply empty when it is missing or malformed.
    """
    try:
        data = json.loads(pool_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"keys": [], "leases": {}}
    if not isinstance(data, dict):
        return {"keys": [], "leases": {}}
    rows = data.get("keys")
    if not isinstance(rows, list):
        rows = []
    seen: set[str] = set()
    keys: list[dict] = []
    for raw in rows:
        entry = _normalize_entry(raw)
        if entry is None or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        keys.append(entry)
    leases: dict[str, dict] = {}
    raw_leases = data.get("leases")
    if isinstance(raw_leases, dict):
        for profile_id, raw in raw_leases.items():
            lease = _normalize_lease(raw)
            if lease is not None and isinstance(profile_id, str):
                leases[profile_id] = lease
    return {"keys": keys, "leases": leases}


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
                    "cooldown_until": None,
                }
            )
            added += 1
        if added:
            save(data)
    return {"added": added, "duplicates": duplicates}


def remove(entry_id: str) -> bool:
    """Drop one key from the pool. False if it was not there.

    Leases pointing at the removed key go with it, so a stale lease can never
    resurrect the key for the next launch.
    """
    with _LOCK:
        data = load()
        kept = [entry for entry in data["keys"] if entry["id"] != entry_id]
        if len(kept) == len(data["keys"]):
            return False
        data["keys"] = kept
        data["leases"] = {
            profile_id: lease
            for profile_id, lease in data["leases"].items()
            if lease["key_id"] != entry_id
        }
        save(data)
    return True


def find(key: str) -> dict | None:
    wanted = key_id(key)
    for entry in entries():
        if entry["id"] == wanted:
            return entry
    return None


def find_by_id(entry_id: str) -> dict | None:
    """Entry by id — lets a caller read ``state``/``plan``/``cooldown_until``
    from a lease without ever touching the raw key."""
    for entry in entries():
        if entry["id"] == entry_id:
            return entry
    return None


def grace_for(plan: str | None) -> int:
    """Seconds the server keeps counting a seat for ``plan`` after the browser died."""
    return SEAT_GRACE_BY_PLAN.get((plan or "").lower(), SEAT_GRACE_DEFAULT)


def leases() -> dict[str, dict]:
    """``{profile_id: {"key_id", "pid", "until"}}``; unusable rows are dropped."""
    return load()["leases"]


def set_lease(profile_id: str, entry_id: str, *, plan: str | None = None) -> None:
    """Persist that ``profile_id`` holds ``entry_id``.

    ``until`` is the moment the server is expected to have reclaimed the seat —
    diagnostics only; the launch path decides availability from
    ``cooldown_until``.
    """
    until = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=grace_for(plan))
    ).isoformat()
    with _LOCK:
        data = load()
        current = data["leases"].get(profile_id)
        if (
            current is not None
            and current["key_id"] == entry_id
            and current["pid"] == os.getpid()
        ):
            return
        data["leases"][profile_id] = {
            "key_id": entry_id,
            "pid": os.getpid(),
            "until": until,
        }
        save(data)


def clear_lease(profile_id: str, *, cooldown: int = 0) -> str | None:
    """Drop ``profile_id``'s lease, optionally cooling its key for ``cooldown`` s.

    Returns the key id that was held, or ``None`` when the profile held nothing.
    """
    with _LOCK:
        data = load()
        lease = data["leases"].pop(profile_id, None)
        if lease is None:
            return None
        entry_id = lease["key_id"]
        if cooldown > 0:
            _stamp(data, entry_id, cooldown)
        save(data)
    return entry_id


def drop_all_leases() -> list[str]:
    """Forget every lease, returning the key ids they pointed at.

    Startup-only: no process survives a restart, so every stored lease is an
    orphan by definition.
    """
    with _LOCK:
        data = load()
        if not data["leases"]:
            return []
        held = [lease["key_id"] for lease in data["leases"].values()]
        data["leases"] = {}
        save(data)
    return held


def _stamp(data: dict, entry_id: str, seconds: int) -> bool:
    until = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=seconds)
    ).isoformat()
    for entry in data["keys"]:
        if entry["id"] == entry_id:
            entry["cooldown_until"] = until
            return True
    return False


def stamp_cooldown(entry_id: str, seconds: int) -> bool:
    """Hold a key out of rotation for ``seconds``. False if nothing changed."""
    if seconds <= 0:
        return False
    with _LOCK:
        data = load()
        if not _stamp(data, entry_id, seconds):
            return False
        save(data)
    return True


def clear_cooldown(entry_id: str) -> bool:
    """Release a cooling key early (the server has proved the seat free)."""
    with _LOCK:
        data = load()
        for entry in data["keys"]:
            if entry["id"] == entry_id and entry["cooldown_until"] is not None:
                entry["cooldown_until"] = None
                save(data)
                return True
    return False


def cooldown_remaining(entry: dict) -> int:
    """Whole seconds this key is still cooling; ``0`` when it is not."""
    until = _parse(entry.get("cooldown_until"))
    if until is None:
        return 0
    remaining = (until - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    return max(0, int(remaining))


def claimable(entry: dict) -> bool:
    """True when the pool may hand this key to a launch right now."""
    if entry["state"] == "invalid":
        return False
    until = _parse(entry.get("cooldown_until"))
    return until is None or until <= datetime.datetime.now(datetime.timezone.utc)


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
