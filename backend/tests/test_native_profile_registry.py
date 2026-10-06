import json

from backend import database as db


def test_sync_native_profiles_creates_and_updates(tmp_db, tmp_path):
    registry = tmp_path / "native-profiles.json"
    registry.write_text(json.dumps([{
        "native_profile": "google-001",
        "name": "GOOGLE 1",
        "start_urls": ["https://accounts.google.com/"],
        "notes": "First",
        "tags": [{"tag": "google"}],
    }]))

    assert db.sync_native_profiles(registry) == 1
    profile = db.list_profiles()[0]
    assert profile["name"] == "GOOGLE 1"
    assert profile["launch_args"] == [
        "--native-profile=google-001",
        "--start-url=https://accounts.google.com/",
    ]

    data = json.loads(registry.read_text())
    data[0]["name"] = "GOOGLE 1 — Updated"
    registry.write_text(json.dumps(data))
    db.sync_native_profiles(registry)

    profiles = db.list_profiles()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "GOOGLE 1 — Updated"
