#!/bin/sh
# Cloudflare Tunnel sidecar.
#
# Picks its mode from the environment because the two cloudflared invocations
# are different subcommands, not different flags: a named tunnel is
# `tunnel run` (routing comes from the Cloudflare dashboard), a free one is
# `tunnel --url <origin>` (routing is the flag). The upstream image is
# distroless and has no shell, so this cannot be an inline `sh -c` in compose —
# hence the thin wrapper image this script ships in.
set -e

ORIGIN="${TUNNEL_ORIGIN:-http://manager:8080}"

# An operator-supplied AUTH_TOKEN (host env/.env) always wins and is used as
# it arrived. Otherwise, the Manager's own entrypoint generated or loaded one
# on its side and wrote it to /data/auth_token — the ONE channel between two
# separate containers, since compose's ${AUTH_TOKEN:-} substitution only ever
# sees the HOST's environment, never what a sibling container decides for
# itself at runtime. `depends_on: condition: service_healthy` in
# docker-compose.yml guarantees the Manager finished that step before this
# container even starts, so the file is always there by now.
AUTH_TOKEN_FILE=/data/auth_token
if [ -z "${AUTH_TOKEN:-}" ] && [ -s "$AUTH_TOKEN_FILE" ]; then
    AUTH_TOKEN="$(cat "$AUTH_TOKEN_FILE")"
fi

# The tunnel publishes the Manager to the public internet, and AUTH_TOKEN is the
# only thing in front of it. Unset, the API answers unauthenticated — and this
# API launches browsers and hands out a CDP tunnel into them, so an open one is
# remote code execution for anyone who guesses the hostname. A *.trycloudflare
# name is random, but it is also unauthenticated-by-default and handed to
# Cloudflare's edge, so "nobody will find it" is not a control. Refuse instead.
# In normal operation this never fires — auth is mandatory now (see the
# Manager's entrypoint.sh) — it is a backstop for /data not being mounted here.
if [ -z "${AUTH_TOKEN:-}" ] && [ -z "${TUNNEL_ALLOW_NO_AUTH:-}" ]; then
    echo "REFUSING TO START: AUTH_TOKEN is empty." >&2
    echo "  A tunnel publishes this Manager to the internet, and its API can" >&2
    echo "  launch browsers and proxy CDP. Set AUTH_TOKEN in .env, or set" >&2
    echo "  TUNNEL_ALLOW_NO_AUTH=1 if you really intend an open endpoint." >&2
    exit 1
fi

if [ -n "${TUNNEL_TOKEN:-}" ]; then
    echo "cloudflared: named tunnel (CF_TUNNEL_TOKEN supplied)."
    echo "cloudflared: ingress/hostname come from the Cloudflare dashboard;"
    echo "cloudflared: point that tunnel's public hostname at ${ORIGIN}."
    # `exec` so cloudflared is PID 1 and gets Docker's SIGTERM directly.
    exec cloudflared --no-autoupdate tunnel run
fi

echo "cloudflared: no CF_TUNNEL_TOKEN — starting a FREE quick tunnel."
echo "cloudflared: origin = ${ORIGIN}"
echo "cloudflared: the public https://<random>.trycloudflare.com URL is printed"
echo "cloudflared: below, and CHANGES EVERY RESTART. Retrieve it with:"
echo "cloudflared:   docker compose logs tunnel | grep trycloudflare.com"
exec cloudflared --no-autoupdate tunnel --url "$ORIGIN"
