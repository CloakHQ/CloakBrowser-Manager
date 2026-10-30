r"""Bulk-validate CloakBrowser license keys before handing them to the Manager.

Standalone utility kept outside the Manager's own code path: it only reads the
key file and calls ``cloakbrowser.license``. It never writes to the Manager's
database or settings.

Usage (from the repository root, with the Manager venv):

    .venv\Scripts\python.exe tools\check_keys.py keys.txt
    .venv\Scripts\python.exe tools\check_keys.py keys.txt --seats

``keys.txt`` holds one license key per line; blank lines and ``#`` comments are
ignored. Exit code is 0 only when every key validated.

Use it to weed out dead keys before pasting a batch into Settings → Key pool,
which also validates on add but reports one row at a time. This tool only reads
and checks: it never writes the pool file or the Manager database.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

VALID = "VALID"
INVALID = "INVALID"
UNREACHABLE = "UNREACHABLE"


def mask(key: str) -> str:
    if len(key) <= 14:
        return key
    return f"{key[:6]}...{key[-4:]}"


def read_keys(path: Path) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            print(f"  (skipping duplicate key: {mask(line)})")
            continue
        seen.add(line)
        keys.append(line)
    return keys


def check_key(key: str, retries: int = 2) -> tuple[str, str, str]:
    """Return (status, plan, expires). Retries a transient network failure."""
    from cloakbrowser.license import validate_license

    info = None
    for attempt in range(retries + 1):
        info = validate_license(key)
        if info is not None:
            break
        if attempt < retries:
            time.sleep(2.0)
    if info is None:
        return UNREACHABLE, "-", "-"
    if not info.valid:
        return INVALID, info.plan, info.expires or "-"
    return VALID, info.plan, info.expires or "-"


def main() -> int:
    # The legacy Windows console is cp1252/strict; never let a cosmetic line
    # abort the check.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(description="Check CloakBrowser license keys.")
    parser.add_argument("key_file", type=Path)
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Seconds to wait between server calls (default 1.0).",
    )
    parser.add_argument(
        "--seats", action="store_true",
        help="Also query seats in use / seat limit for each key.",
    )
    args = parser.parse_args()

    if not args.key_file.exists():
        print(f"File not found: {args.key_file}", file=sys.stderr)
        return 2

    keys = read_keys(args.key_file)
    if not keys:
        print("No keys in file.", file=sys.stderr)
        return 2

    print(f"Checking {len(keys)} key(s)...\n")
    counts = {VALID: 0, INVALID: 0, UNREACHABLE: 0}
    for index, key in enumerate(keys):
        status, plan, expires = check_key(key)
        counts[status] += 1

        seats = ""
        if args.seats and status == VALID:
            from cloakbrowser.license import get_session_seats

            found = get_session_seats(key)
            seats = f" seats={found.active}/{found.limit} ({found.state})"

        print(f"{mask(key):<22} {status:<12} plan={plan:<8} expires={expires}{seats}")
        if args.delay and index < len(keys) - 1:
            time.sleep(args.delay)

    print(f"\nTotal: {counts[VALID]} valid, {counts[INVALID]} invalid, "
          f"{counts[UNREACHABLE]} unreachable.")
    return 0 if counts[INVALID] == 0 and counts[UNREACHABLE] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
