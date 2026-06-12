#!/usr/bin/env python3
"""Host-side watchdog for CloakBrowser Manager.

Polls the manager API and re-launches any profile that has auto_launch=true
but is currently stopped. This closes the gap where the built-in auto-launch
only runs during manager startup.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE_URL = "http://127.0.0.1:8080"
POLL_SECONDS = 15
REQUEST_TIMEOUT = 10
AUTO_LAUNCH_ONLY = True


def http_json(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def manager_up() -> bool:
    try:
        data = http_json("GET", "/api/status")
        return isinstance(data, dict) and "profiles_total" in data
    except Exception:
        return False


def loop_forever() -> None:
    print(f"[watchdog] starting base_url={BASE_URL} poll={POLL_SECONDS}s", flush=True)
    consecutive_failures = 0
    while True:
        try:
            if not manager_up():
                consecutive_failures += 1
                print(f"[watchdog] manager not ready (failures={consecutive_failures})", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            profiles = http_json("GET", "/api/profiles") or []
            consecutive_failures = 0
            relaunched = 0
            for profile in profiles:
                profile_id = profile.get("id")
                name = profile.get("name")
                status = profile.get("status")
                auto_launch = bool(profile.get("auto_launch"))

                if AUTO_LAUNCH_ONLY and not auto_launch:
                    continue
                if status == "running":
                    continue

                print(
                    f"[watchdog] relaunching profile name={name} id={profile_id} "
                    f"status={status} auto_launch={auto_launch}",
                    flush=True,
                )
                try:
                    result = http_json("POST", f"/api/profiles/{profile_id}/launch")
                    relaunched += 1
                    print(
                        f"[watchdog] relaunched name={name} id={profile_id} result={result}",
                        flush=True,
                    )
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", errors="replace")
                    print(
                        f"[watchdog] HTTPError relaunching name={name} id={profile_id}: "
                        f"{exc.code} {body}",
                        flush=True,
                    )
                except Exception as exc:
                    print(
                        f"[watchdog] error relaunching name={name} id={profile_id}: {exc}",
                        flush=True,
                    )

            if relaunched == 0:
                print(f"[watchdog] healthy: checked {len(profiles)} profiles; nothing to relaunch", flush=True)
        except Exception as exc:
            consecutive_failures += 1
            print(f"[watchdog] loop error failures={consecutive_failures}: {exc}", flush=True)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        loop_forever()
    except KeyboardInterrupt:
        print("[watchdog] stopped", flush=True)
        sys.exit(0)
