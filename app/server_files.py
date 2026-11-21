"""Directory listings, display sizes and text files shared by the panel and API."""
from __future__ import annotations

import stat
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from .i18n import tr

# Расширения, считающиеся текстовыми. Файлы за пределами списка открываются только
# на скачивание — не пытаемся читать как UTF-8 во избежание мусора в редакторе.
TEXT_EXTS = {
    ".txt", ".log", ".md",
    ".json", ".jsonc",
    ".toml", ".yaml", ".yml",
    ".properties", ".ini", ".cfg", ".conf", ".env",
    ".lua", ".py", ".js", ".css", ".html", ".sh",
    ".gitignore", ".dockerignore",
}
# Файлы без расширения, которые тоже текстовые
TEXT_NAMES = {"rconpw", "Dockerfile", "Makefile", "README", "LICENSE"}
# Лимит размера для редактирования. Большие логи лучше скачивать, не открывать.
MAX_TEXT_BYTES = 5 * 1024 * 1024


def is_text_file(p: Path) -> bool:
    return p.suffix.lower() in TEXT_EXTS or p.name in TEXT_NAMES


def safe_resolve(base: Path, rel: str) -> Path:
    """Разрешить rel относительно base, проверив что результат не вылез за base.
    Защита от path traversal (../../etc/passwd, /absolute/path и т.п.)."""
    # Bias: обнулим ведущие / и \ чтобы (base / "/etc") не уехал в корень.
    rel = rel.lstrip("/\\") if rel else ""
    try:
        base_r = base.resolve()
        target = (base_r / rel).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, tr("servers.forbidden_path", path=repr(rel)))
    if target != base_r:
        try:
            target.relative_to(base_r)
        except ValueError:
            raise HTTPException(400, tr("servers.forbidden_path", path=repr(rel)))
    return target


def humanize_size(size: int | None) -> str:
    if size is None:
        return "—"
    if size < 1024:
        return f"{size} B"
    amount = float(size)
    for unit in ("KB", "MB", "GB"):
        amount /= 1024
        if amount < 1024:
            return f"{amount:.1f} {unit}"
    return f"{amount / 1024:.1f} TB"


def is_within(base: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def directory_size(directory: Path) -> int:
    size = 0
    for child in directory.rglob("*"):
        if child.is_symlink() or not is_within(directory, child):
            continue
        try:
            info = child.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            size += info.st_size
    return size


def list_dir(directory: Path, base: Path | None = None) -> list[dict]:
    items = []
    base = base or directory
    for path in directory.iterdir():
        # Imported server directories may contain links.  Do not follow or
        # expose them: their targets are not part of this server's data tree.
        if path.is_symlink() or not is_within(base, path):
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        is_file = stat.S_ISREG(info.st_mode)
        items.append({
            "name": path.name,
            "is_dir": stat.S_ISDIR(info.st_mode),
            "is_text": is_file and is_text_file(path),
            "size": info.st_size if is_file else None,
            "size_h": humanize_size(info.st_size if is_file else None),
            "mtime": datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return sorted(items, key=lambda item: (not item["is_dir"], item["name"].lower()))


def breadcrumbs(rel_path: str) -> list[dict]:
    """Хлебные крошки. Пустой rel_path = только корень."""
    crumbs = [{"name": tr("servers.directory_root"), "path": ""}]
    if not rel_path:
        return crumbs
    parts = rel_path.replace("\\", "/").strip("/").split("/")
    acc = ""
    for p in parts:
        if not p:
            continue
        acc = f"{acc}/{p}" if acc else p
        crumbs.append({"name": p, "path": acc})
    return crumbs
