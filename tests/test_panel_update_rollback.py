"""Regression coverage for rollback across a v1 -> beta source transition."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import panel_update


def test_restore_from_backup_keeps_live_database_and_env(tmp_path, monkeypatch):
    project = tmp_path / "project"
    backup = tmp_path / "backup"
    monkeypatch.setattr(panel_update, "PROJECT_ROOT", project)
    project.joinpath("app").mkdir(parents=True)
    project.joinpath(".env").write_text("PANEL_SECRET=live\n", encoding="utf-8")
    live_db = project / "app" / "panel.sqlite"
    live_conn = sqlite3.connect(live_db)
    live_conn.execute("CREATE TABLE marker (value TEXT)")
    live_conn.execute("INSERT INTO marker VALUES ('live rollback record')")
    live_conn.commit()
    project.joinpath("app", "code.py").write_text("new\n", encoding="utf-8")

    backup.joinpath("app").mkdir(parents=True)
    backup.joinpath("app", "panel.sqlite").write_bytes(b"obsolete snapshot")
    backup.joinpath("app", "code.py").write_text("beta\n", encoding="utf-8")
    backup.joinpath(".env").write_text("PANEL_SECRET=old\n", encoding="utf-8")

    try:
        panel_update.restore_from_backup(backup)
        live_conn.execute("INSERT INTO marker VALUES ('written after rollback')")
        live_conn.commit()
        assert project.joinpath("app", "code.py").read_text(encoding="utf-8") == "beta\n"
        assert project.joinpath(".env").read_text(encoding="utf-8") == "PANEL_SECRET=live\n"
        assert [row[0] for row in live_conn.execute("SELECT value FROM marker")] == [
            "live rollback record", "written after rollback",
        ]
    finally:
        live_conn.close()


def test_rollback_dispatches_v1_watchdog_after_restoring_beta_deploy(tmp_path, monkeypatch):
    project = tmp_path / "project"
    backup = tmp_path / "beta-backup"
    pre_rollback = tmp_path / "pre-rollback"
    pre_rollback.mkdir()
    monkeypatch.setattr(panel_update, "PROJECT_ROOT", project)
    monkeypatch.setattr(panel_update, "VERSION_FILE", project / "VERSION")
    project.joinpath("deploy").mkdir(parents=True)
    project.joinpath("deploy", "panel-update-watchdog.sh").write_text("v1 watchdog\n", encoding="utf-8")
    project.joinpath("VERSION").write_text("v1.0.0\n", encoding="utf-8")
    backup.joinpath("deploy").mkdir(parents=True)
    backup.joinpath("deploy", "panel-update-watchdog.sh").write_text("beta watchdog needs third arg\n", encoding="utf-8")
    backup.joinpath("VERSION").write_text("0.1.9-beta\n", encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE panel_updates (
        id INTEGER PRIMARY KEY, started_at INTEGER, finished_at INTEGER,
        from_version TEXT, to_version TEXT, source TEXT, status TEXT,
        error TEXT, backup_dir TEXT, initiated_by TEXT, archive_sha256 TEXT)""")
    conn.execute("INSERT INTO panel_updates (id, started_at, from_version, to_version, source, status, backup_dir, initiated_by) VALUES (1, 1, '0.1.9-beta', 'v1.0.0', 'github:v1.0.0', 'success', ?, 'admin')", (str(backup),))
    conn.commit()
    monkeypatch.setattr(panel_update, "make_backup", lambda label: pre_rollback)
    monkeypatch.setattr(panel_update, "install_requirements", lambda: None)
    dispatched: dict[str, Path] = {}
    monkeypatch.setattr(panel_update, "spawn_watchdog", lambda update_id, backup_dir, watchdog_path=None: dispatched.update(path=watchdog_path))

    panel_update.execute_rollback(conn, 1, "admin")

    assert project.joinpath("deploy", "panel-update-watchdog.sh").read_text(encoding="utf-8") == "beta watchdog needs third arg\n"
    assert dispatched["path"].read_text(encoding="utf-8") == "v1 watchdog\n"


def test_rollback_marks_record_failed_when_watchdog_copy_cannot_be_prepared(tmp_path, monkeypatch):
    project, backup, pre_rollback = tmp_path / "project", tmp_path / "backup", tmp_path / "pre"
    project.joinpath("deploy").mkdir(parents=True)
    project.joinpath("deploy", "panel-update-watchdog.sh").write_text("v1 watchdog\n", encoding="utf-8")
    project.joinpath("VERSION").write_text("v1.0.0\n", encoding="utf-8")
    backup.mkdir(); pre_rollback.mkdir()
    monkeypatch.setattr(panel_update, "PROJECT_ROOT", project)
    monkeypatch.setattr(panel_update, "VERSION_FILE", project / "VERSION")
    monkeypatch.setattr(panel_update, "make_backup", lambda label: pre_rollback)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE panel_updates (id INTEGER PRIMARY KEY, started_at INTEGER, finished_at INTEGER, from_version TEXT, to_version TEXT, source TEXT, status TEXT, error TEXT, backup_dir TEXT, initiated_by TEXT, archive_sha256 TEXT)")
    conn.execute("INSERT INTO panel_updates (id, started_at, from_version, source, status, backup_dir, initiated_by) VALUES (1, 1, '0.1.9-beta', 'github', 'success', ?, 'admin')", (str(backup),))
    conn.commit()
    monkeypatch.setattr(panel_update.shutil, "copy2", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        panel_update.execute_rollback(conn, 1, "admin")

    row = conn.execute("SELECT status, error FROM panel_updates WHERE id = 2").fetchone()
    assert row["status"] == "failed"
    assert "disk full" in row["error"]
