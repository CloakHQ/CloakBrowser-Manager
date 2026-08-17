from pathlib import Path

import pytest

from backend.automation import (
    AutomationPolicyError,
    cookie_payloads,
    is_allowed_url,
    normalize_origin,
    resolve_upload_path,
)
from backend.models import AutomationBootstrapRequest


def test_navigation_policy_requires_exact_origin():
    allowed = frozenset({normalize_origin("https://www.instagram.com")})
    assert is_allowed_url("https://www.instagram.com/accounts/login/", allowed)
    assert not is_allowed_url("https://www.instagram.com.evil.test/", allowed)
    assert not is_allowed_url("file:///etc/passwd", allowed)


def test_cookie_payloads_drop_unrelated_domains():
    request = AutomationBootstrapRequest(
        start_url="https://www.instagram.com/",
        allowed_origins=["https://www.instagram.com"],
        cookies=[
            {"name": "sessionid", "value": "safe", "domain": ".instagram.com"},
            {"name": "foreign", "value": "unsafe", "domain": ".evil.test"},
        ],
    )
    result = cookie_payloads(request, frozenset({"https://www.instagram.com"}))
    assert [cookie["name"] for cookie in result] == ["sessionid"]


def test_upload_path_must_be_inside_shared_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    video = root / "clip.mp4"
    video.write_bytes(b"video")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    monkeypatch.setenv("AUTOMATION_UPLOAD_DIR", str(root))

    assert resolve_upload_path(str(video)) == video.resolve()
    with pytest.raises(AutomationPolicyError):
        resolve_upload_path(str(outside))
