"""Tests for per-profile file artifacts (upload / list / download / delete)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from unittest.mock import MagicMock
from starlette.testclient import TestClient

from backend import artifacts
from backend import database as db
from backend import main


def _upload(
    client: TestClient, pid: str, name: str = "report.csv", data: bytes = b"a,b\n1,2\n",
    content_type: str = "text/csv",
):
    return client.post(
        f"/api/profiles/{pid}/files", content=data,
        headers={"X-File-Name": quote(name), "Content-Type": content_type},
    )


def _new_profile(client: TestClient, name: str = "Files") -> str:
    return client.post("/api/profiles", json={"name": name}).json()["id"]


# ── safe_filename ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("report.csv", "report.csv"),
    ("../../etc/passwd", "passwd"),
    ("..\\..\\windows\\system32\\cfg", "cfg"),
    ("/absolute/path.xlsx", "path.xlsx"),
    ("", "file"),
    ("..", "file"),
    (".", "file"),
    (None, "file"),
    ("with\x00null.csv", "withnull.csv"),
])
def test_safe_filename_neutralises_paths(raw, expected):
    assert artifacts.safe_filename(raw) == expected


def test_safe_filename_caps_length():
    assert len(artifacts.safe_filename("x" * 500)) == 255


# ── upload ───────────────────────────────────────────────────────────────────


def test_upload_stores_the_file_and_reports_its_container_path(app_client: TestClient, tmp_db: Path):
    pid = _new_profile(app_client)
    resp = _upload(app_client, pid, "weekly report.csv", b"payload")
    assert resp.status_code == 201
    art = resp.json()
    assert art["name"] == "weekly report.csv"
    assert art["size"] == len("payload")
    assert art["kind"] == "upload" and art["state"] == "ready"
    # The page is handed the real filename, so a portal that checks the extension accepts it.
    path = Path(art["container_path"])
    assert path.name == "weekly report.csv"
    assert path.read_bytes() == b"payload"


def test_uploads_live_outside_the_profile_user_data_dir(app_client: TestClient, tmp_db: Path):
    """The whole point of a separate artifact root: browser-state copies must not carry these."""
    pid = _new_profile(app_client)
    art = _upload(app_client, pid).json()
    user_data_dir = Path(app_client.get(f"/api/profiles/{pid}").json()["user_data_dir"])
    assert user_data_dir not in Path(art["container_path"]).parents


def test_upload_rejects_unknown_profile(app_client: TestClient):
    assert _upload(app_client, "nonexistent").status_code == 404


def test_upload_filename_cannot_escape_the_artifact_directory(app_client: TestClient, tmp_db: Path):
    pid = _new_profile(app_client)
    art = _upload(app_client, pid, "../../../../etc/passwd", b"nope").json()
    stored = Path(art["container_path"])
    assert stored.name == "passwd"
    assert artifacts.artifacts_root() in stored.parents
    assert stored.read_bytes() == b"nope"


def test_upload_over_the_size_cap_is_rejected_and_leaves_nothing(
    app_client: TestClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 8)
    pid = _new_profile(app_client)
    assert _upload(app_client, pid, "big.csv", b"x" * 64).status_code == 413
    assert app_client.get(f"/api/profiles/{pid}/files").json() == []
    leftovers = list(artifacts.artifact_dir(pid).rglob("*")) if artifacts.artifact_dir(pid).exists() else []
    assert [p for p in leftovers if p.is_file()] == []


def test_upload_over_the_count_quota_is_rejected(
    app_client: TestClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACTS_PER_PROFILE", 1)
    pid = _new_profile(app_client)
    assert _upload(app_client, pid, "one.csv").status_code == 201
    assert _upload(app_client, pid, "two.csv").status_code == 507


# ── list / download / delete ─────────────────────────────────────────────────


def test_list_and_download_round_trip(app_client: TestClient, tmp_db: Path):
    pid = _new_profile(app_client)
    payload = b"store_id,orders\n23197600,142\n"
    art = _upload(app_client, pid, "weekly report.csv", payload).json()

    listing = app_client.get(f"/api/profiles/{pid}/files").json()
    assert [a["id"] for a in listing] == [art["id"]]

    resp = app_client.get(f"/api/profiles/{pid}/files/{art['id']}")
    assert resp.status_code == 200
    assert resp.content == payload
    # Merchant documents are never rendered on the Manager's own origin.
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["content-type"] == "application/octet-stream"
    assert "attachment" in resp.headers["content-disposition"]
    assert "weekly%20report.csv" in resp.headers["content-disposition"]


def test_download_unknown_artifact_404(app_client: TestClient, tmp_db: Path):
    pid = _new_profile(app_client)
    assert app_client.get(f"/api/profiles/{pid}/files/nope").status_code == 404


def test_artifact_of_another_profile_is_not_reachable(app_client: TestClient, tmp_db: Path):
    owner, other = _new_profile(app_client, "Owner"), _new_profile(app_client, "Other")
    art = _upload(app_client, owner).json()
    assert app_client.get(f"/api/profiles/{other}/files/{art['id']}").status_code == 404
    assert app_client.delete(f"/api/profiles/{other}/files/{art['id']}").status_code == 404


def test_delete_removes_row_and_bytes(app_client: TestClient, tmp_db: Path):
    pid = _new_profile(app_client)
    art = _upload(app_client, pid).json()
    assert app_client.delete(f"/api/profiles/{pid}/files/{art['id']}").status_code == 200
    assert app_client.get(f"/api/profiles/{pid}/files").json() == []
    assert app_client.get(f"/api/profiles/{pid}/files/{art['id']}").status_code == 404
    assert not Path(art["container_path"]).exists()
    assert app_client.delete(f"/api/profiles/{pid}/files/{art['id']}").status_code == 404


# ── lifecycle interactions ───────────────────────────────────────────────────


def test_deleting_a_profile_removes_its_artifacts(app_client: TestClient, tmp_db: Path):
    pid = _new_profile(app_client)
    art = _upload(app_client, pid).json()
    assert app_client.delete(f"/api/profiles/{pid}").status_code == 200
    assert not Path(art["container_path"]).exists()
    assert not artifacts.artifact_dir(pid).exists()
    assert db.list_artifacts(pid) == []


def test_duplicate_with_browser_state_does_not_carry_artifacts(app_client: TestClient, tmp_db: Path):
    """Merchant documents must not be silently cloned into a copy of a profile."""
    pid = _new_profile(app_client, "Src")
    art = _upload(app_client, pid, "confidential.csv", b"secret").json()
    user_data_dir = Path(app_client.get(f"/api/profiles/{pid}").json()["user_data_dir"])
    (user_data_dir / "Default").mkdir(parents=True)
    (user_data_dir / "Default" / "Cookies").write_text("session=abc")

    clone = app_client.post(
        f"/api/profiles/{pid}/duplicate", json={"include_browser_state": True}
    ).json()

    # The session travels with the clone; the merchant's report does not.
    assert (Path(clone["user_data_dir"]) / "Default" / "Cookies").read_text() == "session=abc"
    assert app_client.get(f"/api/profiles/{clone['id']}/files").json() == []
    assert not artifacts.artifact_dir(clone["id"]).exists()
    assert Path(art["container_path"]).read_bytes() == b"secret"


# ── file-chooser convenience view ────────────────────────────────────────────


def test_upload_appears_under_its_real_name_in_the_picker_view(app_client: TestClient, tmp_db: Path, docker_runtime):
    """A page's native 'Choose File' dialog must be able to reach the file by name."""
    pid = _new_profile(app_client)
    art = _upload(app_client, pid, "weekly report.csv", b"payload").json()
    link = artifacts.picker_dir(pid) / "weekly report.csv"
    assert link.is_symlink()
    assert link.resolve() == Path(art["container_path"]).resolve()
    assert link.read_bytes() == b"payload"


