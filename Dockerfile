# Stage 1: Build React frontend
FROM node:20-slim AS frontend-builder
WORKDIR /build
# package-lock.json is committed — `npm ci` installs exactly what it pins and
# fails loudly if it's ever missing or out of sync with package.json, instead
# of `npm install` silently resolving a different dependency graph than what
# was tested.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Production image
FROM python:3.12-slim

# Chromium system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
    libxcb1 libxext6 libxshmfence1 \
    libglib2.0-0 libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 \
    libgdk-pixbuf-2.0-0 libxss1 libxtst6 fonts-liberation \
    libgl1-mesa-dri libegl-mesa0 \
    procps wget ca-certificates xclip \
    && rm -rf /var/lib/apt/lists/*

# Playwright system deps (matches test-infra)
RUN pip install --no-cache-dir playwright && playwright install-deps chromium 2>/dev/null || true && pip uninstall -y playwright

# Windows core fonts (Arial, Times New Roman, Verdana, etc.)
RUN echo "deb http://deb.debian.org/debian trixie contrib" >> /etc/apt/sources.list.d/contrib.list \
    && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections \
    && apt-get update && apt-get install -y --no-install-recommends ttf-mscorefonts-installer \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# nginx data plane + VA-API (AMD/Intel) + FFmpeg runtime libs for KasmVNC 1.5
# in-band H.264/H.265/AV1 video streaming (dlopen'd at runtime; JPEG/WebP
# fallback if absent). The codec path only engages under
# KASM_ENCODING_POLICY=video — the default server-authoritative policy never
# negotiates a video codec, so these libs sit unused there.
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    libva2 libva-drm2 mesa-va-drivers vainfo \
    libavcodec61 libavformat61 libavutil59 libswscale8 \
    && rm -rf /var/lib/apt/lists/*

# KasmVNC dlopens UNVERSIONED FFmpeg names (libavcodec.so etc.) which Debian
# ships only in -dev packages — symlink them to the installed runtime libs.
# Multi-arch safe: paths come from ldconfig, not hardcoded.
RUN set -e; \
    for lib in libavcodec libavformat libavutil libswscale; do \
        target="$(ldconfig -p | grep -m1 "${lib}\.so\.[0-9]" | awk '{print $NF}')"; \
        test -n "$target"; \
        ln -sf "$target" "$(dirname "$target")/${lib}.so"; \
    done; \
    ldconfig

# Install KasmVNC 1.5.0 (auto-selects amd64 or arm64 based on build platform),
# SHA256-verified — build fails on mismatch
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        amd64) KASM_SHA256=80b241de7dfe53bba2b7e1cc5ac8c5246d72271efa16be2d4f76607f30fab1c4 ;; \
        arm64) KASM_SHA256=fbb11589958a2acccd2d67f67944be79ac1e8e3a1d6172c0e6db6dc59e55a919 ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && wget -q https://github.com/kasmtech/KasmVNC/releases/download/v1.5.0/kasmvncserver_trixie_1.5.0_${TARGETARCH}.deb \
    && echo "${KASM_SHA256}  kasmvncserver_trixie_1.5.0_${TARGETARCH}.deb" | sha256sum -c - \
    && apt-get update && apt-get install -y -f ./kasmvncserver_trixie_1.5.0_${TARGETARCH}.deb \
    && rm kasmvncserver_trixie_1.5.0_${TARGETARCH}.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY backend/requirements.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# The CloakBrowser Chromium binary is deliberately NOT downloaded at build
# time. Two reasons:
#
#   1. License. BINARY-LICENSE.md's "Cloud, Container & Integration Use"
#      section permits storing the unmodified Binary in an internal Docker
#      image, but bundling/pre-installing it into an image distributed to
#      third parties needs a separate OEM/SaaS license. Baking it in here
#      would make every build of this (public, distributable) image do
#      exactly that. Downloading at container launch means the image itself
#      never contains the Binary — each deployment fetches it directly from
#      CloakHQ under its own key, which is the "dependency listing, not
#      redistribution" case the same section says needs no commercial
#      license.
#
#   2. It matches what README.md already documents ("automatically
#      downloaded on first launch") and what ensure_binary() already does
#      today when the build-time key differs from the run-time one (see git
#      history on this comment) — this just makes that the only path instead
#      of a fallback for a mismatch.
#
# CLOAKBROWSER_CACHE_DIR points the download at the /data volume (declared
# below) instead of the default ~/.cloakbrowser, so the ~337MB binary
# survives container recreation and is fetched once per deployment, not once
# per container. entrypoint.sh creates the directory before uvicorn starts.
ENV CLOAKBROWSER_CACHE_DIR=/data/cloakbrowser

# Application code. No CLOAKBROWSER_LICENSE_KEY build ARG here any more —
# nothing at build time consumes it. It still needs to be set at container
# run time (docker-compose.yml's `environment:`), both to pick the Pro binary
# on first download and because the Pro binary re-validates its license at
# every launch (a missing key is exit 77).
COPY backend/ /app/backend/
COPY --from=frontend-builder /build/dist /app/frontend/dist

# A placeholder self-signed cert, so the shipped nginx.conf validates in ANY
# container and not only one the entrypoint has initialised: nginx refuses to
# start when ssl_certificate names a missing file, and the data-plane probe
# boots this config directly with no /data volume and no entrypoint. The
# entrypoint replaces both files at run time with a persistent pair carrying
# the deployment's real SANs (see TLS_SANS).
RUN mkdir -p /etc/nginx/tls \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout /etc/nginx/tls/server.key -out /etc/nginx/tls/server.crt \
        -subj "/CN=cloakbrowser-manager" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null \
    && chmod 600 /etc/nginx/tls/server.key

EXPOSE 8080 8443

# --start-period: the probe goes through nginx to uvicorn, and uvicorn's
# lifespan runs cleanup_stale plus auto_launch_all (LAUNCH_TIMEOUT_S=60 per
# auto-launch profile) before it accepts. Without a grace period those early
# refusals count against --retries and a container that is merely still booting
# is reported `unhealthy` to operators and to `depends_on: service_healthy`.
# Target stays /api/status, not /healthz, so the probe covers nginx AND uvicorn.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=90s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/status')" || exit 1

VOLUME /data

# Config and entrypoint last: both are tiny and change often, and anything
# copied above them invalidates the 119MB pip layer on every edit.
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/fetch-widevine.py /fetch-widevine.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
