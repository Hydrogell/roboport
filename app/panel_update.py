"""Self-update панели — проверка/установка/откат с GitHub Releases или upload.

Автор: Gellou (https://github.com/Hydrogell). Часть Roboport Controller.

Архитектура:
- Источник один — tar.gz/zip архив. Откуда взялся (GitHub API или upload-форма)
  для самого update flow неважно.
- Установка идёт в фоне через external watchdog-скрипт, переживающий restart
  процесса. Он же делает auto-rollback если health-check после рестарта не
  вернул 200.
- История пишется в БД (panel_updates), бэкапы — в .backups/<ts>/.

Точки входа:
- read_version() — текущая версия панели
- get_repo() / set_repo() — настройка GitHub-репо для проверки апдейтов
- check_github(repo) — фетч latest release с api.github.com
- start_install(source, ref, initiated_by) — запустить апдейт, вернуть update_id
- list_history(limit) — последние попытки
- start_rollback(update_id) — откатиться к бэкапу из истории
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import GITHUB_REPOSITORY, __version__
from .i18n import tr

# =============================================================================
# Константы и пути
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = PROJECT_ROOT / "VERSION"
BACKUPS_DIR = PROJECT_ROOT / ".backups"
STAGING_DIR = PROJECT_ROOT / ".update-staging"
# Загруженные через UI архивы лежат тут — отдельно от staging, потому что
# execute_install целиком rmtree'ит staging перед распаковкой и съел бы
# исходный архив до того как успеет его прочитать.
UPLOADS_DIR = PROJECT_ROOT / ".uploads"

# Папки/файлы которые НЕ перезаписываем при апдейте — это runtime-данные
# и креды юзера, не часть кода.
PRESERVE_PATHS = {
    ".env",
    ".env.local",
    "app/panel.sqlite",
    "app/panel.sqlite-shm",
    "app/panel.sqlite-wal",
    "servers",
    ".venv",
    "venv",
    ".backups",
    ".update-staging",
    ".git",
    ".claude",
    "mods_cache.json",
    "factorio",
}

# Что попадает в бэкап для rollback'а: код + метаданные. БД и .env тоже,
# но как safety net (при краше схема может быть несовместима).
BACKUP_PATHS = (
    "app",
    "deploy",
    "VERSION",
    "requirements.txt",
    "factorio_installer.py",
    "factorio_mod_portal.py",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    ".env",
)

MAX_BACKUPS = 5
MIN_FREE_BYTES = 200 * 1024 * 1024  # 200 MB
DEFAULT_REPO = GITHUB_REPOSITORY

# Таймаут для GitHub API запросов
HTTP_TIMEOUT = 15


# =============================================================================
# Версия и репо
# =============================================================================

def read_version() -> str:
    """Версия из VERSION-файла. Падёт назад на __version__ из app/__init__."""
    try:
        v = VERSION_FILE.read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass
    return __version__


def get_repo() -> str:
    """GitHub-репо для проверки обновлений. Из ENV или дефолтный."""
    return (os.environ.get("PANEL_UPDATE_REPO") or "").strip() or DEFAULT_REPO


def set_repo(repo: str) -> None:
    """Сохранить репо в process env (UI пишет это сразу после submit'а формы).
    Persist в .env делается отдельно через _set_env_in_dotenv (как в panel_config)."""
    os.environ["PANEL_UPDATE_REPO"] = repo


_REPO_RE = __import__("re").compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")


def is_valid_repo(repo: str) -> bool:
    """`owner/name` без слешей внутри сегментов и без шемы."""
    return bool(_REPO_RE.match(repo or ""))


# =============================================================================
# GitHub Releases API
# =============================================================================

@dataclass
class ReleaseInfo:
    tag: str
    name: str
    body: str         # release notes (markdown)
    published_at: str
    tarball_url: str  # https://api.github.com/repos/.../tarball/vX.Y.Z
    html_url: str     # https://github.com/.../releases/tag/vX.Y.Z


def check_github(repo: str) -> ReleaseInfo:
    """Спросить latest release у GitHub. Кидает RuntimeError на сетевые/HTTP ошибки.

    Не валит панель если репо не существует или нет ни одного релиза —
    отдаёт информативный текст ошибки."""
    if not is_valid_repo(repo):
        raise RuntimeError(tr("panel_update.repository_invalid", repo=repo))

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Roboport/{read_version()}",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(
                tr(
                    "panel_update.release_not_found",
                    repo=repo,
                )
            ) from e
        if e.code == 403:
            raise RuntimeError(
                tr("panel_update.github_rate_limited")
            ) from e
        raise RuntimeError(tr("panel_update.github_api_error", status=e.code, detail=e.reason)) from e
    except urllib.error.URLError as e:
        raise RuntimeError(tr("panel_update.github_connection_failed", detail=e.reason)) from e

    return ReleaseInfo(
        tag=data.get("tag_name") or "",
        name=data.get("name") or "",
        body=data.get("body") or "",
        published_at=data.get("published_at") or "",
        tarball_url=data.get("tarball_url") or "",
        html_url=data.get("html_url") or "",
    )


def is_newer(latest_tag: str, current: str) -> bool:
    """Сравнить теги вида vX.Y.Z. Не SemVer-полный — просто числовой кортеж по
    точкам, нечисловые суффиксы (rc1, beta) идут «меньше» числовых."""
    def parse(t: str) -> tuple:
        t = (t or "").lstrip("vV").strip()
        # Отрезаем pre-release / build после '-' / '+'
        for sep in ("-", "+"):
            if sep in t:
                t = t.split(sep, 1)[0]
        parts = []
        for p in t.split("."):
            try:
                parts.append((1, int(p)))   # числовое — выше нечислового
            except ValueError:
                parts.append((0, p))
        return tuple(parts)
    return parse(latest_tag) > parse(current)


# =============================================================================
# DB-helpers (panel_updates)
# =============================================================================

def sha256_file(path: Path) -> str:
    """Hex-sha256 файла, чанками по 1 MB чтобы не грузить большие архивы целиком."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def history_record(conn: sqlite3.Connection, started_at: int, from_version: str,
                   to_version: str | None, source: str, status: str,
                   backup_dir: str | None, initiated_by: str,
                   archive_sha256: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO panel_updates "
        "(started_at, from_version, to_version, source, status, backup_dir, "
        " initiated_by, archive_sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (started_at, from_version, to_version, source, status, backup_dir,
         initiated_by, archive_sha256),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def history_finalize(conn: sqlite3.Connection, update_id: int, status: str,
                     error: str | None = None, to_version: str | None = None) -> None:
    """Watchdog вызывает через CLI после health-check'а."""
    fields = ["finished_at = ?", "status = ?"]
    args: list = [int(time.time()), status]
    if error is not None:
        fields.append("error = ?")
        args.append(error)
    if to_version is not None:
        fields.append("to_version = ?")
        args.append(to_version)
    args.append(update_id)
    conn.execute(
        f"UPDATE panel_updates SET {', '.join(fields)} WHERE id = ?",
        args,
    )
    conn.commit()