def test_picker_view_disambiguates_duplicate_names(app_client: TestClient, tmp_db: Path, docker_runtime):
    pid = _new_profile(app_client)
    _upload(app_client, pid, "report.csv", b"one")
    _upload(app_client, pid, "report.csv", b"two")
    names = sorted(p.name for p in artifacts.picker_dir(pid).iterdir())
    assert names == ["report (1).csv", "report.csv"]


def test_picker_view_drops_a_deleted_file(app_client: TestClient, tmp_db: Path, docker_runtime):
    pid = _new_profile(app_client)
    art = _upload(app_client, pid, "gone.csv").json()
    app_client.delete(f"/api/profiles/{pid}/files/{art['id']}")
    assert list(artifacts.picker_dir(pid).iterdir()) == []


def test_the_file_chooser_view_is_docker_only(
    app_client: TestClient, tmp_db: Path, native_runtime
):
    """On a native macOS/Windows install the browser already sees the user's own disk, so
    the view is unnecessary — and creating symlinks there needs a Windows privilege."""
    assert artifacts.picker_view_enabled() is False
    assert artifacts._gtk_bookmarks_path() is None

    pid = _new_profile(app_client)
    art = _upload(app_client, pid, "report.csv", b"payload").json()

    # The file itself is stored and served exactly as on Linux...
    assert Path(art["container_path"]).read_bytes() == b"payload"
    assert app_client.get(f"/api/profiles/{pid}/files/{art['id']}").content == b"payload"
    # ...but no symlink view and no bookmarks file are created.
    assert not artifacts.picker_dir(pid).exists()


