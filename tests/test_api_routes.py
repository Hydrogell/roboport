import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, db, main, server_data
from app import factorio_mods as mods


SERVER_ID = "test-server"
MOD_NAME = "test-mod"


@pytest.fixture
def mods_api(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    servers_dir = tmp_path / "servers"
    monkeypatch.setattr(server_data, "SERVERS_DIR", servers_dir)
    cache_file = tmp_path / "mods-cache.json"
    monkeypatch.setattr(mods, "CACHE_FILE", cache_file)
    db.init_db()
    with db.connect() as conn:
        user_id = auth.create_user(conn, "api-admin", "correct horse battery staple")
        _, key = auth.create_api_key(conn, user_id, "API regression test", {"admin"})
        conn.execute(
            "INSERT INTO servers (uuid, name, version, port) VALUES (?, ?, ?, ?)",
            (SERVER_ID, "Test Factorio server", "2.0.76", 34197),
        )

    mods_dir = servers_dir / SERVER_ID / "mods"
    mods_dir.mkdir(parents=True)
    _make_archive(mods_dir, MOD_NAME, "1.0.0")

    client = TestClient(main.app, headers={"X-API-Key": key}, raise_server_exceptions=False)
    return client, cache_file, mods_dir


def _make_archive(directory, name, version):
    info = {"name": name, "title": "Test Mod", "version": version, "factorio_version": "2.0"}
    path = directory / f"{name}_{version}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}_{version}/info.json", json.dumps(info))
    return path


def _mock_portal_downloads(monkeypatch, name, fail=False):
    monkeypatch.setattr(mods, "get_credentials", lambda: ("test-user", "test-token"))
    monkeypatch.setattr(mods, "find_release", lambda _, version, __: {"version": version})

    def download(release, username, token, target_dir):
        path = _make_archive(target_dir, name, release["version"])
        if fail:
            raise OSError("controlled download failure")
        return path

    monkeypatch.setattr(mods, "download_release", download)


def test_api_lists_updates_for_installed_mods(mods_api):
    client, cache_file, mods_dir = mods_api
    _make_archive(mods_dir, "incompatible-mod", "1.0.0")
    release = {"version": "1.1.0", "info_json": {"factorio_version": "2.0"}, "file_size": 2048}
    incompatible = {"version": "2.0.0", "info_json": {"factorio_version": "2.1"}}
    cache_file.write_text(json.dumps({
        "synced_at": 0,
        "count": 2,
        "mods": [
            {"name": MOD_NAME, "latest_release": release},
            {"name": "incompatible-mod", "latest_release": incompatible},
        ],
    }), encoding="utf-8")

    response = client.get(f"/api/v1/servers/{SERVER_ID}/mods/updates")

    assert response.status_code == 200
    assert response.json()["updates"] == [{
        "name": MOD_NAME, "title": "Test Mod", "current": "1.0.0", "latest": "1.1.0", "size_kb": 2,
    }]


@pytest.mark.parametrize("endpoint", ["sync", "bulk-update"])
def test_api_syncs_mod_cache_without_external_portal(mods_api, monkeypatch, endpoint):
    client, cache_file, _ = mods_api
    release = {"version": "1.0.0", "info_json": {"factorio_version": "2.0"}}
    portal_mods = [{"name": MOD_NAME, "latest_release": release}]

    def portal_listing(url):
        assert url == "https://mods.factorio.com/api/mods?page_size=max"
        return {"results": portal_mods}

    monkeypatch.setattr(mods, "http_get_json", portal_listing)
    body = {"all": True, "sync_first": True} if endpoint == "bulk-update" else None

    response = client.post(f"/api/v1/servers/{SERVER_ID}/mods/{endpoint}", json=body)

    assert response.status_code == 200
    result = response.json()
    cache = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cache["count"] == 1
    assert cache["mods"] == portal_mods
    if endpoint == "sync":
        assert result == {"ok": True, "synced_at": cache["synced_at"], "count": 1}
    else:
        assert result == {"uuid": SERVER_ID, "synced": True, "results": []}


