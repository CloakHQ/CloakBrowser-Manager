#!/bin/bash
set -e

# Initialize data directories
mkdir -p /data/profiles
# CLOAKBROWSER_CACHE_DIR (set in Dockerfile) points ensure_binary()'s download
# at this path instead of the default ~/.cloakbrowser, so the ~337MB Chromium
# binary lives on the /data volume and survives container recreation instead
# of being re-fetched on every new container.
mkdir -p /data/cloakbrowser
# Extensions the operator drops in on the host (~/.cloakbrowser-manager/extensions
# by default), one unpacked extension per subdirectory. backend/extensions.py
# scans this ONCE per process start and caches the result for the container's
# whole lifetime — see the README note next to EXTENSIONS_DIR for why
# (predictable per-profile state: an extension can't disappear out from under
# a running profile's checkbox mid-session). Adding, removing, or editing an
# extension here needs a `docker compose restart` (or recreate) to be picked
# up; this mkdir is what makes an empty/missing host dir a no-op instead of a
# startup error.
mkdir -p /data/extensions

# Authentication is mandatory — there is no "open" mode. If the operator did
# not set AUTH_TOKEN, generate one and persist it to the /data volume so a
# restart (or a `docker compose recreate`) does not silently issue a NEW
# token and invalidate every bookmark, script and Cloudflare edge session.
# /data/auth_token is also the one thing that lets the tunnel sidecar — a
# SEPARATE container — learn a token this container generated on its own:
# compose's ${AUTH_TOKEN:-} substitution only ever sees the HOST's env/.env,
# never what a sibling container decides at runtime, so the shared volume is
# the only channel between them (see docker/tunnel-entrypoint.sh).
AUTH_TOKEN_FILE=/data/auth_token
auth_token_generated=0
if [ -z "${AUTH_TOKEN:-}" ]; then
    if [ -s "$AUTH_TOKEN_FILE" ]; then
        AUTH_TOKEN="$(cat "$AUTH_TOKEN_FILE")"
    else
        AUTH_TOKEN="$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)"
        auth_token_generated=1
    fi
fi
# Written unconditionally, even when AUTH_TOKEN came from the environment:
# an operator-supplied token must overwrite whatever was here before, and
# /data/auth_token stays the one canonical file the tunnel sidecar reads.
printf '%s' "$AUTH_TOKEN" > "$AUTH_TOKEN_FILE"
chmod 600 "$AUTH_TOKEN_FILE"
export AUTH_TOKEN

echo ""
echo "################################################################"
if [ "$auth_token_generated" = "1" ]; then
    echo "#  NO AUTH_TOKEN WAS SET. GENERATED ONE AND SAVED IT TO:"
    echo "#    $AUTH_TOKEN_FILE"
    echo "#"
fi
echo "#  AUTH_TOKEN=$AUTH_TOKEN"
echo "#"
echo "#  Set AUTH_TOKEN in .env to pin this value across recreations."
echo "################################################################"
echo ""

# Kill stale processes from previous container runs
pkill -f 'Xvnc :[0-9]' 2>/dev/null || true
pkill -f 'cloakbrowser.*chrome' 2>/dev/null || true
pkill -f 'chromium.*fingerprint' 2>/dev/null || true
pkill -f xclip 2>/dev/null || true

# Clean Chrome lock files left on the persistent volume
find /data/profiles -maxdepth 2 -name 'SingletonLock' -delete 2>/dev/null || true
find /data/profiles -maxdepth 2 -name 'SingletonCookie' -delete 2>/dev/null || true
find /data/profiles -maxdepth 2 -name 'SingletonSocket' -delete 2>/dev/null || true

# Remove X11 lock files from previous displays
rm -f /tmp/.X1*-lock 2>/dev/null || true

