"""Shared database records and on-disk directories for managed Factorio servers."""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
from pathlib import Path

from .i18n import tr
from .runtime import ServerSpec
from .server_files import is_within

SERVERS_DIR = Path(__file__).resolve().parents[1] / "servers"
NAME_RE = re.compile(r"^[A-Za-z0-9_\- ]{1,40}$")


def _resolved(path: Path) -> Path:
    """Resolve a path without exposing platform-specific link errors."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as error:
        raise ValueError("unresolvable server path") from error


def _is_plain_entry(path: Path) -> bool:
    """Allow ordinary files and directories, checking the entry itself."""
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError("unreadable server path") from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) and not (attributes & reparse)


def direct_server_dir(root: Path, name: str) -> Path:
    """Return an existing, real, immediate server-root child named by ``name``."""
    if not name or name in (".", "..") or Path(name).name != name:
        raise ValueError("server directory must be a direct child")
    root_resolved = _resolved(root)
    candidate = root / name
    if not _is_plain_entry(candidate) or not candidate.is_dir():
        raise ValueError("server directory is not a real directory")
    resolved = _resolved(candidate)
    if resolved.parent != root_resolved:
        raise ValueError("server directory is outside its root")
    return resolved


def validate_server_tree(source: Path) -> Path:
    """Ensure a server tree is self-contained and contains no link-like entries."""
    if not _is_plain_entry(source) or not source.is_dir():
        raise ValueError("server source is not a real directory")
    root = _resolved(source)

    def walk_error(error: OSError) -> None:
        raise ValueError("unreadable server tree") from error

    for current, directories, files in os.walk(source, followlinks=False,
                                               onerror=walk_error):
        current_path = Path(current)
        if not _is_plain_entry(current_path) or not is_within(root, current_path):
            raise ValueError("server tree escapes its root")
        for name in [*directories, *files]:
            child = current_path / name
            if not _is_plain_entry(child) or not is_within(root, child):
                raise ValueError("server tree contains a link-like entry")
    return root


def copy_server_tree(source: Path, destination: Path) -> None:
    """Copy a checked server tree without following links, and validate again.

    The destination is claimed before copying so cleanup can only remove the
    directory created by this call.
    """
    validate_server_tree(source)
    destination.mkdir()
    try:
        def ignore(_directory: str, names: list[str]) -> list[str]:
            return [name for name in names if name.casefold() == "rconpw"]

        shutil.copytree(source, destination, ignore=ignore, symlinks=True,
                        dirs_exist_ok=True)
        validate_server_tree(destination)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def find_server(db: sqlite3.Connection, uuid: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM servers WHERE uuid = ?", (uuid,)).fetchone()


def spec_from_row(row) -> ServerSpec:
    # network_mode появилось в миграции — у старых БД-объектов ключа может не быть.
    nm = "bridge"
    try:
        nm = row["network_mode"] or "bridge"
    except (KeyError, IndexError):
        pass
    return ServerSpec(
        uuid=row["uuid"], name=row["name"], version=row["version"],
        port=row["port"], data_dir=SERVERS_DIR / row["uuid"],
        network_mode=nm,
    )


def default_server_settings(*, name: str, description: str, password: str,
                            max_players: int, visibility: str, verify_users: bool,
                            tags: list[str] | None = None) -> dict:
    """Build Factorio's shared defaults with the verification policy chosen by the caller."""
    return {
        "name": name,
        "description": description,
        **({"tags": tags} if tags is not None else {}),
        "max_players": max_players,
        "visibility": {
            "public": visibility == "public",
            "lan": visibility in ("public", "lan"),
        },
        "username": os.environ.get("FACTORIO_USERNAME", "").strip(),
        "token": os.environ.get("FACTORIO_TOKEN", "").strip(),
        "game_password": password,
        "require_user_verification": verify_users,
        "max_upload_in_kilobytes_per_second": 0,
        "max_upload_slots": 5,
        "minimum_latency_in_ticks": 0,
        "ignore_player_limit_for_returning_players": False,
        "allow_commands": "admins-only",
        "autosave_interval": 10,
        "autosave_slots": 5,
        "afk_autokick_interval": 0,
        "auto_pause": True,
        "only_admins_can_pause_the_game": True,
        "autosave_only_on_server": True,
        "non_blocking_saving": False,
    }


def delete_server_records(db: sqlite3.Connection, uuid: str) -> None:
    """Remove the server and its dependent data in the caller's transaction."""
    db.execute("DELETE FROM player_sessions WHERE server_uuid = ?", (uuid,))
    db.execute("DELETE FROM metrics_samples WHERE server_uuid = ?", (uuid,))
    db.execute("DELETE FROM scheduled_tasks WHERE server_uuid = ?", (uuid,))
    db.execute("DELETE FROM servers WHERE uuid = ?", (uuid,))


def unique_conflict_message(exc: sqlite3.IntegrityError) -> str:
    """Распознать UNIQUE-конфликт на servers.name/port и вернуть человеческое
    сообщение. Это backstop поверх SELECT-then-INSERT проверок: ловит race и
    случаи когда кто-то добавил новый INSERT-сайт, забыв проверить.
    Сообщение для самого 409 ответа — детали (значение) пользователь введёт сам."""
    msg = str(exc)
    if "servers.name" in msg:
        return tr("servers.name_already_taken")
    if "servers.port" in msg:
        return tr("servers.port_already_taken")
    return tr("servers.unique_conflict", detail=msg)


def remove_directory_via_docker(target_dir: Path) -> str | None:
    """Linux-fallback для server_delete когда shutil.rmtree упёрся в права:
    образ factoriotools пишет файлы под uid контейнера (PUID=845 по умолчанию),
    панель под non-root юзером — rmtree получает PermissionError на части файлов.

    Запускаем одноразовый alpine-контейнер с правами root внутри, делаем chown
    на текущего user/group панели, после чего обычный rmtree уже справится.
    Возвращает None при успехе, текст ошибки иначе.

    На Windows os.getuid() не существует — там PermissionError обычно из-за
    write-handle на /logs (Docker Desktop), это не лечится chown'ом."""
    import os
    import subprocess as _sp

    if not hasattr(os, "getuid"):
        return tr("servers.remove_only_on_linux")
    uid, gid = os.getuid(), os.getgid()
    try:
        # alpine:latest — 7 МБ, лежит в кэше после первого pull. На свежей
        # системе один pull ~5-10 сек.
        _sp.run(
            ["docker", "run", "--rm",
             "-v", f"{target_dir.resolve()}:/_t",
             "alpine:latest", "chown", "-R", f"{uid}:{gid}", "/_t"],
            check=True, timeout=120, capture_output=True, text=True,
        )
    except _sp.CalledProcessError as e:
        return tr("servers.remove_chown_failed", status=e.returncode, detail=(e.stderr or '').strip()[:160])
    except FileNotFoundError:
        return tr("updates.docker_cli_not_found")
    except Exception as e:
        return f"docker chown: {type(e).__name__}: {e!s:.160}"
    try:
        shutil.rmtree(target_dir)
    except Exception as e:
        return tr("servers.remove_after_chown_failed", kind=type(e).__name__, detail=f"{e!s:.160}")
    return None