def test_api_installs_a_new_mod_in_the_mod_directory(mods_api, monkeypatch):
    client, _, mods_dir = mods_api
    _mock_portal_downloads(monkeypatch, "new-mod")

    response = client.post(f"/api/v1/servers/{SERVER_ID}/mods", json={
        "name": "new-mod", "version": "1.2.0",
    })

    assert response.status_code == 200
    assert response.json()["installed"] == [{"name": "new-mod", "version": "1.2.0"}]
    assert {p.name for p in mods_dir.iterdir()} == {f"{MOD_NAME}_1.0.0.zip", "new-mod_1.2.0.zip"}


def test_api_cache_refresh_preserves_previous_listing_if_replace_fails(mods_api, monkeypatch):
    client, cache_file, _ = mods_api
    previous = '{"synced_at": 1, "count": 0, "mods": []}'
    cache_file.write_text(previous, encoding="utf-8")
    monkeypatch.setattr(mods, "http_get_json", lambda _: {"results": [{"name": MOD_NAME}]})
    replace = Path.replace

    def fail_cache_replace(path, target):
        if target == cache_file:
            raise OSError("controlled cache write failure")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_cache_replace)
    before = set(cache_file.parent.iterdir())
    response = client.post(f"/api/v1/servers/{SERVER_ID}/mods/bulk-update", json={
        "all": True, "sync_first": True,
    })

    assert response.status_code == 502
    assert cache_file.read_text(encoding="utf-8") == previous
    assert set(cache_file.parent.iterdir()) == before


def test_api_reports_failed_bulk_update_when_no_archive_was_installed(mods_api, monkeypatch):
    client, cache_file, mods_dir = mods_api
    current = _make_archive(mods_dir, "quality", "1.0.0")
    current_contents = current.read_bytes()
    before = set(mods_dir.iterdir())
    release = {"version": "1.1.0", "info_json": {"factorio_version": "2.0"}}
    cache_file.write_text(json.dumps({
        "synced_at": 0, "count": 1, "mods": [{"name": "quality", "latest_release": release}],
    }), encoding="utf-8")
    monkeypatch.setattr(mods, "get_credentials", lambda: ("test-user", "test-token"))

    response = client.post(f"/api/v1/servers/{SERVER_ID}/mods/bulk-update", json={"all": True})

    assert response.status_code == 200
    assert response.json()["results"] == [{
        "name": "quality", "from": "1.0.0", "to": None, "ok": False, "error": "builtin",
    }]
    assert current.read_bytes() == current_contents
    assert set(mods_dir.iterdir()) == before


@pytest.mark.parametrize(("failure", "expected_error"), [
    (None, None),
    ("download", "controlled download failure"),
    ("old-file-locked", "controlled file lock"),
    ("stale-catalog", "not-on-portal"),
])
def test_api_bulk_update_replaces_mod_or_keeps_previous(mods_api, monkeypatch, failure, expected_error):
    client, cache_file, mods_dir = mods_api
    current = mods_dir / f"{MOD_NAME}_1.0.0.zip"
    before = current.read_bytes()
    release = {"version": "1.1.0", "info_json": {"factorio_version": "2.0"}}
    cache_file.write_text(json.dumps({
        "synced_at": 0, "count": 1, "mods": [{"name": MOD_NAME, "latest_release": release}],
    }), encoding="utf-8")
    _mock_portal_downloads(monkeypatch, MOD_NAME, fail=failure == "download")
    if failure == "old-file-locked":
        unlink = Path.unlink

        def unlink_except_current(path, *args, **kwargs):
            if path == current:
                raise PermissionError("controlled file lock")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink_except_current)
    if failure == "stale-catalog":
        monkeypatch.setattr(mods, "find_release", lambda *_: {"version": "1.0.0"})

    response = client.post(f"/api/v1/servers/{SERVER_ID}/mods/bulk-update", json={"all": True})

    assert response.status_code == 200
    if expected_error:
        assert response.json()["results"] == [{
            "name": MOD_NAME, "from": "1.0.0", "to": None, "ok": False, "error": expected_error,
        }]
        assert current.read_bytes() == before
        assert {p.name for p in mods_dir.iterdir()} == {current.name}
    else:
        assert response.json()["results"] == [{
            "name": MOD_NAME, "from": "1.0.0", "to": "1.1.0", "ok": True,
        }]
        assert {p.name for p in mods_dir.iterdir()} == {f"{MOD_NAME}_1.1.0.zip"}