def list_history(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT id, started_at, finished_at, from_version, to_version, source, "
        "       status, error, backup_dir, initiated_by, archive_sha256 "
        "FROM panel_updates ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_history(conn: sqlite3.Connection, update_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, started_at, finished_at, from_version, to_version, source, "
        "       status, error, backup_dir, initiated_by "
        "FROM panel_updates WHERE id = ?",
        (update_id,),
    ).fetchone()
    return dict(row) if row else None


def cleanup_old_updates(conn: sqlite3.Connection, days: int = 90) -> int:
    """Удалить записи старше N дней. Бэкапы на диске не трогаем (их чистит
    отдельный _trim_backups при создании нового)."""
    cutoff = int(time.time()) - days * 86400
    cur = conn.execute(
        "DELETE FROM panel_updates WHERE started_at < ? AND status != 'running'",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


# =============================================================================
# Скачивание архива
# =============================================================================

def download_tarball(url: str, dest: Path) -> int:
    """Скачать GitHub tarball в dest. Возвращает размер в байтах.
    Проверка sha256 не делается — GitHub Releases не публикует чексумму
    в API; для нашей модели угроз tls-канала достаточно."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={
        "User-Agent": f"Roboport/{read_version()}",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f, length=64 * 1024)
    return dest.stat().st_size


# =============================================================================
# Распаковка и валидация
# =============================================================================

def extract_archive(archive: Path, dest: Path) -> Path:
    """Распаковать tar.gz/zip в dest. Возвращает путь к корневой папке (внутри
    архива GitHub кладёт всё в `<repo>-<sha>/...`, мы возвращаем эту папку).

    Защита от path-traversal: проверяем что все имена внутри архива не
    содержат '..' и не абсолютные."""
    dest.mkdir(parents=True, exist_ok=True)

    if archive.suffix == ".zip" or zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                _check_safe_path(name)
            zf.extractall(dest)
    else:
        # Считаем что это tar.gz / tar.xz / tar
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                _check_safe_path(member.name)
                if member.islnk() or member.issym():
                    raise RuntimeError(tr("panel_update.archive_has_link", path=member.name))
            # Python 3.12+: filter='data' — отбрасывает unsafe-вещи
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                tf.extractall(dest)

    # Найти top-level dir с приложением (где есть app/main.py)
    candidates = [p for p in dest.iterdir() if p.is_dir()]
    for c in candidates:
        if (c / "app" / "main.py").exists():
            return c
    # Может архив был распакован сразу в корень (без обёртки)
    if (dest / "app" / "main.py").exists():
        return dest
    raise RuntimeError(tr("panel_update.archive_roboport_missing"))


def _check_safe_path(name: str) -> None:
    if not name:
        return
    norm = name.replace("\\", "/")
    if norm.startswith("/") or ".." in norm.split("/"):
        raise RuntimeError(tr("panel_update.archive_unsafe_path", path=repr(name)))


def validate_staging(staging_root: Path) -> str:
    """Базовая sanity-проверка: внутри есть app/main.py + requirements.txt + VERSION.
    Возвращает версию из staging-VERSION."""
    required = ("app/main.py", "requirements.txt", "VERSION")
    for rel in required:
        if not (staging_root / rel).exists():
            raise RuntimeError(tr("panel_update.archive_file_missing", path=rel))
    v = (staging_root / "VERSION").read_text(encoding="utf-8").strip()
    if not v:
        raise RuntimeError(tr("panel_update.archive_version_empty"))
    return v


# =============================================================================
# Бэкап и rsync-style overlay
# =============================================================================

def make_backup(label: str) -> Path:
    """Создать бэкап в .backups/<ts>-<label>/. Возвращает путь к директории.
    Затрагиваем только BACKUP_PATHS — runtime-данные (servers/, mods_cache.json)
    не бэкапим: они весят сотни МБ и не часть апдейта."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = BACKUPS_DIR / f"{ts}-{label}"
    backup_dir.mkdir(mode=0o700, parents=True)

    for rel in BACKUP_PATHS:
        src = PROJECT_ROOT / rel
        if not src.exists():
            continue
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            # The SQLite backup below includes committed WAL pages.
            shutil.copytree(
                src, dst,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", "*.pyo",
                    "panel.sqlite", "panel.sqlite-shm", "panel.sqlite-wal", "panel.sqlite-journal",
                ),
                dirs_exist_ok=False,
            )
        else:
            shutil.copy2(src, dst)

    # A filesystem copy can miss committed pages still in WAL.  A closed
    # SQLite backup is a consistent rollback safety snapshot.
    live_db = PROJECT_ROOT / "app" / "panel.sqlite"
    if live_db.exists():
        backup_db = backup_dir / "app" / "panel.sqlite"
        backup_db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(live_db)) as source, closing(sqlite3.connect(backup_db)) as snapshot:
            source.backup(snapshot)
        shutil.copymode(live_db, backup_db)

    # manifest.json — что внутри
    manifest = {
        "created_at": int(time.time()),
        "version": read_version(),
        "label": label,
        "paths": list(BACKUP_PATHS),
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    _trim_backups(MAX_BACKUPS)
    return backup_dir


def _trim_backups(keep: int) -> None:
    """Оставить только последние `keep` бэкапов. Старые удаляются."""
    if not BACKUPS_DIR.exists():
        return
    backups = sorted(
        (p for p in BACKUPS_DIR.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in backups[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def apply_overlay(staging_root: Path) -> None:
    """Скопировать содержимое staging в PROJECT_ROOT, не трогая PRESERVE_PATHS.

    Для каждого файла в staging:
      - если относительный путь начинается на один из PRESERVE_PATHS → skip
      - иначе copy2 поверх

    Удаление файлов которые есть на проде но нет в staging — НЕ делаем.
    Это сейф: если юзер положил свой фикс рядом с кодом, он переживёт апдейт.
    Минус: переименованный файл останется как orphan; для апдейтов это редко."""
    for src in staging_root.rglob("*"):
        rel = src.relative_to(staging_root)
        if _is_preserved(rel):
            continue
        if src.is_dir():
            (PROJECT_ROOT / rel).mkdir(parents=True, exist_ok=True)
        else:
            dst = PROJECT_ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _is_preserved(rel: Path) -> bool:
    s = str(rel).replace("\\", "/")
    for p in PRESERVE_PATHS:
        if s == p or s.startswith(p + "/"):
            return True
    return False


# =============================================================================
# pip install в venv
# =============================================================================

def install_requirements() -> None:
    """Запустить `pip install -r requirements.txt` через venv'овый python.
    Если процесс запущен из venv — sys.executable указывает на него. Иначе
    фоллбек на текущий python (юзер сам отвечает за виртуалку)."""
    py = sys.executable
    req = PROJECT_ROOT / "requirements.txt"
    if not req.exists():
        return
    try:
        subprocess.run(
            [py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
             "-r", str(req)],
            cwd=str(PROJECT_ROOT),
            check=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(tr("panel_update.dependencies_timeout")) from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(tr("panel_update.dependencies_failed", code=e.returncode)) from e


# =============================================================================
# Pre-flight check
# =============================================================================

def preflight_check() -> None:
    """Бросить RuntimeError если апдейт сейчас не пройдёт. Делается ДО backup'а."""
    free = shutil.disk_usage(PROJECT_ROOT).free
    if free < MIN_FREE_BYTES:
        mb = free // (1024 * 1024)
        raise RuntimeError(tr("panel_update.disk_space_insufficient", size=mb))
    if not (PROJECT_ROOT / "VERSION").exists():
        raise RuntimeError(tr("panel_update.version_missing"))


# =============================================================================
# Watchdog: внешний скрипт запускающий restart + health-check + auto-rollback
# =============================================================================

def spawn_watchdog(update_id: int, backup_dir: Path,
                   watchdog_path: Path | None = None) -> None:
    """Запустить watchdog entrypoint. Он сам создаёт независимый transient
    systemd service, переживает restart и при неудаче откатывает обновление.

    Не делает блокирующего ожидания — endpoint должен сразу вернуть 202."""
    watchdog = watchdog_path or (PROJECT_ROOT / "deploy" / "panel-update-watchdog.sh")
    if not watchdog.exists():
        raise RuntimeError(tr("panel_update.watchdog_missing", path=watchdog))

    cmd = ["bash", str(watchdog), str(update_id), str(backup_dir)]
    # На Windows bash/systemd нет — self-update там пока не поддержан.
    if sys.platform == "win32":
        raise RuntimeError(
            tr("panel_update.windows_manual_install")
        )

    log_path = backup_dir / "watchdog.log"
    with open(log_path, "ab") as logf:
        subprocess.Popen(
            cmd,
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            cwd=str(PROJECT_ROOT),
            start_new_session=True,
            env=os.environ | {"ROBOPORT_PROJECT_ROOT": str(PROJECT_ROOT)},
        )


# =============================================================================
# Главный entry-point: install
# =============================================================================

@dataclass
class InstallPlan:
    source: str          # 'github:vX.Y.Z' | 'upload:<filename>'
    archive_path: Path
    cleanup_archive: bool


def prepare_from_github(repo: str, tag: str) -> InstallPlan:
    """Скачать tarball под /tmp."""
    if not is_valid_repo(repo):
        raise RuntimeError(tr("panel_update.repository_unrecognized", repo=repo))
    if not tag:
        raise RuntimeError(tr("settings.update_tag_required"))
    url = f"https://api.github.com/repos/{repo}/tarball/{tag}"
    archive = STAGING_DIR / f"{tag.replace('/', '_')}.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    download_tarball(url, archive)
    return InstallPlan(
        source=f"github:{tag}",
        archive_path=archive,
        cleanup_archive=True,
    )


def prepare_from_upload(uploaded_archive: Path) -> InstallPlan:
    return InstallPlan(
        source=f"upload:{uploaded_archive.name}",
        archive_path=uploaded_archive,
        cleanup_archive=True,
    )


def execute_install(conn: sqlite3.Connection, plan: InstallPlan,
                    initiated_by: str) -> int:
    """Полный цикл апдейта (без блокирующего ожидания health). Возвращает
    update_id чтобы UI мог поллить status."""
    preflight_check()

    # 0. Если архив лежит ВНУТРИ STAGING_DIR (старый upload-flow) или это
    #    свежескачанный github-tarball который тоже клался в staging —
    #    rmtree(STAGING_DIR) ниже его прибьёт раньше чем мы успеем прочитать.
    #    Перенесём сначала во временный файл вне staging, и подменим путь.
    archive_path = plan.archive_path
    try:
        archive_path.relative_to(STAGING_DIR)
        archive_in_staging = True
    except ValueError:
        archive_in_staging = False
    safe_archive: Path | None = None
    if archive_in_staging:
        fd, tmp_path = tempfile.mkstemp(prefix="roboport-update-", suffix=archive_path.suffix)
        os.close(fd)
        safe_archive = Path(tmp_path)
        shutil.copy2(archive_path, safe_archive)
        archive_path = safe_archive

    # 1. Распаковать в staging (sha256 архива считаем ДО — на распакованном
    # дереве сделать нельзя, плюс это даёт ID сборки даже если versions равны)
    archive_sha = sha256_file(archive_path)
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
    STAGING_DIR.mkdir(parents=True)
    staging_root = extract_archive(archive_path, STAGING_DIR)
    new_version = validate_staging(staging_root)
    current = read_version()

    # 2. Записать в историю как running, чтобы UI сразу увидел
    started = int(time.time())
    update_id = history_record(
        conn, started_at=started,
        from_version=current, to_version=new_version,
        archive_sha256=archive_sha,
        source=plan.source, status="running",
        backup_dir=None, initiated_by=initiated_by,
    )

    # 3. Бэкап
    backup_dir = make_backup(label=f"pre-update-{update_id}")
    conn.execute(
        "UPDATE panel_updates SET backup_dir = ? WHERE id = ?",
        (str(backup_dir), update_id),
    )
    conn.commit()

    # 4. Apply overlay + pip
    apply_overlay(staging_root)
    install_requirements()

    # 5. Cleanup staging + archive
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    # Временную копию из /tmp убираем всегда (если делали)
    if safe_archive is not None:
        try:
            safe_archive.unlink()
        except OSError:
            pass
    if plan.cleanup_archive:
        try:
            plan.archive_path.unlink()
        except OSError:
            pass  # уже удалён вместе с STAGING_DIR — это ОК

    # 6. Spawn watchdog → restart → health-check → maybe rollback
    spawn_watchdog(update_id, backup_dir)

    return update_id


# =============================================================================
# Rollback
# =============================================================================

def restore_from_backup(backup_dir: Path) -> None:
    """Скопировать BACKUP_PATHS из бэкапа поверх PROJECT_ROOT.
    Перед вызовом неплохо бы остановить панель — но т.к. rollback запускается
    из watchdog'а ПОСЛЕ что-то-пошло-не-так, текущий процесс уже мёртв, и
    после restore'а watchdog снова делает systemctl restart."""
    if not backup_dir.is_dir():
        raise RuntimeError(tr("panel_update.backup_missing", path=backup_dir))
    runtime_db_names = {"panel.sqlite", "panel.sqlite-wal", "panel.sqlite-shm", "panel.sqlite-journal"}
    for rel in BACKUP_PATHS:
        # The running panel owns its configuration and open SQLite files.
        if rel == ".env":
            continue
        src = backup_dir / rel
        if not src.exists():
            continue
        dst = PROJECT_ROOT / rel
        if rel == "app":
            dst.mkdir(parents=True, exist_ok=True)
            for child in src.iterdir():
                if child.name in runtime_db_names:
                    continue
                target = dst / child.name
                if child.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target)
        elif src.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def execute_rollback(conn: sqlite3.Connection, src_update_id: int,
                     initiated_by: str) -> int:
    """Создать новую запись panel_updates со статусом running и source='rollback',
    перенести файлы из старого backup_dir, спавнить watchdog для restart."""
    src = get_history(conn, src_update_id)
    if not src:
        raise RuntimeError(tr("panel_update.record_missing", id=src_update_id))
    if not src["backup_dir"] or not Path(src["backup_dir"]).is_dir():
        raise RuntimeError(tr("panel_update.backup_unavailable", id=src_update_id))
    backup_dir = Path(src["backup_dir"])

    current = read_version()
    target = src.get("from_version") or "?"
    started = int(time.time())
    update_id = history_record(
        conn, started_at=started,
        from_version=current, to_version=target,
        source=f"rollback:{src_update_id}", status="running",
        backup_dir=str(backup_dir), initiated_by=initiated_by,
    )

    # Сохраняем pre-rollback бэкап на случай если откат тоже сломается
    pre_rollback = make_backup(label=f"pre-rollback-{update_id}")
    conn.execute(
        "UPDATE panel_updates SET backup_dir = ? WHERE id = ?",
        (str(pre_rollback), update_id),
    )
    conn.commit()

    # The restored release can be an older beta with the old, killable shell
    # watchdog. Copy our known-good entrypoint outside deploy/ before restore.
    watchdog_copy = pre_rollback / "panel-update-watchdog-v1.sh"
    try:
        shutil.copy2(PROJECT_ROOT / "deploy" / "panel-update-watchdog.sh", watchdog_copy)
        watchdog_copy.chmod(watchdog_copy.stat().st_mode | 0o111)
        restore_from_backup(backup_dir)
        install_requirements()
        spawn_watchdog(update_id, pre_rollback, watchdog_path=watchdog_copy)
    except Exception as exc:
        history_finalize(conn, update_id, "failed", str(exc))
        raise
    return update_id
