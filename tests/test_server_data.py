"""Shared behavior used by the HTML panel, API and background sampler."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from app import factorio_versions, server_data, server_files, server_saves


@pytest.mark.parametrize("mode, expected", [("host", "host"), (None, "bridge"), ("", "bridge")])
def test_runtime_spec_for_stored_servers(tmp_path, monkeypatch, mode, expected):
    monkeypatch.setattr(server_data, "SERVERS_DIR", tmp_path)
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT 'test' AS uuid, 'Test' AS name, '2.0' AS version, "
                           "34197 AS port, ? AS network_mode", (mode,)).fetchone()
        legacy = conn.execute("SELECT 'old' AS uuid, 'Old' AS name, '1.1' AS version, 34198 AS port").fetchone()
    spec = server_data.spec_from_row(row)
    assert (spec.name, spec.port, spec.data_dir, spec.network_mode) == ("Test", 34197, tmp_path / "test", expected)
    assert server_data.spec_from_row(legacy).network_mode == "bridge"


def test_shared_file_listing_and_save_metadata(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "readme.txt").write_bytes(b"hello")
    listing = server_files.list_dir(tmp_path)
    assert [(item["name"], item["size_h"], item["is_text"]) for item in listing] == [
        ("config", "—", False), ("readme.txt", "5 B", True),
    ]
    saves = server_saves.saves_dir(tmp_path)
    (saves / "a.zip").write_bytes(b"placeholder")
    (saves / "ignored.txt").touch()
    result = server_saves.list_saves(tmp_path, lambda p: [{"name": p.stem}])
    assert [(item["name"], item["mods_count"], item["size_h"]) for item in result] == [("a.zip", 1, "11 B")]
    assert server_saves.safe_save_name(" factory ") == "factory.zip"


def test_server_listings_ignore_symbolic_links(tmp_path, monkeypatch):
    (tmp_path / "local.txt").write_text("local", encoding="utf-8")
    (tmp_path / "linked.txt").write_text("link-like", encoding="utf-8")
    saves = server_saves.saves_dir(tmp_path)
    (saves / "local.zip").write_bytes(b"local save")
    (saves / "linked.zip").write_bytes(b"link-like save")
    original_is_symlink = type(tmp_path).is_symlink
    monkeypatch.setattr(
        type(tmp_path), "is_symlink",
        lambda path: path.name.startswith("linked") or original_is_symlink(path),
    )

    assert [item["name"] for item in server_files.list_dir(tmp_path)] == ["saves", "local.txt"]
    assert [item["name"] for item in server_saves.list_saves(tmp_path, lambda _path: [])] == ["local.zip"]


def test_saves_directory_rejects_an_entry_resolving_outside_server(tmp_path, monkeypatch):
    (tmp_path / "saves").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-saves"
    outside.mkdir()
    original_resolve = type(tmp_path).resolve
    monkeypatch.setattr(
        type(tmp_path), "resolve",
        lambda path: outside if path.name == "saves" else original_resolve(path),
    )

    with pytest.raises(HTTPException):
        server_saves.saves_dir(tmp_path)


def test_save_path_rejects_a_link_like_entry(tmp_path, monkeypatch):
    saves = server_saves.saves_dir(tmp_path)
    (saves / "linked.zip").write_bytes(b"not readable through a link")
    original_is_symlink = type(tmp_path).is_symlink
    monkeypatch.setattr(
        type(tmp_path), "is_symlink",
        lambda path: path.name == "linked.zip" or original_is_symlink(path),
    )

    with pytest.raises(HTTPException):
        server_saves.save_path(saves, "linked.zip")


def test_server_tree_helpers_accept_a_self_contained_direct_child(tmp_path):
    root = tmp_path / "servers"
    source = root / "orphan"
    (source / "config").mkdir(parents=True)
    (source / "config" / "server-settings.json").write_text("{}", encoding="utf-8")

    assert server_data.direct_server_dir(root, "orphan") == source
    assert server_data.validate_server_tree(source) == source.resolve()


def test_server_tree_helpers_reject_non_direct_and_link_like_sources(tmp_path, monkeypatch):
    root = tmp_path / "servers"
    source = root / "orphan"
    source.mkdir(parents=True)
    (source / "linked-file").write_text("placeholder", encoding="utf-8")

    with pytest.raises(ValueError):
        server_data.direct_server_dir(root, "../orphan")

    monkeypatch.setattr(
        server_data, "_is_plain_entry",
        lambda path: path.name != "linked-file",
    )
    with pytest.raises(ValueError):
        server_data.validate_server_tree(source)


def test_server_tree_validation_propagates_walk_errors(tmp_path, monkeypatch):
    source = tmp_path / "orphan"
    source.mkdir()

    def unreadable_walk(_source, *, onerror, **_kwargs):
        onerror(OSError("controlled unreadable directory"))
        return iter(())

    monkeypatch.setattr(server_data.os, "walk", unreadable_walk)
    with pytest.raises(ValueError, match="unreadable server tree"):
        server_data.validate_server_tree(source)


def test_copy_server_tree_keeps_normal_files_and_excludes_rcon_password(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "world.zip").write_bytes(b"world")
    (source / "rconpw").write_text("secret", encoding="utf-8")

    server_data.copy_server_tree(source, destination)

    assert (destination / "world.zip").read_bytes() == b"world"
    assert not (destination / "rconpw").exists()


def test_copy_server_tree_removes_its_destination_when_post_copy_validation_fails(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "world.zip").write_bytes(b"world")
    original_validate = server_data.validate_server_tree
    calls = []

    def reject_destination(path):
        calls.append(path)
        if path == destination:
            raise ValueError("controlled unsafe clone")
        return original_validate(path)

    monkeypatch.setattr(server_data, "validate_server_tree", reject_destination)

    with pytest.raises(ValueError, match="controlled unsafe clone"):
        server_data.copy_server_tree(source, destination)
    assert calls == [source, destination]
    assert not destination.exists()

def test_clearing_catalog_during_refresh_keeps_new_snapshot(monkeypatch):
    started = threading.Event()
    finish = threading.Event()
    versions = iter((["1.1.0"], ["2.0.0"]))

    def fetch():
        result = next(versions)
        if result == ["1.1.0"]:
            started.set()
            assert finish.wait(10)
        return result

    monkeypatch.setattr(factorio_versions, "_fetch_dockerhub_tags", fetch)
    factorio_versions.clear_cache()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(factorio_versions.available_versions)
            try:
                assert started.wait(3)
                pool.submit(factorio_versions.clear_cache).result(timeout=3)
                assert factorio_versions.available_versions() == ["2.0.0"]
            finally:
                finish.set()
            assert pending.result() == ["1.1.0"]
        assert factorio_versions.available_versions() == ["2.0.0"]
    finally:
        factorio_versions.clear_cache()
