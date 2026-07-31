#!/bin/bash
set -e

# Initialize data directories
mkdir -p /data/profiles

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

# Graceful stop: uvicorn's lifespan shutdown stops the browsers and Xvnc, so
# let it finish before pulling the data plane out from under it.
shutdown() {
    trap - TERM INT
    kill -TERM "$uvicorn_pid" 2>/dev/null || true
    wait "$uvicorn_pid" 2>/dev/null || true
    kill -TERM "$nginx_pid" 2>/dev/null || true
    wait "$nginx_pid" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

echo ""
echo "  CloakBrowser Manager running at http://localhost:8080 (nginx → uvicorn 127.0.0.1:8081)"
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
