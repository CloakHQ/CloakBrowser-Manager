# Stage 1: Build React frontend
FROM node:20-slim AS frontend-builder
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
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

# Backend code
COPY backend/ /app/backend/

# Frontend build from stage 1
COPY --from=frontend-builder /build/dist /app/frontend/dist

# Pre-download CloakBrowser binary.
#
# The key has to be in the environment of THIS stage, and before this RUN.
# ensure_binary() resolves the licensed Pro build only when it can read a key,
# and falls back to the free binary silently otherwise — the image then ships
# one Chromium while the first launch downloads a different one (~337MB) into
# the /root/.cloakbrowser VOLUME, inside the launch timeout. Measured, same
# image: no key -> chromium-146.0.7680.177.5, key -> chromium-150.x-pro.
#
# Passed as a build ARG sourced from the gitignored .env via compose, so the
# key stays out of the repo. It is re-exported as ENV because the Pro binary
# validates its license at RUN time too (a missing key is exit 77), which
# keeps a plain `docker run <image>` working; compose's `environment:` still
# overrides it, so a different key needs no rebuild.
ARG CLOAKBROWSER_LICENSE_KEY
ENV CLOAKBROWSER_LICENSE_KEY=${CLOAKBROWSER_LICENSE_KEY}
RUN python -c "from cloakbrowser.download import ensure_binary; ensure_binary()"

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
# copied above them invalidates the 119MB pip layer and the 337MB
# ensure_binary download on every edit (~2.5 min of a 3 min build).
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
