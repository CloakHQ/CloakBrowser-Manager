"""The proxy-speed badge's data: read the records, judge against the expectation, never touch a browser."""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from backend import native_health

EXPECT = {"kbps": 100, "tls_sec": 2.5}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "cloak"
    (home / "profiles" / "facebook" / "facebook-013").mkdir(parents=True)
    (home / "profiles" / "facebook" / "facebook-014").mkdir(parents=True)
    return home


def _record(home: Path, name: str, ref: str, exit_info: dict | None = None, timezone: str | None = None) -> None:
    path = home / "profiles" / name.split("-")[0] / name / "cloak.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    browser: dict = {"proxy": {"ref": ref}}
    if timezone:
        browser["timezone"] = timezone
    path.write_text(json.dumps({"browser": browser, "history": {"exit": exit_info or {}}}))


def _registry(home: Path, entries: dict) -> None:
    (home / "proxies.json").write_text(json.dumps(entries))


def _use(tmp_path: Path, monkeypatch) -> Path:
    home = _home(tmp_path)
    monkeypatch.setattr(native_health, "CLOAK_HOME", home)
    monkeypatch.setattr(native_health, "PROFILES_ROOT", home / "profiles")
    return home


def _profile(name: str, status: str = "running") -> dict:
    return {"id": "id-" + name, "status": status, "launch_args": [f"--native-profile={name}"]}


def test_a_slow_line_is_named_slow_and_scored_against_the_expectation(tmp_path, monkeypatch):
    home = _use(tmp_path, monkeypatch)
    _record(home, "facebook-013", "px-013", {"ip": "107.77.215.36", "country": "US", "city": "Gainesville", "timezone": "America/New_York", "family": "v6", "drifted": ["v4"]})
    _registry(home, {"px-013": {"server": "http://gw:10080", "ip": "107.77.215.36", "country": "US", "city": "Gainesville", "network": "AT&T", "speed": {"ok": True, "kbps": 12, "tlsSec": 0.86}, "lastScore": {"at": "2026-10-26T15:00:00Z", "verdict": "green"}}})

    out = native_health.proxy_health([_profile("facebook-013")])
    assert out["expectation"] == EXPECT
    row = out["profiles"][0]
    assert row["profile"] == "facebook-013"
    assert row["kbps"] == 12
    assert row["ratio"] == 0.12
    assert row["verdict"] == "slow"
    assert row["family"] == "v6"
    assert row["drifted"] == ["v4"]
    assert row["network"] == "AT&T"
    assert row["city"] == "Gainesville"
    assert row["country"] == "US"
    assert row["timezone"] == "America/New_York"


def test_a_fast_line_is_good_and_a_slow_handshake_is_its_own_verdict(tmp_path, monkeypatch):
    home = _use(tmp_path, monkeypatch)
    _record(home, "facebook-013", "px-013")
    _record(home, "facebook-014", "px-014")
    _registry(home, {
        "px-013": {"speed": {"ok": True, "kbps": 140, "tlsSec": 0.8}},
        "px-014": {"speed": {"ok": True, "kbps": 130, "tlsSec": 3.1}},
    })

    out = native_health.proxy_health([_profile("facebook-013"), _profile("facebook-014")])
    by = {r["profile"]: r for r in out["profiles"]}
    assert by["facebook-013"]["verdict"] == "good"
    assert by["facebook-014"]["verdict"] == "handshake"


def test_an_unmeasured_line_says_so_instead_of_looking_healthy(tmp_path, monkeypatch):
    home = _use(tmp_path, monkeypatch)
    _record(home, "facebook-013", "px-013")
    _registry(home, {"px-013": {"server": "http://gw:10080"}})

    row = native_health.proxy_health([_profile("facebook-013")])["profiles"][0]
    assert row["verdict"] == "unmeasured"
    assert row["kbps"] is None
    assert row["ratio"] is None


def test_a_profile_without_a_proxy_or_a_native_name_is_omitted(tmp_path, monkeypatch):
    home = _use(tmp_path, monkeypatch)
    _registry(home, {})
    (home / "proxies.json").write_text(json.dumps({}))
    # No cloak.json for facebook-014, and a row that is not a native profile at all.
    out = native_health.proxy_health([
        _profile("facebook-014"),
        {"id": "docker", "status": "stopped", "launch_args": []},
    ])
    assert out["profiles"] == []


def test_the_worst_line_comes_first(tmp_path, monkeypatch):
    home = _use(tmp_path, monkeypatch)
    _record(home, "facebook-013", "px-013")
    _record(home, "facebook-014", "px-014")
    _registry(home, {
        "px-013": {"speed": {"ok": True, "kbps": 140, "tlsSec": 0.8}},
        "px-014": {"speed": {"ok": True, "kbps": 14, "tlsSec": 0.9}},
    })
    rows = native_health.proxy_health([_profile("facebook-013"), _profile("facebook-014")])["profiles"]
    assert [r["profile"] for r in rows] == ["facebook-014", "facebook-013"]


def test_the_endpoint_answers_with_the_expectation(app_client: TestClient):
    resp = app_client.get("/api/native/proxy-health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["expectation"]["kbps"] == 100
    assert body["expectation"]["tls_sec"] == 2.5
    assert isinstance(body["profiles"], list)


def test_the_exit_place_is_compared_with_the_clock_the_profile_expects(tmp_path, monkeypatch):
    home = _use(tmp_path, monkeypatch)
    _record(home, "facebook-013", "px-013", {"country": "US", "city": "Gainesville", "timezone": "America/New_York"}, timezone="America/New_York")
    _record(home, "facebook-014", "px-014", {"country": "FR", "city": "Paris", "timezone": "Europe/Paris"}, timezone="America/New_York")
    _registry(home, {"px-013": {"speed": {"ok": True, "kbps": 140, "tlsSec": 0.8}}, "px-014": {"speed": {"ok": True, "kbps": 140, "tlsSec": 0.8}}})

    rows = {r["profile"]: r for r in native_health.proxy_health([_profile("facebook-013"), _profile("facebook-014")])["profiles"]}
    assert rows["facebook-013"]["place_match"] is True
    assert rows["facebook-014"]["place_match"] is False
    assert rows["facebook-014"]["timezone"] == "Europe/Paris"
    assert rows["facebook-014"]["profile_timezone"] == "America/New_York"
