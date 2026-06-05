"""Tests for local script discovery and API-triggered runs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from backend import main
from backend.browser_manager import RunningProfile
from backend.script_runner import ScriptRun, ScriptRunner


def test_list_scripts_discovers_visible_parameters():
    runner = ScriptRunner()

    like_script = runner.get_script("facebook_like_feed")
    like_params = {param.name: param for param in like_script.parameters}
    assert like_script.profile_required is True
    assert "cdp_url" not in like_params
    assert like_params["count"].value_type == "integer"
    assert like_params["count"].default == 10
    assert like_params["dry_run"].kind == "flag"

    comment_script = runner.get_script("facebook_comment_feed")
    comment_params = {param.name: param for param in comment_script.parameters}
    assert comment_params["comments_file"].kind == "positional"
    assert comment_params["comments_file"].required is True


def test_build_command_injects_direct_cdp_url():
    runner = ScriptRunner()

    command = runner.build_command(
        script_id="facebook_like_feed",
        profile_id="profile-1",
        cdp_port=5111,
        manager_host="http://manager.local",
        arguments={"count": 3, "dry_run": True},
    )

    assert "--cdp-url" in command
    assert "http://127.0.0.1:5111" in command
    assert "--profile-id" not in command
    assert "--host" not in command
    assert command[-3:] == ["--count", "3", "--dry-run"]


def test_build_command_requires_positional_arguments():
    runner = ScriptRunner()

    with pytest.raises(ValueError, match="comments_file"):
        runner.build_command(
            script_id="facebook_comment_feed",
            profile_id="profile-1",
            cdp_port=5111,
            manager_host="http://manager.local",
            arguments={},
        )


def test_list_scripts_api(app_client: TestClient):
    resp = app_client.get("/api/scripts")

    assert resp.status_code == 200
    scripts = {script["id"]: script for script in resp.json()}
    assert "facebook_like_feed" in scripts
    assert "facebook_comment_feed" in scripts
    assert scripts["facebook_like_feed"]["profile_required"] is True


def test_start_script_run_requires_running_profile(app_client: TestClient):
    create = app_client.post("/api/profiles", json={"name": "Stopped"})
    pid = create.json()["id"]

    resp = app_client.post(
        "/api/scripts/facebook_like_feed/runs",
        json={"profile_id": pid, "arguments": {"count": 1}},
    )

    assert resp.status_code == 409
    assert "running" in resp.json()["detail"]


def test_start_script_run_passes_profile_cdp_port(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    create = app_client.post("/api/profiles", json={"name": "Running"})
    pid = create.json()["id"]

    mock_running = MagicMock(spec=RunningProfile)
    mock_running.display = 100
    mock_running.ws_port = 6100
    mock_running.cdp_port = 5123
    mock_running.profile_id = pid
    main.browser_mgr.running[pid] = mock_running

    mock_run = ScriptRun(
        id="run-1",
        script_id="facebook_like_feed",
        script_name="Facebook Like Feed",
        profile_id=pid,
        profile_name="Running",
        status="running",
        started_at=1.0,
        command=["python", "script.py"],
        log="started",
    )
    start_run = AsyncMock(return_value=mock_run)
    monkeypatch.setattr(main.script_mgr, "start_run", start_run)

    resp = app_client.post(
        "/api/scripts/facebook_like_feed/runs",
        json={"profile_id": pid, "arguments": {"count": 1}},
    )

    assert resp.status_code == 201
    assert resp.json()["id"] == "run-1"
    start_run.assert_awaited_once()
    kwargs = start_run.await_args.kwargs
    assert kwargs["profile_id"] == pid
    assert kwargs["profile_name"] == "Running"
    assert kwargs["cdp_port"] == 5123
    assert kwargs["arguments"] == {"count": 1}

    main.browser_mgr.running.pop(pid, None)