def test_gtk_bookmark_points_at_the_picker_view(
    app_client: TestClient, tmp_db: Path, docker_runtime
):
    bookmarks = artifacts._gtk_bookmarks_path()
    assert bookmarks is not None
    pid = _new_profile(app_client, "Work profile")
    _upload(app_client, pid, "menu.csv")
    line = bookmarks.read_text().strip()
    assert line.startswith(f"file://{artifacts.picker_dir(pid)} ")
    assert line.endswith("Work profile")


def test_a_picker_name_keeps_pointing_at_the_document_it_was_given_to(
    app_client: TestClient, tmp_db: Path, docker_runtime):
    """Two files sharing a name must not swap identities when the view is rebuilt."""
    pid = _new_profile(app_client)
    first = _upload(app_client, pid, "report.csv", b"FIRST").json()
    second = _upload(app_client, pid, "report.csv", b"SECOND").json()

    assert first["picker_name"] == "report.csv"
    assert second["picker_name"] == "report (1).csv"
    view = artifacts.picker_dir(pid)
    # The name the first upload was given still resolves to the first upload's bytes.
    assert (view / "report.csv").read_bytes() == b"FIRST"
    assert (view / "report (1).csv").read_bytes() == b"SECOND"

    # ...and still does after an unrelated change forces another sync.
    _upload(app_client, pid, "other.csv", b"OTHER")
    assert (view / "report.csv").read_bytes() == b"FIRST"


def test_deleting_a_file_does_not_rename_its_neighbours(app_client: TestClient, tmp_db: Path, docker_runtime):
    pid = _new_profile(app_client)
    first = _upload(app_client, pid, "report.csv", b"FIRST").json()
    second = _upload(app_client, pid, "report.csv", b"SECOND").json()

    app_client.delete(f"/api/profiles/{pid}/files/{first['id']}")

    view = artifacts.picker_dir(pid)
    assert not (view / "report.csv").exists()
    # The survivor keeps the name it was given rather than being promoted onto the free one.
    assert (view / "report (1).csv").read_bytes() == b"SECOND"
    assert db.get_artifact(pid, second["id"])["picker_name"] == "report (1).csv"


def test_safe_filename_fits_the_filesystem_byte_limit_and_keeps_the_extension():
    # Three bytes per character, so 200 characters is 600 bytes — well over the 255-byte cap.
    name = ("長" * 200) + ".csv"
    fitted = artifacts.safe_filename(name)
    assert len(fitted.encode("utf-8")) <= 255
    assert fitted.endswith(".csv")


def test_only_profiles_holding_files_are_reconciled(app_client: TestClient, tmp_db: Path):
    """The startup repair must not walk every profile on a fleet-sized Manager."""
    with_files = _new_profile(app_client, "Has files")
    _new_profile(app_client, "Empty")
    _upload(app_client, with_files)
    assert db.profile_ids_with_artifacts() == [with_files]


def test_startup_repair_drops_a_picker_link_whose_artifact_is_gone(
    app_client: TestClient, tmp_db: Path, docker_runtime):
    pid = _new_profile(app_client)
    _upload(app_client, pid, "kept.csv")
    stale = artifacts.picker_dir(pid) / "vanished.csv"
    stale.symlink_to(artifacts.artifact_dir(pid) / "nowhere" / "vanished.csv")

    artifacts.sync_picker_view(pid)

    assert not stale.is_symlink()
    assert (artifacts.picker_dir(pid) / "kept.csv").is_symlink()


