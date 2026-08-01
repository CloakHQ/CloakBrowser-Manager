#!/usr/bin/env python3
"""
Data-plane contract probe — runs INSIDE the cloakbrowser-manager image.

The shipped docker/nginx.conf and entrypoint.sh have no other automated
coverage: nothing in backend/tests parses, executes or boots either of them, so
every edit to the ingress path used to ship on the strength of a few manual
curls. This script boots the REAL config (only log destinations, the pid path
and the 1800s read timeout are templated) against stub upstreams on loopback,
exercises the contracts the ingress promises, then runs the REAL entrypoint.sh
under nginx/uvicorn stand-ins to check its signal handling.

Architecture:
  stub :8081  — plays both control-plane roles nginx points at 127.0.0.1:8081:
                /api/viewer-auth (verdict encoded in the token prefix) and the
                catch-all `location /` upstream.
  stub :9000  — plays a per-profile KasmVNC port; the auth stub hands nginx
                this address in X-Viewer-Upstream.
  nginx :8080 — the shipped config, rendered.
  Every stub records the request line and headers it saw; the probe reads them
  back over a loopback side channel that never traverses nginx.

Output is a JSON object of check -> {ok, detail}, framed by sentinel lines so a
caller can recover it from mixed stdout. Exit status is 0 only when every check
passed. Run it via backend/tests/test_dataplane.py, or directly:

  docker run --rm --network none -v "$PWD":/repo:ro \
    --entrypoint python cloakbrowser-manager:kasm15 \
    /repo/scripts/dataplane_probe.py --repo /repo
"""

import argparse
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

JSON_BEGIN = "---DATAPLANE-JSON-BEGIN---"
JSON_END = "---DATAPLANE-JSON-END---"

NGINX_PORT = 8080
CONTROL_PLANE_PORT = 8081
KASM_UPSTREAM_PORT = 9000

# The rendered read timeout. Everything the idle-connection checks assert is
# expressed as a multiple of this, so the whole probe stays under ~15s.
SCALED_READ_TIMEOUT_S = 3.0
IDLE_SURVIVAL_S = 9.0

VIEWER_AUTHORIZATION = "Basic dGVzdG93bmVyOnRlc3RwYXNz"


# ── stub upstream ────────────────────────────────────────────────────────────


def parse_http_head(raw_head):
    """Split a raw request head into (request_line, {lowercased header: value})."""
    lines = raw_head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers


def read_http_head(sock):
    """Read bytes until the end of the header block; None if the peer hung up."""
    buffered = b""
    while b"\r\n\r\n" not in buffered:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buffered += chunk
    return buffered.split(b"\r\n\r\n", 1)[0]


def auth_verdict_for_uri(original_uri):
    """Map the viewer token's prefix onto the status the auth endpoint returns.

    Encoding the verdict in the token lets one long-lived stub cover the whole
    auth_request status matrix without a restart between cases.
    """
    match = re.match(r"^/viewer/([^/?]+)", original_uri or "")
    token = match.group(1) if match else ""
    for prefix, status in (
        ("allow", 200),
        ("deny401", 401),
        ("deny403", 403),
        ("err404", 404),
        ("err500", 500),
    ):
        if token.startswith(prefix):
            return status
    return 500


def build_auth_response(status):
    """Serialize the auth subrequest reply, with upstream hints on the 2xx case."""
    reason = {200: "OK", 401: "Unauthorized", 403: "Forbidden",
              404: "Not Found", 500: "Internal Server Error"}[status]
    head = [f"HTTP/1.1 {status} {reason}", "Content-Length: 0"]
    if status == 200:
        head.append(f"X-Viewer-Upstream: 127.0.0.1:{KASM_UPSTREAM_PORT}")
        head.append(f"X-Viewer-Authorization: {VIEWER_AUTHORIZATION}")
    return ("\r\n".join(head) + "\r\n\r\n").encode()


