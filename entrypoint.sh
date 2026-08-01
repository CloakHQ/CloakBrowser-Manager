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