# ── cross-platform filenames ─────────────────────────────────────────────────
# The Manager ships as a Linux container AND as native macOS/Windows apps, and a /data
# directory written by one can be opened by another, so names must satisfy all of them.


@pytest.mark.parametrize("raw,expected", [
    ('report:2026.csv', "report_2026.csv"),      # ':' is illegal on Windows
    ('a<b>c|d?e*f".csv', "a_b_c_d_e_f_.csv"),
    ("trailing dot..", "trailing dot"),          # Windows silently drops these
    ("trailing space  ", "trailing space"),
    ("NUL", "_NUL"),                             # reserved device names
    ("con.txt", "_con.txt"),
    ("COM1.csv", "_COM1.csv"),
    ("console.csv", "console.csv"),              # only the exact name is reserved
])
def test_safe_filename_satisfies_windows_rules_too(raw, expected):
    assert artifacts.safe_filename(raw) == expected


def test_a_maximum_length_duplicate_name_still_resolves(app_client: TestClient, tmp_db: Path, docker_runtime):
    """Trimming must never regenerate the taken name, or the rename loop never terminates."""
    pid = _new_profile(app_client)
    longest = ("a" * 251) + ".csv"
    assert len(longest.encode()) == 255

    first = _upload(app_client, pid, longest, b"FIRST").json()
    second = _upload(app_client, pid, longest, b"SECOND").json()

    assert first["picker_name"] != second["picker_name"]
    for art in (first, second):
        assert len(art["picker_name"].encode()) <= 255
        assert art["picker_name"].endswith(".csv")
    view = artifacts.picker_dir(pid)
    assert (view / first["picker_name"]).read_bytes() == b"FIRST"
    assert (view / second["picker_name"]).read_bytes() == b"SECOND"


@pytest.mark.parametrize("raw", ["x." + "a" * 300, "x." + "長" * 100, "長" * 400])
def test_safe_filename_never_exceeds_the_byte_budget(raw):
    """An extension can be longer than the whole budget on its own."""
    assert len(artifacts.safe_filename(raw).encode("utf-8")) <= 255


@pytest.mark.parametrize("raw", ["COM¹", "LPT²", "com³"])
def test_safe_filename_covers_the_superscript_device_names(raw):
    assert artifacts.safe_filename(raw).startswith("_")


