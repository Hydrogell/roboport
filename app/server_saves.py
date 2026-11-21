"""Names, locations and listings of Factorio save archives."""
from __future__ import annotations

import re
import shutil
import stat
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException

from .i18n import tr
from .server_files import humanize_size

SAVE_NAME_RE = re.compile(r"^[A-Za-z0-9_\- ]{1,60}\.zip$")
# Какой сейв по умолчанию грузит образ при старте (см. SAVE_NAME=default в DockerRuntime).
LOADED_SAVE_NAME = "default.zip"
_SAVE_OPERATION_LOCK = threading.Lock()


class InvalidSaveArchiveError(ValueError):
    """Uploaded file is not a readable ZIP archive."""


def saves_dir(data_dir: Path) -> Path:
    d = data_dir / "saves"
    d.mkdir(parents=True, exist_ok=True)
    try:
        base = data_dir.resolve()
        resolved = d.resolve()
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(400, tr("servers.forbidden_path", path="saves"))
    return resolved


def save_path(saves: Path, name: str) -> Path:
    """Return a save entry only when it is a non-link inside ``saves``."""
    candidate = saves / name
    if candidate.is_symlink():
        raise HTTPException(400, tr("servers.forbidden_path", path=name))
    try:
        target = candidate.resolve()
        target.relative_to(saves.resolve())
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(400, tr("servers.forbidden_path", path=name))
    return target


def safe_save_name(name: str) -> str:
    """Валидация имени сейва. Только базовое имя, .zip, без traversal."""
    name = (name or "").strip()
    if "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(400, tr("servers.invalid_save_name"))
    if not name.lower().endswith(".zip"):
        # дружелюбно дописываем расширение
        name = name + ".zip"
    if not SAVE_NAME_RE.match(name):
        raise HTTPException(400, tr("servers.invalid_save_name_format"))
    return name


def list_saves(data_dir: Path, mod_parser) -> list[dict]:
    """Список сейвов с размером, mtime и числом модов внутри. Невалидные zip-файлы
    показываем без mod-инфо (mod_parser сам обработает ошибку)."""
    saves_d = saves_dir(data_dir)
    out: list[dict] = []
    for p in sorted(saves_d.iterdir(), key=lambda x: x.name.lower()):
        if p.is_symlink():
            continue
        if p.suffix.lower() != ".zip":
            continue
        try:
            st = p.stat()
        except FileNotFoundError:
            # A simultaneous rename or delete can remove a directory entry before stat.
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        mods = mod_parser(p) if mod_parser else []
        out.append({
            "name": p.name,
            "size": st.st_size,
            "size_h": humanize_size(st.st_size),
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "mtime_ts": int(st.st_mtime),
            "mods_count": len(mods),
            "mods": mods,  # [{name, version}]
        })
    # Самый свежий — наверх (полезно после save-now / upload)
    out.sort(key=lambda x: x["mtime_ts"], reverse=True)
    return out


def store_uploaded_save(upload: BinaryIO, destination: Path) -> int:
    """Stage and validate the upload on the same filesystem as the game."""
    if destination.is_symlink():
        raise HTTPException(400, tr("servers.forbidden_path", path=destination.name))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}.", dir=destination.parent) as tmp_dir:
        temporary = Path(tmp_dir) / "upload.part"
        with temporary.open("xb") as out:
            shutil.copyfileobj(upload, out, length=1 << 20)
            written = out.tell()
        if not zipfile.is_zipfile(temporary):
            raise InvalidSaveArchiveError()
        with _SAVE_OPERATION_LOCK:
            try:
                shutil.copymode(destination, temporary)
            except FileNotFoundError:
                pass  # New files retain the mode set by the process umask.
            temporary.replace(destination)
    return written


def _previous_save_name(saves: Path, now: datetime) -> str:
    stem = f"default-prev-{now.strftime('%Y%m%d-%H%M%S')}"
    candidate = f"{stem}.zip"
    sequence = 1
    while (saves / candidate).exists():
        candidate = f"{stem}-{sequence}.zip"
        sequence += 1
    return candidate


def set_current_save(saves: Path, name: str, *, now: datetime | None = None) -> str | None:
    """Keep the current world available until its replacement is installed atomically."""
    with _SAVE_OPERATION_LOCK:
        source = save_path(saves, name)
        if not source.is_file():
            raise FileNotFoundError(name)
        current = save_path(saves, LOADED_SAVE_NAME)
        backup: Path | None = None
        if current.exists():
            backup = saves / _previous_save_name(saves, now or datetime.now())
            try:
                backup.hardlink_to(current)
            except FileExistsError:
                raise
            except OSError:
                # Some network shares and removable filesystems do not support hard links.
                with tempfile.TemporaryDirectory(prefix=".save-backup-", dir=saves) as staging:
                    copy = Path(staging) / "backup.zip"
                    shutil.copy2(current, copy)
                    copy.replace(backup)
        try:
            source.replace(current)
        except OSError:
            if backup is not None:
                backup.unlink()
            raise
        return backup.name if backup is not None else None


def rename_save(saves: Path, old_name: str, new_name: str) -> None:
    """Rename one archive without racing panel uploads or selections."""
    with _SAVE_OPERATION_LOCK:
        source = save_path(saves, old_name)
        destination = save_path(saves, new_name)
        if not source.is_file():
            raise FileNotFoundError(old_name)
        if destination.exists():
            raise FileExistsError(new_name)
        source.replace(destination)


def delete_save(saves: Path, name: str) -> None:
    """Delete one archive without racing panel uploads or selections."""
    with _SAVE_OPERATION_LOCK:
        target = save_path(saves, name)
        if not target.is_file():
            raise FileNotFoundError(name)
        target.unlink()
