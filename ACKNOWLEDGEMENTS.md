# Acknowledgements

CloakBrowser Manager's own source is MIT-licensed (see [LICENSE](LICENSE)).
The Docker images it builds also install or vendor the following third-party
components, unmodified, under their own licenses.

## KasmVNC — GPL-2.0

The image installs the official `kasmvncserver` `.deb` package (version
1.5.0, SHA256-pinned per architecture in [`Dockerfile`](Dockerfile)),
unmodified, from [kasmtech/KasmVNC](https://github.com/kasmtech/KasmVNC).
KasmVNC is licensed under the GNU General Public License, version 2
("GPLv2").

Installing this GPLv2 binary into a Docker image that is itself distributed
is distribution of the Program in object-code form under GPLv2 §3, which
requires that recipients also be given (or offered) the corresponding
source code. That source is unmodified upstream source, publicly available
at the exact pinned version:

- Source: https://github.com/kasmtech/KasmVNC/tree/v1.5.0
- Release/binaries: https://github.com/kasmtech/KasmVNC/releases/tag/v1.5.0
- Full license text: https://github.com/kasmtech/KasmVNC/blob/v1.5.0/LICENSE.TXT (also reproduced in the `.deb` at `/usr/share/doc/kasmvncserver/`)

KasmVNC in turn bundles other third-party components (zlib, TigerVNC, etc.)
under their own licenses — see KasmVNC's own
[ACKNOWLEDGEMENTS.md](https://github.com/kasmtech/KasmVNC/blob/v1.5.0/ACKNOWLEDGEMENTS.md).

## cloudflared — Apache License 2.0

The optional Cloudflare Tunnel sidecar image
([`docker/Dockerfile.tunnel`](docker/Dockerfile.tunnel)) copies the
`cloudflared` binary, unmodified, out of the official
`cloudflare/cloudflared` image. cloudflared is licensed under the
[Apache License, Version 2.0](https://github.com/cloudflare/cloudflared/blob/master/LICENSE).

- Source: https://github.com/cloudflare/cloudflared

## CloakBrowser (Chromium) — proprietary, separately licensed

The CloakBrowser Chromium binary is **not** baked into the image at build
time — it is downloaded at container launch (see the "License" section of
[README.md](README.md)) directly by the user's own environment/license key,
so this repository never builds or distributes that binary itself. Its use
is governed by [BINARY-LICENSE.md](BINARY-LICENSE.md). CloakBrowser is
itself built on Chromium (BSD 3-Clause, The Chromium Authors) and
incorporates [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) —
see BINARY-LICENSE.md for details.

## Windows core fonts — Microsoft EULA

The image installs Debian's `ttf-mscorefonts-installer`, which downloads
Microsoft's TrueType core fonts (Arial, Times New Roman, Verdana, etc.)
under Microsoft's own End User License Agreement, accepted non-interactively
at build time via `debconf-set-selections` in `Dockerfile`. These fonts are
not open source and are not covered by this repository's MIT license.

## Everything else

All other build- and run-time dependencies (FastAPI, uvicorn, Pydantic,
httpx, websockets, React, Vite, Tailwind, etc., see
[`backend/requirements.txt`](backend/requirements.txt) and
[`frontend/package.json`](frontend/package.json)) are used unmodified under
their own permissive (MIT/BSD/Apache-2.0) licenses, as published by each
project.
