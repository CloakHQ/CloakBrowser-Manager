# CloakBrowser host watchdog

## Problem

`auto_launch=true` only launches profiles when the manager process starts.
If the manager stays alive but an individual browser/profile crashes and flips
back to `stopped`, the built-in startup auto-launch does not bring it back.

## What this adds

- `cloakbrowser_watchdog.py` polls the manager API
- any profile with `auto_launch=true` and `status != running` gets relaunched
- intended for host-level `systemd` supervision

## Install

Assume your repo checkout lives at `/opt/cloakbrowser-manager` and the manager
API is reachable at `http://127.0.0.1:8080`.

```bash
sudo cp ops/watchdog/cloakbrowser-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cloakbrowser-watchdog.service
```

## Verify

```bash
systemctl status cloakbrowser-watchdog.service --no-pager
journalctl -u cloakbrowser-watchdog.service -n 50 --no-pager
curl http://127.0.0.1:8080/api/profiles
```

Smoke test:
- create a profile
- set `auto_launch=true`
- wait for watchdog to launch it
- stop the profile manually
- verify watchdog launches it again within ~15s

## Caveat

If you intentionally want a profile to stay down, turn `auto_launch=false`
first. Otherwise the watchdog will treat a manual stop the same as a crash and
bring it back.