# TLS material for the HTTPS listener.
#
# This is not decoration. The KasmVNC client gates EVERY video codec on
# WebCodecs -- `if (!("VideoDecoder" in window)) return "WebCodecs API not
# available"` -- and browsers only expose WebCodecs in a secure context. Reached
# over plain http:// at anything other than localhost, the client therefore
# reports no decodable codecs at all and quietly settles on JPEG/WebP, so NVENC
# never engages no matter how the server is configured. Measured, same browser
# and same viewer URL: via 127.0.0.1 the client negotiated -1029 (h264_nvenc)
# and nvidia-smi showed an encoder session; via the LAN IP it negotiated -1025
# (JPEG/WebP) and the encoder stayed at zero.
#
# The cert lives on the /data volume so it survives a restart. Regenerating it
# every boot would work, but self-signed certs are trusted by exception and a
# new fingerprint invalidates that exception on every single restart.
TLS_DIR=/data/tls
NGINX_TLS_DIR=/etc/nginx/tls
mkdir -p "$TLS_DIR"
if [ ! -s "$TLS_DIR/server.crt" ] || [ ! -s "$TLS_DIR/server.key" ]; then
    # Browsers match the address you typed against the SAN list, not the CN.
    # TLS_SANS carries the address users actually reach this box by, which the
    # container cannot work out for itself: it only ever sees its bridge
    # address, never the host's LAN IP.
    tls_sans="DNS:localhost,IP:127.0.0.1,IP:::1"
    for tls_entry in $(echo "${TLS_SANS:-}" | tr ',' ' '); do
        case "$tls_entry" in
            "") continue ;;
            # Digits and dots only, or anything containing a colon: an address.
            # Everything else is a name.
            *:*)       tls_sans="$tls_sans,IP:$tls_entry" ;;
            *[!0-9.]*) tls_sans="$tls_sans,DNS:$tls_entry" ;;
            *)         tls_sans="$tls_sans,IP:$tls_entry" ;;
        esac
    done
    echo "Generating self-signed TLS certificate (SAN: $tls_sans)"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" \
        -subj "/CN=cloakbrowser-manager" \
        -addext "subjectAltName=$tls_sans" >/dev/null 2>&1
    chmod 600 "$TLS_DIR/server.key"
fi
# Copied into the image rather than read from /data directly, so the shipped
# nginx.conf points at a path that exists even in a container with no volume
# (the data-plane probe boots it that way, and nginx refuses to start when
# ssl_certificate names a missing file).
install -d -m 755 "$NGINX_TLS_DIR"
install -m 644 "$TLS_DIR/server.crt" "$NGINX_TLS_DIR/server.crt"
install -m 600 "$TLS_DIR/server.key" "$NGINX_TLS_DIR/server.key"

# Graceful stop: uvicorn's lifespan shutdown stops the browsers and Xvnc, so
# let it finish before pulling the data plane out from under it.
shutdown() {
    # `trap ''` (ignore), NOT `trap -` (restore the default terminate action):
    # shutdown() blocks in an unbounded `wait` while cleanup_all closes every
    # profile, so a SECOND SIGTERM — `docker compose down` after an impatient
    # `docker stop`, a systemd ExecStop retry, a k8s preStop plus
    # terminationGracePeriod — lands squarely inside that window. With the
    # default disposition restored the shell dies with 143 mid-cleanup, the
    # runtime SIGKILLs the namespace and every Chromium dies uncleanly: exactly
    # the "restore pages?" prompts the ordered teardown and
    # stop_grace_period: 60s exist to prevent. (A container PID 1 with a
    # default disposition ignores SIGTERM anyway, which is why this only bites
    # under `docker run --init`, k8s, or any supervisor that is PID 1 instead.)
    trap '' TERM INT
    # Both guards matter: this trap is armed before either child exists so the
    # start-up window is covered, and `kill -TERM ""` would abort under set -e.
    if [ -n "${uvicorn_pid:-}" ]; then
        kill -TERM "$uvicorn_pid" 2>/dev/null || true
        wait "$uvicorn_pid" 2>/dev/null || true
    fi
    if [ -n "${nginx_pid:-}" ]; then
        kill -TERM "$nginx_pid" 2>/dev/null || true
        wait "$nginx_pid" 2>/dev/null || true
    fi
    exit 0
}
# Armed before the children start: a SIGTERM arriving during `nginx -t`, the
# nginx spawn or the uvicorn spawn would otherwise hit the default disposition
# and kill the shell with cleanup skipped entirely.
trap shutdown TERM INT

# nginx is the sole ingress and uvicorn the sole control plane — the container
# is useless without either. Both run in the foreground under this shell so
# that the death of one takes the container down: Docker does not act on an
# unhealthy container by itself, so terminating is what lets `restart:` bring
# back a working data plane. Running nginx detached instead would leave the
# container "up" with port 8080 dead forever.
# set -e aborts the container if the config test fails.
nginx -t
nginx -g 'daemon off;' &
nginx_pid=$!

cd /app
uvicorn backend.main:app --host 127.0.0.1 --port 8081 --log-level warning &
uvicorn_pid=$!

echo ""
echo "  CloakBrowser Manager running at http://localhost:8080 (nginx → uvicorn 127.0.0.1:8081)"
echo "  HTTPS on :8443 (self-signed). Use it for any non-localhost viewer:"
echo "  hardware video encoding needs WebCodecs, which needs a secure context."
echo ""

# First exit wins.
set +e
wait -n "$nginx_pid" "$uvicorn_pid"
status=$?
if kill -0 "$nginx_pid" 2>/dev/null; then
    echo "FATAL: uvicorn exited (status $status) — shutting down nginx" >&2
    kill -TERM "$nginx_pid" 2>/dev/null
else
    echo "FATAL: nginx exited (status $status) — shutting down uvicorn" >&2
    kill -TERM "$uvicorn_pid" 2>/dev/null
fi
wait
exit $((status == 0 ? 1 : status))
