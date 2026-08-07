#!/usr/bin/env python3
# Vendored verbatim from CloakHQ/CloakBrowser@c1dd58902a26032b24e7ca821b04800831f3d5c8
# (bin/fetch-widevine.py, 2026-08-06) instead of curled at build/run time: it is
# small, self-contained (stdlib + the `cryptography` package cloakbrowser already
# depends on), and this way a GitHub outage or an upstream edit can never affect
# our build or a container's boot. Re-sync by hand when bumping the cloakbrowser
# pin in backend/requirements.txt if CloakHQ changes this script.
#
# entrypoint.sh runs this in the BACKGROUND on every boot (never blocking nginx/
# uvicorn startup or the healthcheck) so every profile gets Widevine DRM
# automatically: cloakbrowser's own launch_persistent_context_async() already
# seeds the CDM hint file into each profile on first launch (widevine.py) — the
# only thing missing without this script is the CDM binary itself, which this
# fetches straight from Google's component-update server (the same source
# Chrome uses) into CLOAKBROWSER_CACHE_DIR/WidevineCdm, which is
# /data/cloakbrowser/WidevineCdm here (see Dockerfile) — on the persistent
# volume, fetched once ever, not once per container.
"""Fetch the Widevine CDM from Google's component-update server (Linux).

The CloakBrowser binary is built with Widevine support, but the CDM itself is a
proprietary Google component we don't redistribute. This pulls it at runtime from
the same component server Chrome uses, then drops it where the wrapper's
``CLOAKBROWSER_WIDEVINE_CDM`` resolution (cloakbrowser/widevine.py) expects it:

    <out>/manifest.json
    <out>/_platform_specific/linux_<arch>/libwidevinecdm.so

No curl/jq/unzip needed. Linux x86-64 only (Google doesn't publish the CDM for
linux arm64). The Docker entrypoint runs this when CLOAKBROWSER_FETCH_WIDEVINE is
set; bare-metal Linux users can run it directly.

Integrity: the download is checked against the server-provided SHA-256 (over TLS).
When `cryptography` is importable (it is in any pip/Docker install of cloakbrowser),
the CRX3 publisher signature is additionally verified and bound to the expected
Widevine app id — same trust root Chrome's component updater uses. Standalone runs
without `cryptography` fall back to TLS + SHA-256.
"""

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import struct
import sys
import tempfile
import urllib.request
import zipfile

# Widevine CDM component id in Chromium's component updater.
APP_ID = "oimompecagnajdejgnnjijobebaeigek"
UPDATE_URL = "https://update.googleapis.com/service/update2/json"
# Deliberately-low installed version so the server always reports an update.
INSTALLED_VERSION = "1.4.9.1088"
XSSI_PREFIX = ")]}'"


def _arch():
    """Map the host machine to the Widevine platform suffix (x86-64 only).

    Google's component server publishes the Linux Widevine CDM for x86-64 only —
    arm64/aarch64 return no update (verified: the server either reports noupdate
    or hands back the x86-64 binary), so reject them with a clear message rather
    than letting the request reach the misleading "no update available" path.
    """
    m = platform.machine().lower()
    if m in ("x86_64", "amd64", "x64"):
        return "x64"
    if m in ("aarch64", "arm64", "arm"):
        raise SystemExit("the Widevine CDM is not published for linux arm64 (x86-64 only)")
    raise SystemExit(f"unsupported architecture for Widevine: {platform.machine()!r}")


def _read_varint(b, i):
    shift = result = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        byte = b[i]; i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _parse_pb(b):
    """Minimal protobuf reader → {field_num: [length-delimited bytes, ...]}."""
    out, i, n = {}, 0, len(b)
    while i < n:
        tag, i = _read_varint(b, i)
        field, wire = tag >> 3, tag & 7
        if wire == 2:
            ln, i = _read_varint(b, i)
            out.setdefault(field, []).append(b[i:i + ln]); i += ln
        elif wire == 0:
            _, i = _read_varint(b, i)
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
    return out


