import subprocess
import sys
from pathlib import Path


def test_real_wrapper_geoip_and_webrtc_contracts():
    program = '''
import asyncio
import inspect
import threading
from unittest.mock import patch
from cloakbrowser import launch_persistent_context_async
from cloakbrowser.browser import _append_webrtc_exit_ip
from backend.browser_manager import _resolve_geoip, _resolve_webrtc_auto

async def verify():
    event_loop_thread = threading.get_ident()
    lookup_threads = []
    def resolve_geo(proxy):
        lookup_threads.append(threading.get_ident())
        return "America/New_York", "en-US", "203.0.113.7"
    def resolve_exit(proxy):
        lookup_threads.append(threading.get_ident())
        return "203.0.113.7"
    proxy = "http://127.0.0.1:9999"
    with patch("cloakbrowser.geoip.resolve_proxy_geo_with_ip", resolve_geo):
        assert await _resolve_geoip("test-profile", proxy, None, None, []) == (
            "America/New_York", "en-US", "203.0.113.7"
        )
    with patch("cloakbrowser.geoip.resolve_proxy_exit_ip", resolve_exit):
        assert await _resolve_geoip("test-profile", proxy, "UTC", "en-GB", []) == (
            "UTC", "en-GB", "203.0.113.7"
        )
        flags = await _resolve_webrtc_auto(
            ["--fingerprint-webrtc-ip=auto", "--fingerprint-webrtc-ip=auto"], proxy, None
        )
        assert flags == ["--fingerprint-webrtc-ip=203.0.113.7"]
    assert lookup_threads and all(thread != event_loop_thread for thread in lookup_threads)
    with patch("cloakbrowser.geoip.resolve_proxy_exit_ip", side_effect=RuntimeError("test")):
        assert await _resolve_webrtc_auto(["--fingerprint-webrtc-ip=auto"], proxy, None) == []
    assert _append_webrtc_exit_ip([], "203.0.113.7") == ["--fingerprint-webrtc-ip=203.0.113.7"]
    explicit = ["--fingerprint-webrtc-ip=198.51.100.2"]
    assert _append_webrtc_exit_ip(explicit, "203.0.113.7") == explicit
    explicit_parameters = {
        "user_data_dir", "headless", "proxy", "args", "timezone", "locale",
        "humanize", "human_preset", "geoip", "color_scheme", "extension_paths",
        "license_key", "release_channel",
    }
    assert explicit_parameters <= set(inspect.signature(launch_persistent_context_async).parameters)

asyncio.run(verify())
'''
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
