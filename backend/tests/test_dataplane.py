"""Data-plane coverage: docker/nginx.conf and entrypoint.sh.

Neither file had a single automated assertion before this module, so every
ingress change shipped on manual curls — which is how a 30-minute idle reaping
of CDP tunnels and a fatal second SIGTERM both got through review.

Two layers:

* Static lint of docker/nginx.conf. Runs everywhere, no dependencies. Guards the
  one inheritance rule the config silently depends on.
* Behavioural contract, executed by scripts/dataplane_probe.py inside the
  cloakbrowser-manager image, which boots the SHIPPED config against stub
  upstreams and runs the SHIPPED entrypoint under nginx/uvicorn stand-ins.
  Skipped when docker or the image is unavailable; ~25s when it runs.

Run just this file:

    ~/anaconda3/bin/python -m pytest backend/tests/test_dataplane.py -v

Run the probe by hand (same thing, more output):

    docker run --rm --network none -v "$PWD":/repo:ro \\
      --entrypoint python cloakbrowser-manager:kasm15 \\
      /repo/scripts/dataplane_probe.py --repo /repo
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONF = REPO_ROOT / "docker" / "nginx.conf"
PROBE_SCRIPT = REPO_ROOT / "scripts" / "dataplane_probe.py"
PROBE_IMAGE = os.environ.get("DATAPLANE_IMAGE", "cloakbrowser-manager:kasm15")

JSON_BEGIN = "---DATAPLANE-JSON-BEGIN---"
JSON_END = "---DATAPLANE-JSON-END---"

# Every check scripts/dataplane_probe.py reports. Listed here rather than
# derived from the run so that a probe which silently stops emitting a check
# fails the suite instead of shrinking it.
PROBE_CHECKS = (
    "auth_status_mapping",
    "redirect_relative_with_args",
    "kasm_management_api_blocked",
    "viewer_prefix_stripped",
    "viewer_websocket_upgrade",
    "control_plane_websocket_upgrade",
    "idle_cdp_tunnel_survives",
    "authorization_injected",
    "healthz",
    "entrypoint_double_sigterm",
    "entrypoint_sigterm_before_children",
)


# ── static config lint ───────────────────────────────────────────────────────


def parse_nginx_blocks(config_text: str) -> list[dict]:
    """Return every brace block as {header, body, parents} in source order.

    Comments are stripped first; nginx comments run to end of line and this
    config has no quoted '#'.
    """
    stripped = re.sub(r"#[^\n]*", "", config_text)
    blocks: list[dict] = []
    stack: list[tuple[str, int, list[str]]] = []
    token_start = 0
    for index, char in enumerate(stripped):
        if char == "{":
            header = " ".join(stripped[token_start:index].split())
            stack.append((header, index + 1, [entry[0] for entry in stack]))
            token_start = index + 1
        elif char == "}":
            header, body_start, parents = stack.pop()
            blocks.append({"header": header,
                           "body": stripped[body_start:index],
                           "parents": parents})
            token_start = index + 1
        elif char == ";":
            token_start = index + 1
    assert not stack, "unbalanced braces in nginx config"
    return blocks


def nested_viewer_locations_missing_rewrite(config_text: str) -> list[str]:
    """Headers of locations nested under /viewer/ that proxy without the prefix strip.

    auth_request IS inherited into a nested location; `rewrite` and `proxy_pass`
    are NOT. A nested block that proxies therefore forwards the unstripped
    /viewer/<token>/... to KasmVNC and gets a 404 after a successful auth.
    """
    return [
        block["header"]
        for block in parse_nginx_blocks(config_text)
        if block["parents"]
        and block["parents"][-1].startswith("location /viewer/")
        and "proxy_pass" in block["body"]
        and "rewrite ^/viewer/" not in block["body"]
    ]


def test_nested_viewer_locations_strip_the_token_prefix():
    """No nested /viewer/ location may proxy without repeating the rewrite."""
    offenders = nested_viewer_locations_missing_rewrite(
        NGINX_CONF.read_text(encoding="utf-8"))
    assert offenders == [], (
        "these locations nested under `location /viewer/` proxy without "
        f"`rewrite ^/viewer/...`, so KasmVNC will see the token-bearing path: {offenders}")


def test_nested_viewer_location_guard_detects_a_missing_rewrite():
    """The guard above must actually trip — proves it is not vacuously empty."""
    synthetic = """
    http {
        server {
            location /viewer/ {
                location ~ ^/viewer/[^/]+/websockify$ {
                    proxy_pass http://$viewer_upstream;
                }
                location ~ ^/viewer/[^/]+/api(/|$) { return 403; }
                auth_request /_viewer_auth;
                rewrite ^/viewer/[^/]+(/.*)$ $1 break;
                proxy_pass http://$viewer_upstream;
            }
        }
    }
    """
    assert nested_viewer_locations_missing_rewrite(synthetic) == [
        "location ~ ^/viewer/[^/]+/websockify$"]


# ── behavioural contract, executed in the image ──────────────────────────────


def _probe_unavailable_reason() -> str | None:
    """Why the container probe cannot run here, or None if it can."""
    if shutil.which("docker") is None:
        return "docker is not on PATH"
    inspect = subprocess.run(["docker", "image", "inspect", PROBE_IMAGE],
                             capture_output=True, text=True)
    if inspect.returncode != 0:
        return f"image {PROBE_IMAGE} is not present"
    return None


@pytest.fixture(scope="module")
def probe_results() -> dict:
    """Boot the shipped data plane once and return every check's verdict."""
    reason = _probe_unavailable_reason()
    if reason:
        pytest.skip(f"data-plane probe skipped: {reason}")

    completed = subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{REPO_ROOT}:/repo:ro",
         "--entrypoint", "python", PROBE_IMAGE,
         f"/repo/{PROBE_SCRIPT.relative_to(REPO_ROOT)}", "--repo", "/repo"],
        capture_output=True, text=True, timeout=300)
    output = completed.stdout + completed.stderr
    if JSON_BEGIN not in output or JSON_END not in output:
        pytest.fail(f"probe produced no result block (exit {completed.returncode}):\n{output}")
    payload = output.split(JSON_BEGIN, 1)[1].split(JSON_END, 1)[0]
    return json.loads(payload)


@pytest.mark.parametrize("check_name", PROBE_CHECKS)
def test_dataplane_contract(probe_results: dict, check_name: str):
    """Each contract the ingress promises, asserted against the real config."""
    assert check_name in probe_results, (
        f"scripts/dataplane_probe.py no longer reports {check_name!r}; "
        f"it reported {sorted(probe_results)}")
    result = probe_results[check_name]
    assert result["ok"], f"{check_name}: {result['detail']}"