def _crx_appid(pubkey_der):
    """CRX app id = first 16 bytes of SHA-256(pubkey), each nibble mapped a–p."""
    digest = hashlib.sha256(pubkey_der).digest()[:16]
    return "".join(chr(0x61 + (byte >> 4)) + chr(0x61 + (byte & 0xF)) for byte in digest), digest


def _verify_crx3(crx_bytes):
    """Verify the CRX3 RSA publisher signature and bind it to APP_ID.

    Returns True if verified, False if `cryptography` is unavailable (caller then
    relies on TLS + the server SHA-256). Raises SystemExit on a real failure.
    We verify the RSASSA-PKCS1-v1_5 / SHA-256 proof (CRX3 field 2), which is what
    Google signs the Widevine component with; ECDSA proofs (field 3) are not
    relied on. The app id is derived from the signing key — the same trust root
    Chrome verifies — so a non-Widevine publisher key can't satisfy the check.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        return False

    if len(crx_bytes) < 12:
        raise SystemExit("not a CRX3 file (too short)")
    if crx_bytes[:4] != b"Cr24":
        raise SystemExit("not a CRX3 file (bad magic)")
    version = struct.unpack("<I", crx_bytes[4:8])[0]
    if version != 3:
        raise SystemExit(f"unexpected CRX version {version}")
    header_len = struct.unpack("<I", crx_bytes[8:12])[0]
    header = crx_bytes[12:12 + header_len]
    archive = crx_bytes[12 + header_len:]

    fields = _parse_pb(header)
    signed_header = fields.get(10000, [b""])[0]
    # Signed payload: "CRX3 SignedData\x00" + uint32LE(len) + signed_header + archive
    payload = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed_header)) + signed_header + archive
    declared_id = _parse_pb(signed_header).get(1, [b""])[0]  # SignedData.crx_id

    for proof in fields.get(2, []):  # sha256_with_rsa proofs
        p = _parse_pb(proof)
        pub_der, sig = p.get(1, [None])[0], p.get(2, [None])[0]
        if not pub_der or not sig:
            continue
        appid, digest16 = _crx_appid(pub_der)
        if appid != APP_ID:
            continue  # not the Widevine publisher key — ignore
        if declared_id and declared_id != digest16:
            raise SystemExit("CRX signed-header crx_id does not match the signing key")
        try:
            serialization.load_der_public_key(pub_der).verify(
                sig, payload, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            raise SystemExit("CRX3 publisher signature is INVALID")
        return True
    raise SystemExit("no CRX3 RSA proof from the expected Widevine publisher key")


def _post_json(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", "replace")
    if body.startswith(XSSI_PREFIX):
        body = body[len(XSSI_PREFIX):]
    return json.loads(body)


def _resolve_crx(arch):
    """Query the component server; return (version, crx_url, sha256_hex)."""
    payload = {"request": {
        "@os": "", "@updater": "",
        "acceptformat": "crx3,download,puff,run,xz,zucc",
        "apps": [{"appid": APP_ID, "installsource": "ondemand",
                  "updatecheck": {}, "version": INSTALLED_VERSION}],
        "dedup": "cr", "ismachine": False, "arch": arch,
        "os": {"arch": arch, "platform": "linux"},
        "protocol": "4.0", "updaterversion": "142.0.7444.175",
    }}
    resp = _post_json(UPDATE_URL, payload)
    uc = resp["response"]["apps"][0]["updatecheck"]
    status = uc.get("status")
    if status and status != "ok":
        raise SystemExit(f"component server returned status={status!r} (no update available)")
    version = uc.get("nextversion", "?")
    # Find the first operation that carries download URLs + its sha256.
    for pipeline in uc.get("pipelines", []):
        for op in pipeline.get("operations", []):
            urls = [u["url"] for u in op.get("urls", []) if u.get("url", "").startswith("https")]
            if urls:
                sha = (op.get("out") or {}).get("sha256")
                return version, urls[0], sha
    raise SystemExit("no CRX download URL in component server response")


def _download(url, sha256_hex):
    with urllib.request.urlopen(url, timeout=120) as resp:
        blob = resp.read()
    # Integrity: server-provided SHA-256 over TLS (always). The CRX3 publisher
    # signature is additionally verified in main() when `cryptography` is present.
    if sha256_hex:
        got = hashlib.sha256(blob).hexdigest()
        if got.lower() != sha256_hex.lower():
            raise SystemExit(f"sha256 mismatch: expected {sha256_hex}, got {got}")
    return blob


def _extract(crx_bytes, arch, out_dir):
    """Extract manifest.json + the .so into out_dir, replacing any prior copy.

    Staged in a temp dir then renamed into place — the rename is atomic, but the
    rmtree of an existing out_dir that precedes it is not, so this is not safe
    against another process writing the same out_dir concurrently.
    """
    so_member = f"_platform_specific/linux_{arch}/libwidevinecdm.so"
    # zipfile locates the central directory from the end, so a CRX3 (header+zip)
    # opens directly without stripping the prefix.
    with zipfile.ZipFile(io.BytesIO(crx_bytes)) as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names or so_member not in names:
            raise SystemExit(f"CRX missing expected members (manifest.json / {so_member})")
        parent = os.path.dirname(os.path.abspath(out_dir)) or "."
        os.makedirs(parent, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".widevine.tmp.", dir=parent)
        try:
            zf.extract("manifest.json", tmp)
            zf.extract(so_member, tmp)
            os.chmod(os.path.join(tmp, so_member), 0o644)
            # Swap into place. The rename is atomic; the preceding rmtree is not.
            if os.path.exists(out_dir):
                shutil.rmtree(out_dir)
            os.rename(tmp, out_dir)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise


def _default_out():
    cache = os.environ.get("CLOAKBROWSER_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cloakbrowser")
    return os.path.join(cache, "WidevineCdm")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch the Widevine CDM for CloakBrowser (Linux).")
    ap.add_argument("--out", default=_default_out(),
                    help="WidevineCdm output directory (default: $CLOAKBROWSER_CACHE_DIR/WidevineCdm)")
    ap.add_argument("--force", action="store_true", help="re-download even if already present")
    ap.add_argument("--quiet", action="store_true", help="only print the final path / errors")
    args = ap.parse_args(argv)

    def log(msg):
        if not args.quiet:
            print(f"[fetch-widevine] {msg}", file=sys.stderr)

    # Linux only: the hint-file mechanism is Linux/ChromeOS-specific and the .so
    # we fetch is a Linux binary. Fail loudly rather than drop a useless .so.
    if platform.system() != "Linux":
        raise SystemExit(f"Widevine fetch is Linux-only (this host is {platform.system()})")

    out = os.path.abspath(args.out)
    if os.path.isfile(os.path.join(out, "manifest.json")) and not args.force:
        log("already present (cache hit)")
        print(out)
        return 0

    arch = _arch()
    log(f"querying component server (linux {arch})…")
    version, url, sha = _resolve_crx(arch)
    log(f"Widevine CDM {version} → downloading…")
    blob = _download(url, sha)  # raises on a SHA-256 mismatch when `sha` is present

    # Integrity policy: require at least one positive check before installing a
    # native .so the browser will load. The CRX3 publisher signature (when
    # `cryptography` is available — it is in any pip/Docker install of cloakbrowser)
    # is the primary guarantee; the server-provided SHA-256 over TLS is the
    # fallback. The server can legitimately omit `out.sha256`, so don't treat its
    # presence as given — if neither check is available, refuse rather than trust
    # TLS alone.
    sig_ok = _verify_crx3(blob)
    if sig_ok and sha:
        log(f"verified {len(blob)} bytes (SHA-256 + CRX3 publisher signature)")
    elif sig_ok:
        log(f"verified {len(blob)} bytes (CRX3 publisher signature; server sent no SHA-256)")
    elif sha:
        log(f"verified {len(blob)} bytes (SHA-256 over TLS; cryptography absent, CRX3 sig skipped)")
    else:
        raise SystemExit(
            "refusing to install: server provided no SHA-256 and `cryptography` is "
            "unavailable for CRX3 signature verification — cannot confirm CDM integrity"
        )
    log(f"extracting → {out}")
    _extract(blob, arch, out)
    log("done")
    print(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — top-level guard; entrypoint treats nonzero as soft-fail
        print(f"[fetch-widevine] error: {e}", file=sys.stderr)
        sys.exit(1)