def build_json_response(payload):
    """Serialize a 200 with a JSON body, used to echo what the upstream saw."""
    body = json.dumps(payload).encode()
    head = (f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode()
    return head + body


WEBSOCKET_ACCEPT = (
    "HTTP/1.1 101 Switching Protocols\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n"
).encode()


def serve_stub_connection(sock, is_auth_endpoint, recorded, recorded_lock):
    """Answer one stub request: auth verdict, WebSocket upgrade, or JSON echo."""
    try:
        raw_head = read_http_head(sock)
        if raw_head is None:
            return
        request_line, headers = parse_http_head(raw_head)
        seen = {"request_line": request_line, "headers": headers}
        path = request_line.split(" ")[1] if " " in request_line else ""

        if path == "/__recorded":
            with recorded_lock:
                drained = list(recorded)
                recorded.clear()
            sock.sendall(build_json_response(drained))
            return

        with recorded_lock:
            recorded.append(seen)

        if is_auth_endpoint and path.startswith("/api/viewer-auth"):
            sock.sendall(build_auth_response(
                auth_verdict_for_uri(headers.get("x-original-uri", ""))))
            return

        if headers.get("upgrade", "").lower() == "websocket":
            sock.sendall(WEBSOCKET_ACCEPT)
            # Deliberately silent forever: an upgraded tunnel with no upstream
            # traffic is exactly the idle-CDP case under test. The probe closes
            # us out by exiting the process.
            sock.settimeout(120)
            try:
                while sock.recv(4096):
                    pass
            except OSError:
                pass
            return

        sock.sendall(build_json_response(seen))
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def start_stub_server(port, is_auth_endpoint):
    """Bind a threaded loopback stub; returns a callable draining its recordings."""
    recorded, recorded_lock = [], threading.Lock()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(64)

    def accept_loop():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(
                target=serve_stub_connection,
                args=(client, is_auth_endpoint, recorded, recorded_lock),
                daemon=True,
            ).start()

    threading.Thread(target=accept_loop, daemon=True).start()

    def drain():
        with recorded_lock:
            drained = list(recorded)
            recorded.clear()
        return drained

    return drain


# ── nginx under test ─────────────────────────────────────────────────────────


def render_nginx_conf(source_text, work_dir):
    """Retarget logs/pid and scale the read timeout; everything else is shipped as-is.

    Each substitution is asserted to have fired. Without that a rename in
    nginx.conf would silently turn the timeout checks vacuous instead of red.
    """
    substitutions = [
        ("error_log /dev/stderr warn;", f"error_log {work_dir}/error.log info;", 1),
        ("access_log /dev/stdout redacted;", f"access_log {work_dir}/access.log redacted;", 1),
        ("pid /run/nginx.pid;", f"pid {work_dir}/nginx.pid;", 1),
        ("proxy_read_timeout 1800s;",
         f"proxy_read_timeout {int(SCALED_READ_TIMEOUT_S)}s;", 2),
    ]
    rendered = source_text
    for needle, replacement, expected in substitutions:
        found = rendered.count(needle)
        if found != expected:
            raise SystemExit(
                f"nginx.conf no longer contains {expected}x {needle!r} (found {found}); "
                "the probe cannot render a faithful config")
        rendered = rendered.replace(needle, replacement)
    return rendered


def start_nginx(conf_path, work_dir):
    """Validate then run nginx in the foreground; returns the Popen handle."""
    subprocess.run(["nginx", "-t", "-c", conf_path], check=True,
                   capture_output=True, text=True)
    process = subprocess.Popen(["nginx", "-c", conf_path, "-g", "daemon off;"])
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", NGINX_PORT), timeout=0.5):
                return process
        except OSError:
            time.sleep(0.1)
    raise SystemExit(f"nginx did not accept on {NGINX_PORT}; see {work_dir}/error.log")


# ── clients ──────────────────────────────────────────────────────────────────


def http_probe(method, path, port=NGINX_PORT, extra_headers=None):
    """Issue one request without following redirects; returns (status, headers, body)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, headers=extra_headers or {})
        response = connection.getresponse()
        body = response.read().decode("utf-8", "replace")
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def drain_recordings(port):
    """Read a stub's recorded requests back over loopback, bypassing nginx."""
    _, _, body = http_probe("GET", "/__recorded", port=port)
    return json.loads(body)


def open_websocket(path):
    """Perform an upgrade through nginx; returns (socket, status_line, headers)."""
    sock = socket.create_connection(("127.0.0.1", NGINX_PORT), timeout=10)
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{NGINX_PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: http://127.0.0.1:{NGINX_PORT}\r\n\r\n"
    )
    sock.sendall(request.encode())
    raw_head = read_http_head(sock)
    if raw_head is None:
        sock.close()
        raise OSError(f"upstream closed before responding to upgrade of {path}")
    status_line, headers = parse_http_head(raw_head)
    return sock, status_line, headers


def measure_idle_lifetime(sock, budget_s):
    """Block until the peer FINs or the budget expires; returns (closed, seconds)."""
    started = time.time()
    sock.settimeout(budget_s)
    try:
        closed = sock.recv(4096) == b""
    except socket.timeout:
        closed = False
    except OSError:
        closed = True
    return closed, round(time.time() - started, 2)


# ── nginx checks ─────────────────────────────────────────────────────────────


def check_auth_status_mapping():
    """auth_request contract: 2xx allows, 401/403 pass through, anything else is a 500."""
    expected = {
        "allow-tok": 200, "deny401-tok": 401, "deny403-tok": 403,
        "err404-tok": 500, "err500-tok": 500,
    }
    observed = {token: http_probe("GET", f"/viewer/{token}/index.html")[0]
                for token in expected}
    return observed == expected, f"expected {expected} got {observed}"


def check_redirect_is_relative_with_args():
    """/viewer/<t> must 308 to a RELATIVE Location keeping every query argument.

    An absolute Location would be built from nginx's own http://host:8080
    listener — wrong scheme behind a TLS terminator (mixed content in the
    iframe) and a port that is not published.
    """
    query = "?path=viewer%2Fallow-tok%2Fwebsockify&autoconnect=true"
    status, headers, _ = http_probe("GET", f"/viewer/allow-tok{query}")
    location = headers.get("Location", "")
    ok = (status == 308
          and location == f"/viewer/allow-tok/{query}"
          and not location.startswith("http"))
    bare_status, bare_headers, _ = http_probe("GET", "/viewer/allow-tok")
    ok = ok and bare_status == 308 and bare_headers.get("Location") == "/viewer/allow-tok/"
    return ok, (f"args: {status} {location!r}; "
                f"bare: {bare_status} {bare_headers.get('Location')!r}")


def check_kasm_management_api_blocked():
    """Kasm's own /api must 403 for every method and form, before auth even runs.

    The token used here would otherwise authorize (allow-*), so a 403 proves the
    nested `return` short-circuits in the rewrite phase rather than the client
    merely failing auth.
    """
    cases = [("GET", "/viewer/allow-tok/api"),
             ("GET", "/viewer/allow-tok/api/"),
             ("GET", "/viewer/allow-tok/api/get_frame_stats"),
             ("POST", "/viewer/allow-tok/api/get_screenshot"),
             ("GET", "/viewer/err404-tok/api/get_frame_stats")]
    observed = {f"{method} {path}": http_probe(method, path)[0]
                for method, path in cases}
    return all(status == 403 for status in observed.values()), str(observed)


def check_viewer_prefix_is_stripped():
    """The token prefix must not reach KasmVNC, and the query must survive it."""
    drain_recordings(KASM_UPSTREAM_PORT)
    http_probe("GET", "/viewer/allow-tok/index.html?path=websockify&autoconnect=true")
    seen = drain_recordings(KASM_UPSTREAM_PORT)
    lines = [entry["request_line"] for entry in seen]
    ok = lines == ["GET /index.html?path=websockify&autoconnect=true HTTP/1.1"]
    return ok, str(lines)


def check_viewer_websocket_upgrade():
    """The upgrade must reach KasmVNC intact, on the stripped path."""
    drain_recordings(KASM_UPSTREAM_PORT)
    sock, status_line, _ = open_websocket("/viewer/allow-tok/websockify")
    sock.close()
    seen = drain_recordings(KASM_UPSTREAM_PORT)
    upgraded = [entry for entry in seen
                if entry["headers"].get("upgrade", "").lower() == "websocket"]
    ok = ("101" in status_line
          and len(upgraded) == 1
          and upgraded[0]["request_line"] == "GET /websockify HTTP/1.1"
          and upgraded[0]["headers"].get("connection", "").lower() == "upgrade")
    return ok, f"{status_line!r}; upstream saw {upgraded}"


def check_control_plane_websocket_upgrade():
    """The CDP tunnel must upgrade through `location /`'s regex sibling too."""
    drain_recordings(CONTROL_PLANE_PORT)
    sock, status_line, _ = open_websocket("/api/profiles/p1/cdp")
    sock.close()
    seen = drain_recordings(CONTROL_PLANE_PORT)
    upgraded = [entry for entry in seen
                if entry["headers"].get("upgrade", "").lower() == "websocket"]
    ok = ("101" in status_line
          and len(upgraded) == 1
          and upgraded[0]["request_line"] == "GET /api/profiles/p1/cdp HTTP/1.1"
          and upgraded[0]["headers"].get("connection", "").lower() == "upgrade")
    return ok, f"{status_line!r}; upstream saw {upgraded}"


def check_idle_cdp_tunnel_survives():
    """An idle CDP tunnel must outlive the read timeout; a plain one must not.

    The control leg is load-bearing: without it a config whose timeout scaling
    silently stopped applying would make the survival assertion vacuous.
    """
    results = {}

    def probe(name, path, budget):
        try:
            sock, status_line, _ = open_websocket(path)
            if "101" not in status_line:
                results[name] = ("no-upgrade", status_line)
                return
            results[name] = measure_idle_lifetime(sock, budget)
            sock.close()
        except OSError as error:
            results[name] = ("error", str(error))

    threads = [
        threading.Thread(target=probe,
                         args=("cdp", "/api/profiles/p1/cdp", IDLE_SURVIVAL_S)),
        threading.Thread(target=probe,
                         args=("plain", "/api/profiles/p1/notcdp",
                               SCALED_READ_TIMEOUT_S * 2)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    cdp_closed, cdp_elapsed = results["cdp"]
    plain_closed, plain_elapsed = results["plain"]
    ok = cdp_closed is False and plain_closed is True
    return ok, (f"cdp closed={cdp_closed} after {cdp_elapsed}s "
                f"(read_timeout {SCALED_READ_TIMEOUT_S}s, budget {IDLE_SURVIVAL_S}s); "
                f"plain closed={plain_closed} after {plain_elapsed}s")


def check_authorization_is_injected():
    """nginx must replace any client Authorization with the auth response's value."""
    drain_recordings(KASM_UPSTREAM_PORT)
    http_probe("GET", "/viewer/allow-tok/index.html",
               extra_headers={"Authorization": "Basic Y2xpZW50OnN1cHBsaWVk"})
    seen = drain_recordings(KASM_UPSTREAM_PORT)
    observed = [entry["headers"].get("authorization") for entry in seen]
    return observed == [VIEWER_AUTHORIZATION], str(observed)


def check_healthz():
    """The external probe endpoint answers without touching the control plane."""
    status, _, _ = http_probe("GET", "/healthz")
    return status == 200, f"status {status}"


NGINX_CHECKS = (
    ("auth_status_mapping", check_auth_status_mapping),
    ("redirect_relative_with_args", check_redirect_is_relative_with_args),
    ("kasm_management_api_blocked", check_kasm_management_api_blocked),
    ("viewer_prefix_stripped", check_viewer_prefix_is_stripped),
    ("viewer_websocket_upgrade", check_viewer_websocket_upgrade),
    ("control_plane_websocket_upgrade", check_control_plane_websocket_upgrade),
    ("idle_cdp_tunnel_survives", check_idle_cdp_tunnel_survives),
    ("authorization_injected", check_authorization_is_injected),
    ("healthz", check_healthz),
)


# ── entrypoint signal checks ─────────────────────────────────────────────────

# Both shims reap their own `sleep` on the way out. An orphan that survives the
# shim keeps the inherited stdout open, and anything waiting on EOF from the
# entrypoint's output would then block for the sleep's full duration.
# Both shims journal their own lifecycle when DP_JOURNAL is set. Without it
# there is no way to tell "shutdown() stopped both children in the documented
# order" from "shutdown() returned 0" — and the second is what a shutdown()
# that killed nothing at all would also do.
SHIM_JOURNAL = """_log() {
    if [ -n "${DP_JOURNAL:-}" ]; then echo "$(date +%s.%N) $1 $2" >> "$DP_JOURNAL"; fi
}
"""

NGINX_SHIM = """#!/bin/bash
""" + SHIM_JOURNAL + """
for arg in "$@"; do [ "$arg" = "-t" ] && { sleep "${DP_NGINX_T_DELAY:-0}"; exit 0; }; done
_log nginx start
sleep 300 & idle_pid=$!
trap '_log nginx term; kill "$idle_pid" 2>/dev/null; _log nginx exit; exit 0' TERM
wait "$idle_pid"
"""

UVICORN_SHIM = """#!/bin/bash
# Stands in for the lifespan shutdown: cleanup_all closes every profile with a
# bounded 10s wait each, so the entrypoint really does sit in `wait` for
# seconds to tens of seconds after the first SIGTERM.
""" + SHIM_JOURNAL + """
_log uvicorn start
sleep 300 & idle_pid=$!
_bye() {
    _log uvicorn term
    kill "$idle_pid" 2>/dev/null
    sleep "${DP_UVICORN_TERM_DELAY:-0}"
    _log uvicorn exit
    exit 0
}
trap _bye TERM
wait "$idle_pid"
"""


def install_entrypoint_shims(shim_dir):
    """Write nginx/uvicorn stand-ins so entrypoint.sh can run without either."""
    os.makedirs(shim_dir, exist_ok=True)
    for name, body in (("nginx", NGINX_SHIM), ("uvicorn", UVICORN_SHIM)):
        path = os.path.join(shim_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, 0o755)


def run_entrypoint(entrypoint_path, shim_dir, env_overrides, signal_delays_s):
    """Run entrypoint.sh as a NON-PID-1 child and TERM it on the given schedule.

    Non-PID-1 on purpose. A container's namespace init has SIGNAL_UNKILLABLE
    set, so the kernel discards a SIGTERM whose disposition is the default —
    which would mask the `trap -` bug at PID 1 and make this check vacuous.
    Every deployment that puts a supervisor in front (docker run --init, k8s,
    podman) runs the entrypoint exactly as this does.
    """
    environment = dict(os.environ)
    environment["PATH"] = shim_dir + os.pathsep + environment["PATH"]
    environment.update(env_overrides)
    log_path = os.path.join(shim_dir, "entrypoint.log")
    # A file, not a pipe: communicate() waits for EOF on stdout, which any shim
    # descendant that outlives the entrypoint would hold open long past exit.
    with open(log_path, "w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            ["/bin/bash", entrypoint_path], env=environment,
            stdout=log_handle, stderr=subprocess.STDOUT, text=True)
        started = time.time()
        for delay in signal_delays_s:
            time.sleep(max(0.0, delay - (time.time() - started)))
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
            code = process.returncode
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            code = None
    with open(log_path, encoding="utf-8") as log_handle:
        return code, log_handle.read()


def check_double_sigterm_during_shutdown(entrypoint_path, shim_dir):
    """A second SIGTERM inside shutdown() must be ignored, not fatal."""
    code, output = run_entrypoint(
        entrypoint_path, shim_dir, {"DP_UVICORN_TERM_DELAY": "3"}, [1.0, 1.7])
    return code == 0, (f"exit={code} (-15/143 = SIGTERM killed the shell mid-cleanup); "
                       f"output={output.strip()!r}")


def check_sigterm_before_children_start(entrypoint_path, shim_dir):
    """A SIGTERM during start-up must still run shutdown() instead of killing PID 1."""
    code, output = run_entrypoint(
        entrypoint_path, shim_dir, {"DP_NGINX_T_DELAY": "3"}, [1.0])
    return code == 0, (f"exit={code} (-15/143 = the trap was not armed yet); "
                       f"output={output.strip()!r}")


def read_journal(journal_path):
    """Parse the shim journal into {"<who> <event>": timestamp}."""
    events = {}
    if not os.path.exists(journal_path):
        return events
    with open(journal_path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) == 3:
                events[f"{parts[1]} {parts[2]}"] = float(parts[0])
    return events


def check_shutdown_terminates_children(entrypoint_path, shim_dir):
    """shutdown() must stop BOTH children, and finish uvicorn before nginx.

    The other two entrypoint checks assert only that the shell exits 0. A
    shutdown() that killed nothing at all also exits 0 — while the container
    goes away with Chromium still live and still writing to user_data_dir,
    which is the precise outcome the ordered teardown exists to prevent. So
    the children have to be observed dying, not merely inferred.

    The order is the other half. uvicorn's lifespan shutdown is what closes
    every profile, and it reaches those browsers over the CDP tunnels nginx
    proxies; tearing nginx down first pulls the data plane out from under the
    cleanup that is still using it.
    """
    journal = os.path.join(shim_dir, "shutdown-journal.txt")
    if os.path.exists(journal):
        os.remove(journal)
    # A measurable gap between uvicorn's TERM and its exit, so "nginx was
    # signalled after uvicorn had FINISHED" is distinguishable from "both were
    # signalled at once".
    code, output = run_entrypoint(
        entrypoint_path, shim_dir,
        {"DP_UVICORN_TERM_DELAY": "1", "DP_JOURNAL": journal}, [1.0])
    events = read_journal(journal)

    missing = [key for key in ("uvicorn term", "uvicorn exit", "nginx term", "nginx exit")
               if key not in events]
    if code != 0 or missing:
        return False, (f"exit={code}, missing={missing}, journal={events}, "
                       f"output={output.strip()!r}")
    if events["nginx term"] < events["uvicorn exit"]:
        return False, ("nginx was signalled "
                       f"{events['uvicorn exit'] - events['nginx term']:.2f}s before "
                       "uvicorn finished shutting down; the cleanup reaches the "
                       f"browsers through nginx. journal={events}")
    return True, (f"uvicorn term->exit {events['uvicorn exit'] - events['uvicorn term']:.2f}s, "
                  f"nginx signalled {events['nginx term'] - events['uvicorn exit']:.2f}s after")


# ── encoder probe ────────────────────────────────────────────────────────────


PROBE_SUBSET_MARKER = "Using CLI-specified video codecs (supported subset):"


def xvnc_supported_codecs(video_codec, display=":91"):
    """Start Xvnc with -videoCodec and return the subset it says it will use."""
    command = [
        "Xvnc", display, "-websocketPort", "6191", "-geometry", "800x600",
        "-depth", "24", "-SecurityTypes", "None", "-interface", "127.0.0.1",
        "-AlwaysShared", "-Log", "*:stdout:100", "-videoCodec", video_codec,
        # Required here, not cosmetic: the probe container runs --network none,
        # and Xvnc EXITS ("Failed to get public IP, please specify it with
        # -publicIP") before it ever reaches the encoder probe. Without this
        # the check fails for a reason that has nothing to do with codecs.
        "-publicIP", "127.0.0.1",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
        output = completed.stdout + completed.stderr
    except subprocess.TimeoutExpired as expired:
        output = (expired.stdout or b"").decode() + (expired.stderr or b"").decode()
    finally:
        for path in (f"/tmp/.X{display[1:]}-lock", f"/tmp/.X11-unix/X{display[1:]}"):
            try:
                os.remove(path)
            except OSError:
                pass
    for line in output.splitlines():
        if PROBE_SUBSET_MARKER in line:
            return line.split(PROBE_SUBSET_MARKER, 1)[1].split()
    return None


def check_codec_probe_narrowing(entrypoint_path, shim_dir):
    """-videoCodec must narrow what the server will offer a client.

    This is the single empirical fact KASM_ENCODING_POLICY=video rests on. The
    server does not ENFORCE the codec — ConnParams accepts any streaming-mode
    pseudo-encoding a client offers — but the client we ship can only choose
    from the set the server advertises, and that set is this probe's output. If
    an upstream bump ever stopped -videoCodec narrowing it, the policy would
    silently start offering every encoder the build has, including the software
    AV1 that stalls a core per keyframe and then fails the session to Tight.
    """
    del entrypoint_path, shim_dir  # runs against Xvnc, not the entrypoint
    narrowed = xvnc_supported_codecs("h264")
    widest = xvnc_supported_codecs("auto")
    if narrowed is None or widest is None:
        return False, (f"Xvnc printed no {PROBE_SUBSET_MARKER!r} line "
                       f"(h264={narrowed}, auto={widest})")
    if narrowed != ["libx264"]:
        return False, f"-videoCodec h264 offered {narrowed}, expected ['libx264']"
    if not set(narrowed) <= set(widest):
        return False, f"h264 subset {narrowed} is not contained in auto's {widest}"
    if len(widest) <= len(narrowed):
        return False, (f"-videoCodec did not narrow anything: h264={narrowed} "
                       f"auto={widest}. Either the build lost its extra encoders "
                       f"or the flag stopped filtering.")
    return True, f"h264 -> {narrowed}; auto -> {widest}"


ENTRYPOINT_CHECKS = (
    ("entrypoint_double_sigterm", check_double_sigterm_during_shutdown),
    ("entrypoint_sigterm_before_children", check_sigterm_before_children_start),
    ("entrypoint_shutdown_terminates_children", check_shutdown_terminates_children),
    ("codec_probe_narrowing", check_codec_probe_narrowing),
)


# ── driver ───────────────────────────────────────────────────────────────────


def run_checks(named_checks, extra_args=()):
    """Run each check, capturing exceptions as failures rather than aborting."""
    results = {}
    for name, function in named_checks:
        try:
            ok, detail = function(*extra_args)
        except Exception as error:  # a crashed check is a failed check
            ok, detail = False, f"{type(error).__name__}: {error}"
        results[name] = {"ok": bool(ok), "detail": detail}
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return results


def main(repo_dir, work_dir):
    """Boot the shipped data plane against stubs, run every check, emit JSON."""
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir, exist_ok=True)

    conf_source = os.path.join(repo_dir, "docker", "nginx.conf")
    with open(conf_source, encoding="utf-8") as handle:
        rendered = render_nginx_conf(handle.read(), work_dir)
    conf_path = os.path.join(work_dir, "nginx.conf")
    with open(conf_path, "w", encoding="utf-8") as handle:
        handle.write(rendered)

    start_stub_server(CONTROL_PLANE_PORT, is_auth_endpoint=True)
    start_stub_server(KASM_UPSTREAM_PORT, is_auth_endpoint=False)

    nginx = start_nginx(conf_path, work_dir)
    try:
        results = run_checks(NGINX_CHECKS)
    finally:
        nginx.terminate()
        nginx.wait(timeout=10)

    shim_dir = os.path.join(work_dir, "shim")
    install_entrypoint_shims(shim_dir)
    entrypoint_path = os.path.join(repo_dir, "entrypoint.sh")
    results.update(run_checks(ENTRYPOINT_CHECKS, (entrypoint_path, shim_dir)))

    print(JSON_BEGIN, flush=True)
    print(json.dumps(results, indent=2), flush=True)
    print(JSON_END, flush=True)
    return 0 if all(entry["ok"] for entry in results.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="/repo",
                        help="repository root as mounted inside the container")
    parser.add_argument("--work-dir", default="/tmp/dataplane-probe",
                        help="scratch directory for the rendered config and logs")
    arguments = parser.parse_args()
    sys.exit(main(arguments.repo, arguments.work_dir))
