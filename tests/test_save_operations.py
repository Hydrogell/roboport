import io
import os
import stat
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import auth, db, main, server_data
from app.runtime import ServerState, ServerStatus
from app.runtime.docker import DockerRuntime
from app.server_saves import LOADED_SAVE_NAME, list_saves, saves_dir, set_current_save, store_uploaded_save


SERVER_ID = "save-test-server"


def _zip_bytes(name="level.dat0"):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(name, b"test world")
    return content.getvalue()


@pytest.fixture
def saves_api(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    monkeypatch.setattr(server_data, "SERVERS_DIR", tmp_path / "servers")
    db.init_db()
    with db.connect() as conn:
        user_id = auth.create_user(conn, "save-api-admin", "correct horse battery staple")
        _, key = auth.create_api_key(conn, user_id, "Save regression test", {"admin"})
        conn.execute(
            "INSERT INTO servers (uuid, name, version, port) VALUES (?, ?, ?, ?)",
            (SERVER_ID, "Save test server", "2.0.76", 34197),
        )
    saves = tmp_path / "servers" / SERVER_ID / "saves"
    saves.mkdir(parents=True)
    return TestClient(main.app, headers={"X-API-Key": key}), saves


def test_invalid_api_upload_keeps_existing_archive(saves_api):
    client, saves = saves_api
    target = saves / "world.zip"
    old_archive = _zip_bytes("old-level.dat0")
    target.write_bytes(old_archive)

    response = client.post(
        f"/api/v1/servers/{SERVER_ID}/saves/upload",
        files={"file": ("world.zip", b"not a zip", "application/zip")},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "not_a_zip"
    assert target.read_bytes() == old_archive


def test_uploaded_world_can_be_downloaded_and_renamed_via_api(saves_api):
    client, saves = saves_api
    base = f"/api/v1/servers/{SERVER_ID}/saves"
    world = _zip_bytes()
    response = client.post(base + "/upload", files={"file": ("world.zip", world, "application/zip")})
    assert response.status_code == 200
    assert response.json() == {"uuid": SERVER_ID, "name": "world.zip", "size": len(world)}
    assert client.get(base + "/world.zip").content == world
    renamed = client.post(base + "/world.zip/rename", json={"new_name": "archive.zip"})
    assert renamed.status_code == 200 and renamed.json()["renamed"] is True
    assert (saves / "archive.zip").read_bytes() == world


def test_interrupted_upload_keeps_existing_archive(tmp_path):
    class Interrupted(io.BytesIO):
        def read(self, size):
            if self.tell():
                raise OSError("controlled interrupted upload")
            return super().read(size)

    target = tmp_path / "saves" / "world.zip"
    target.parent.mkdir()
    old_archive = _zip_bytes("old-level.dat0")
    target.write_bytes(old_archive)

    with pytest.raises(OSError, match="controlled interrupted upload"):
        store_uploaded_save(Interrupted(b"incomplete"), target)

    assert target.read_bytes() == old_archive
    assert set(target.parent.iterdir()) == {target}


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not meaningful on Windows")
def test_new_upload_uses_normal_open_mode_subject_to_umask(tmp_path):
    previous_umask = os.umask(0o027)
    try:
        target = tmp_path / "saves" / "world.zip"
        store_uploaded_save(io.BytesIO(_zip_bytes()), target)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    target.chmod(0o600)
    store_uploaded_save(io.BytesIO(_zip_bytes("updated")), target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="Checks native POSIX directory and file symlinks")
def test_linked_save_directory_inside_server_supports_normal_worlds(tmp_path):
    storage = tmp_path / "worlds"
    storage.mkdir()
    (tmp_path / "saves").symlink_to("worlds", target_is_directory=True)
    world = _zip_bytes()
    store_uploaded_save(io.BytesIO(world), saves_dir(tmp_path) / "world.zip")
    (storage / "alias.zip").symlink_to("world.zip")

    assert [item["name"] for item in list_saves(tmp_path, None)] == ["world.zip"]
    assert set_current_save(saves_dir(tmp_path), "world.zip") is None
    assert (storage / LOADED_SAVE_NAME).read_bytes() == world


@pytest.mark.parametrize("client_type", ["web", "api"])
def test_imported_world_can_be_cloned_without_sharing_rcon(saves_api, monkeypatch, client_type):
    client, _ = saves_api
    monkeypatch.setattr(DockerRuntime, "status", lambda self, spec: ServerStatus(ServerState.STOPPED))
    root = server_data.SERVERS_DIR
    source = root / "Factory"
    (source / "saves").mkdir(parents=True)
    (source / "config").mkdir()
    world = _zip_bytes()
    (source / "saves" / LOADED_SAVE_NAME).write_bytes(world)
    (source / "config" / "rconpw").write_text("fixture", encoding="utf-8")
    with db.connect() as conn:
        owner = auth.get_user_by_username(conn, "save-api-admin")
        session = auth.create_session(conn, owner["id"])
    client.cookies.set(auth.SESSION_COOKIE, session.id)
    imported = client.post("/servers/import", follow_redirects=False, data={
        "_csrf": session.csrf_token, "dir_name": "Factory", "name": "Factory", "version": "2.0.76", "port": 34198,
    })
    assert imported.status_code == 303
    imported_path = urlsplit(imported.headers["location"]).path
    imported_id = imported_path.rsplit("/", 1)[1]
    existing_meta = (root / imported_id / "meta.json").read_bytes()
    duplicate = client.post("/servers/import", follow_redirects=False, data={
        "_csrf": session.csrf_token, "dir_name": imported_id, "name": "Second", "version": "2.0.76", "port": 34200,
    })
    assert duplicate.status_code == 409
    assert (root / imported_id / "meta.json").read_bytes() == existing_meta
    fields = {"new_name": "Copy", "new_port": 34199}
    if client_type == "web":
        result = client.post(f"{imported_path}/clone", follow_redirects=False,
                             data={"_csrf": session.csrf_token, **fields})
        assert result.status_code == 303
        clone_id = urlsplit(result.headers["location"]).path.rsplit("/", 1)[1]
    else:
        result = client.post(f"/api/v1{imported_path}/clone", json=fields)
        assert result.status_code == 200
        clone_id = result.json()["uuid"]

    assert (root / clone_id / "saves" / LOADED_SAVE_NAME).read_bytes() == world
    assert not (root / clone_id / "config" / "rconpw").exists()


def test_overlapping_same_name_uploads_install_one_complete_archive(tmp_path):
    target = tmp_path / "saves" / "world.zip"
    barrier = threading.Barrier(2)
    payloads = [_zip_bytes("one"), _zip_bytes("two-world")]

    class Overlapping(io.BytesIO):
        def read(self, size):
            if self.tell() == 0:
                barrier.wait(timeout=5)
            return super().read(size)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(store_uploaded_save, Overlapping(data), target) for data in payloads]
        assert [result.result(timeout=10) for result in results] == [len(data) for data in payloads]

    assert target.read_bytes() in payloads
    assert set(target.parent.iterdir()) == {target}


def test_set_current_uses_unique_backup_name_within_same_second(tmp_path):
    saves = tmp_path / "saves"
    saves.mkdir()
    frozen_now = datetime(2026, 11, 21, 12, 0, 0)
    (saves / LOADED_SAVE_NAME).write_bytes(b"first current")
    (saves / "first.zip").write_bytes(b"first selected")

    first_backup = set_current_save(saves, "first.zip", now=frozen_now)
    (saves / "second.zip").write_bytes(b"second selected")
    second_backup = set_current_save(saves, "second.zip", now=frozen_now)

    assert first_backup == "default-prev-20261121-120000.zip"
    assert second_backup == "default-prev-20261121-120000-1.zip"
    assert (saves / first_backup).read_bytes() == b"first current"
    assert (saves / second_backup).read_bytes() == b"first selected"
    assert (saves / LOADED_SAVE_NAME).read_bytes() == b"second selected"


@pytest.mark.parametrize("supports_links", [True, False], ids=["hard-link", "copy"])
def test_promoting_a_previous_backup_preserves_the_current_world(tmp_path, monkeypatch, supports_links):
    saves = tmp_path / "saves"
    saves.mkdir()
    frozen_now = datetime(2026, 11, 21, 12, 0, 0)
    existing = saves / "default-prev-20261121-120000.zip"
    existing.write_bytes(b"older backup")
    (saves / LOADED_SAVE_NAME).write_bytes(b"current")
    if not supports_links:
        def unsupported_link(*args, **kwargs):
            raise OSError("hard links unavailable")
        monkeypatch.setattr(os, "link", unsupported_link)
    archived = set_current_save(saves, existing.name, now=frozen_now)

    assert archived == "default-prev-20261121-120000-1.zip"
    assert not existing.exists()
    assert (saves / LOADED_SAVE_NAME).read_bytes() == b"older backup"
    assert (saves / archived).read_bytes() == b"current"


def test_simultaneous_set_current_operations_keep_all_archives(tmp_path):
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / LOADED_SAVE_NAME).write_bytes(b"initial")
    (saves / "first.zip").write_bytes(b"first")
    (saves / "second.zip").write_bytes(b"second")
    barrier = threading.Barrier(2)
    errors = []

    def select(name):
        try:
            barrier.wait()
            set_current_save(saves, name, now=datetime(2026, 11, 21, 12, 0, 0))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=select, args=(name,)) for name in ("first.zip", "second.zip")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert sorted(path.read_bytes() for path in saves.glob("*.zip")) == [b"first", b"initial", b"second"]


def test_set_current_restores_previous_default_when_install_fails(tmp_path, monkeypatch):
    saves = tmp_path / "saves"
    saves.mkdir()
    current = saves / LOADED_SAVE_NAME
    selected = saves / "selected.zip"
    current.write_bytes(b"previous current")
    selected.write_bytes(b"selected")
    original_replace = type(selected).replace

    def fail_selected_replace(path, destination):
        if path == selected and destination == current:
            assert current.read_bytes() == b"previous current"
            raise OSError("controlled install failure")
        return original_replace(path, destination)

    monkeypatch.setattr(type(selected), "replace", fail_selected_replace)

    with pytest.raises(OSError, match="controlled install failure"):
        set_current_save(saves, selected.name, now=datetime(2026, 11, 21, 12, 0, 0))

    assert current.read_bytes() == b"previous current"
    assert selected.read_bytes() == b"selected"
    assert set(saves.iterdir()) == {current, selected}
