"""Regression checks for the panel updater's archive boundaries."""
import os
import tarfile
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import auth, db, main, panel_update


@pytest.fixture
def update_api(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    monkeypatch.setattr(panel_update, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(panel_update, "STAGING_DIR", tmp_path / "staging")
    db.init_db()
    with closing(db.connect()) as conn, conn:
        user_id = auth.create_user(conn, "updates", "test operator passphrase")
        _, key = auth.create_api_key(conn, user_id, "Update test", {"admin"})
    plans = []

    def record_install(conn, plan, initiated_by):
        plans.append(plan)
        return 42

    monkeypatch.setattr(panel_update, "execute_install", record_install)
    with closing(TestClient(main.app, headers={"X-API-Key": key})) as client:
        yield client, plans


def test_api_install_reuses_upload_directory(update_api):
    client, plans = update_api
    payload = b"update archive placeholder"
    response = client.post("/api/v1/panel/update/upload", files={"file": ("Release 1.zip", payload)})
    assert response.status_code == 200
    filename = response.json()["filename"]
    installed = client.post("/api/v1/panel/update/install", json={"source": "upload", "upload_filename": filename})
    assert installed.status_code == 202, installed.text
    assert plans[0].archive_path == panel_update.UPLOADS_DIR / filename
    assert plans[0].archive_path.read_bytes() == payload


def test_api_install_can_read_old_staged_upload(update_api):
    client, plans = update_api
    panel_update.STAGING_DIR.mkdir()
    expected = panel_update.STAGING_DIR / "Previous.tar.gz"
    expected.write_bytes(b"previous update archive placeholder")
    response = client.post("/api/v1/panel/update/install",
                           json={"source": "upload", "upload_filename": expected.name})
    assert response.status_code == 202
    assert plans[0].archive_path == expected


def test_api_install_requires_a_regular_uploaded_file(update_api):
    client, plans = update_api
    panel_update.STAGING_DIR.joinpath("Directory.zip").mkdir(parents=True)
    response = client.post("/api/v1/panel/update/install",
                           json={"source": "upload", "upload_filename": "Directory.zip"})
    assert response.status_code == 404
    assert plans == []


def test_github_download_destinations_are_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(panel_update, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(panel_update.tempfile, "tempdir", str(tmp_path))
    requests = []

    def download(url, path):
        requests.append(url)
        path.write_bytes(b"compressed official release")
        return path.stat().st_size

    monkeypatch.setattr(panel_update, "download_tarball", download)
    first = panel_update.prepare_from_github("sample/roboport", "stable/v1.2.3")
    second = panel_update.prepare_from_github("sample/roboport", "stable/v1.2.3")
    try:
        assert first.archive_path != second.archive_path
        assert first.archive_path.parent == second.archive_path.parent == tmp_path
        assert first.source == second.source == "github:stable/v1.2.3"
        assert requests == ["https://api.github.com/repos/sample/roboport/tarball/stable%2Fv1.2.3"] * 2
    finally:
        first.archive_path.unlink(missing_ok=True)
        second.archive_path.unlink(missing_ok=True)


def test_failed_github_download_leaves_no_partial_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(panel_update.tempfile, "tempdir", str(tmp_path))

    def interrupted(url, path):
        path.write_bytes(b"partial download")
        raise OSError("connection closed")

    monkeypatch.setattr(panel_update, "download_tarball", interrupted)
    with pytest.raises(OSError, match="connection closed"):
        panel_update.prepare_from_github("sample/roboport", "v1.2.3")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX executable modes")
def test_tar_extraction_preserves_executable_release_scripts(tmp_path):
    source = tmp_path / "source"
    source.joinpath("app").mkdir(parents=True)
    source.joinpath("app", "main.py").write_text("app = None\n", encoding="utf-8")
    source.joinpath("deploy").mkdir()
    script = source / "deploy" / "install.sh"
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    script.chmod(0o755)
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname="sample-release")

    extracted = panel_update.extract_archive(archive, tmp_path / "staging")
    installed = extracted / "deploy" / "install.sh"
    assert installed.read_bytes() == script.read_bytes()
    assert installed.stat().st_mode & 0o111 == 0o111