def test_an_upload_that_cannot_be_recorded_leaves_no_file_behind(
    app_client: TestClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """Bytes with no row could never be listed, deleted, or counted against the quota."""
    pid = _new_profile(app_client)
    monkeypatch.setattr(db, "create_artifact", MagicMock(side_effect=RuntimeError("disk full")))

    resp = _upload(app_client, pid, "doomed.csv", b"payload")

    assert resp.status_code == 500
    assert app_client.get(f"/api/profiles/{pid}/files").json() == []
    leftovers = [p for p in artifacts.artifact_dir(pid).rglob("*") if p.is_file()]
    assert leftovers == []


# ── receipt bound ────────────────────────────────────────────────────────────
# The per-file cap bounds what is STORED; the body is bounded while it arrives as well, since
# nothing is buffered ahead of the endpoint any more.


def test_an_oversized_upload_is_refused_from_its_declared_length_before_a_byte_is_read(
    app_client: TestClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 1024)
    monkeypatch.setattr(artifacts, "save_stream", MagicMock(side_effect=AssertionError("reached")))
    pid = _new_profile(app_client)

    resp = _upload(app_client, pid, "huge.csv", b"x" * (256 * 1024))

    assert resp.status_code == 413
    assert "exceeds" in resp.json()["detail"]
    assert resp.headers.get("connection") == "close"  # the client should stop sending
    assert app_client.get(f"/api/profiles/{pid}/files").json() == []


def test_an_upload_that_would_breach_the_total_is_refused_from_its_declared_length(
    app_client: TestClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(artifacts, "MAX_PROFILE_ARTIFACT_BYTES", 100)
    monkeypatch.setattr(artifacts, "save_stream", MagicMock(side_effect=AssertionError("reached")))
    pid = _new_profile(app_client)

    resp = _upload(app_client, pid, "big.csv", b"x" * 200)

    assert resp.status_code == 507
    assert resp.headers.get("connection") == "close"  # refused unread: the client must stop
    assert app_client.get(f"/api/profiles/{pid}/files").json() == []


def test_a_body_of_unknown_length_is_cut_off_at_the_cap(
    app_client: TestClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """A client may omit or understate Content-Length, so arriving bytes are counted; the
    partial file is removed and no row is written."""
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 16)
    pid = _new_profile(app_client)

    resp = app_client.post(
        f"/api/profiles/{pid}/files",
        content=iter([b"x" * 10, b"x" * 10, b"x" * 10]),  # chunked: no Content-Length
        headers={"X-File-Name": "chunked.csv"},
    )

    assert resp.status_code == 413
    assert app_client.get(f"/api/profiles/{pid}/files").json() == []
    assert [p for p in artifacts.artifact_dir(pid).rglob("*") if p.is_file()] == []


# ── the name and type of a raw-body upload ───────────────────────────────────


def test_the_file_name_travels_url_encoded(app_client: TestClient, tmp_db: Path):
    pid = _new_profile(app_client)
    created = _upload(app_client, pid, "résumé (final) 100%.pdf", b"%PDF", "application/pdf").json()
    assert created["name"] == "résumé (final) 100%.pdf"
    assert created["content_type"] == "application/pdf"


def test_an_upload_without_a_name_is_refused(app_client: TestClient, tmp_db: Path):
    pid = _new_profile(app_client)
    resp = app_client.post(f"/api/profiles/{pid}/files", content=b"x")
    assert resp.status_code == 400
    assert "X-File-Name" in resp.json()["detail"]
    assert app_client.get(f"/api/profiles/{pid}/files").json() == []


def test_a_default_body_type_is_replaced_by_a_guess_from_the_name(app_client: TestClient, tmp_db: Path):
    pid = _new_profile(app_client)
    # `curl --data-binary @report.csv` sends form-urlencoded, which is never what a file is.
    curl = _upload(app_client, pid, "report.csv", b"a,b\n", "application/x-www-form-urlencoded").json()
    assert curl["content_type"] == "text/csv"
    # A browser falls back to octet-stream for types it does not know.
    browser = _upload(app_client, pid, "notes.txt", b"x", "application/octet-stream").json()
    assert browser["content_type"] == "text/plain"
    unknown = _upload(app_client, pid, "blob.zzz", b"x", "application/octet-stream").json()
    assert unknown["content_type"] == "application/octet-stream"
    nothing = app_client.post(f"/api/profiles/{pid}/files", content=b"x", headers={"X-File-Name": "blob.zzz"}).json()
    assert nothing["content_type"] is None


# ── reclaiming what a hard kill left ─────────────────────────────────────────


def test_startup_reclaims_bytes_with_no_row(app_client: TestClient, tmp_db: Path):
    """A container kill never runs the endpoint's cleanup."""
    pid = _new_profile(app_client)
    kept = _upload(app_client, pid, "kept.csv", b"keep").json()

    orphan_dir = artifacts.artifact_dir(pid) / "11111111-2222-3333-4444-555555555555"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "ghost.csv").write_bytes(b"orphan")
    half_written = artifacts.artifact_dir(pid) / ".incoming-99999999"
    half_written.write_bytes(b"partial")

    assert artifacts.reclaim_all() == 2

    assert not orphan_dir.exists()
    assert not half_written.exists()
    assert Path(kept["container_path"]).read_bytes() == b"keep"
    assert [a["id"] for a in app_client.get(f"/api/profiles/{pid}/files").json()] == [kept["id"]]


def test_reclaim_leaves_the_picker_view_and_staging_alone(
    app_client: TestClient, tmp_db: Path, docker_runtime
):
    pid = _new_profile(app_client)
    _upload(app_client, pid, "kept.csv", b"keep")
    staging = artifacts.artifact_dir(pid) / artifacts.STAGING_DIRNAME
    staging.mkdir(parents=True, exist_ok=True)

    assert artifacts.reclaim_all() == 0
    assert artifacts.picker_dir(pid).is_dir()
    assert staging.is_dir()


def test_renaming_a_profile_relabels_its_chooser_entry(
    app_client: TestClient, tmp_db: Path, docker_runtime
):
    pid = _new_profile(app_client, "before-rename")
    _upload(app_client, pid, "report.csv")
    bookmarks = artifacts._gtk_bookmarks_path()
    assert bookmarks is not None and "before-rename" in bookmarks.read_text()

    app_client.put(f"/api/profiles/{pid}", json={"name": "after-rename"})

    assert "after-rename" in bookmarks.read_text()
    assert "before-rename" not in bookmarks.read_text()


def test_bookmarks_are_built_without_hydrating_every_profile(
    app_client: TestClient, tmp_db: Path, docker_runtime, monkeypatch: pytest.MonkeyPatch
):
    """list_profiles runs a tag query per profile; this rebuilds on every file mutation."""
    monkeypatch.setattr(db, "list_profiles", MagicMock(side_effect=AssertionError("hydrated")))
    pid = _new_profile(app_client, "dd-merchant")

    _upload(app_client, pid, "report.csv")

    assert "dd-merchant" in artifacts._gtk_bookmarks_path().read_text()


def test_blank_limit_settings_mean_the_defaults(monkeypatch: pytest.MonkeyPatch):
    # `docker compose` renders an unset `${ARTIFACT_MAX_BYTES:-}` as an empty string, which
    # must not stop the Manager from starting.
    monkeypatch.setenv("ARTIFACT_MAX_BYTES", "")
    assert artifacts._env_int("ARTIFACT_MAX_BYTES", 7) == 7
    monkeypatch.delenv("ARTIFACT_MAX_BYTES")
    assert artifacts._env_int("ARTIFACT_MAX_BYTES", 7) == 7
    monkeypatch.setenv("ARTIFACT_MAX_BYTES", "12")
    assert artifacts._env_int("ARTIFACT_MAX_BYTES", 7) == 12


# ── when the body is read ────────────────────────────────────────────────────
# The test client hands the app one coalesced body, which cannot show WHEN it was read.
# Driving the ASGI app directly with a counting ``receive`` can.


async def _drive_upload(pid: str, headers: dict[str, str], chunks: list[bytes]) -> tuple[int, int]:
    """POST to the upload route with a scripted body; returns (status, body chunks requested)."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": f"/api/profiles/{pid}/files", "raw_path": b"", "query_string": b"",
        "root_path": "", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    pending = list(chunks)
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        body = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await main.app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, reads


@pytest.mark.asyncio
async def test_an_over_cap_declared_length_is_refused_without_reading_the_body(
    app_client: TestClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 1024)
    pid = _new_profile(app_client)
    status, reads = await _drive_upload(
        pid, {"X-File-Name": "huge.bin", "Content-Length": "4096"}, [b"x" * 4096]
    )
    assert (status, reads) == (413, 0)


@pytest.mark.asyncio
async def test_a_declared_length_over_the_total_is_refused_without_reading_the_body(
    app_client: TestClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(artifacts, "MAX_PROFILE_ARTIFACT_BYTES", 100)
    pid = _new_profile(app_client)
    status, reads = await _drive_upload(
        pid, {"X-File-Name": "big.bin", "Content-Length": "200"}, [b"x" * 200]
    )
    assert (status, reads) == (507, 0)


@pytest.mark.asyncio
async def test_a_chunked_body_stops_being_read_once_it_passes_the_cap(
    app_client: TestClient, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 16)
    pid = _new_profile(app_client)
    status, reads = await _drive_upload(
        pid, {"X-File-Name": "chunked.bin"}, [b"x" * 10, b"x" * 10, b"x" * 10]
    )
    assert status == 413
    assert reads == 2  # the second chunk crossed the cap; the third was never asked for
    assert app_client.get(f"/api/profiles/{pid}/files").json() == []
    assert [p for p in artifacts.artifact_dir(pid).rglob("*") if p.is_file()] == []


def test_a_raw_utf8_name_is_accepted_too(app_client: TestClient, tmp_db: Path):
    """A curl user types the name as-is; Starlette hands it over decoded as latin-1."""
    pid = _new_profile(app_client)
    resp = app_client.post(
        f"/api/profiles/{pid}/files", content=b"x",
        headers={"X-File-Name": "résumé.pdf".encode("utf-8")},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "résumé.pdf"
