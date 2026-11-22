"""Factorio mod archives, installed state, and the local Mod Portal catalog."""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import time
import urllib.parse
import zipfile
import zlib
from pathlib import Path
from threading import Lock

from factorio_mod_portal import (
    API as MOD_API,
    CACHE as CACHE_FILE,
    download_release,
    get_credentials,
    http_get_json,
    load_dotenv,
    parse_dep,
    parse_version,
    read_mod_info,
    save_catalog,
)

from .i18n import tr

# Populate the credential environment from .env, keeping shell values first.
load_dotenv()

logger = logging.getLogger(__name__)
_archive_update_lock = Lock()


def factorio_major_minor(server_version: str) -> str:
    """'2.0.76' → '2.0'. Mods на портале объявляют совместимость по major.minor."""
    return ".".join((server_version or "").split(".")[:2])


def list_installed(server_dir: Path) -> list[dict]:
    """Сканирует servers/<uuid>/mods/, возвращает список модов с метаданными.
    Невалидные zip-файлы пропускаются молча (read_mod_info вернёт None)."""
    mods_dir = server_dir / "mods"
    out: list[dict] = []
    if not mods_dir.is_dir():
        return out
    for zf in sorted(mods_dir.glob("*.zip")):
        info = read_mod_info(zf)
        if info is None:
            continue
        deps = info.get("dependencies", []) or []
        out.append({
            "name": info["name"],
            "title": info.get("title") or info["name"],
            "version": info["version"],
            "factorio_version": info.get("factorio_version", "?"),
            "author": info.get("author", "—"),
            "filename": zf.name,
            "size": zf.stat().st_size,
            "deps": deps,
            "deps_required": [d for d in (parse_dep(x) for x in deps)
                              if d.get("kind") == "required"],
        })
    return out


def load_cache_quiet() -> dict:
    """В отличие от cmd_search/load_cache из CLI — не выходит из процесса.
    Возвращает {'synced_at': int, 'count': int, 'mods': [...]} либо пустую структуру."""
    if not CACHE_FILE.exists():
        return {"synced_at": 0, "count": 0, "mods": []}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"synced_at": 0, "count": 0, "mods": []}


class PortalListingError(RuntimeError):
    """Downloading the Mod Portal listing failed."""


def refresh_catalog() -> dict:
    """Fetch the full listing and replace the shared cache after writing it."""
    try:
        data = http_get_json(f"{MOD_API}/mods?page_size=max")
    except Exception as exc:
        logger.warning("Mod Portal catalog request failed (%s)", type(exc).__name__)
        raise PortalListingError(tr("settings.general.unavailable")) from exc

    return save_catalog(CACHE_FILE, data.get("results", []))


def search_cache(query: str, factorio_version: str | None = None,
                 limit: int = 20) -> list[dict]:
    """Поиск по локальному mods_cache.json. Если факторио-версия задана —
    показываем ВСЕ результаты, но проставляем поле 'compat_factorio' (версия
    из latest_release) и 'is_compatible' (bool, для бейджа в UI). Раньше
    фильтровали — это путало юзера: моды просто исчезали из выдачи без
    объяснения. Теперь видны все, с пометкой о (не)совместимости."""
    cache = load_cache_quiet()
    q = (query or "").strip().lower()
    mods = cache["mods"]
    if q:
        mods = [m for m in mods
                if q in (m.get("name") or "").lower()
                or q in (m.get("title") or "").lower()]
    mods.sort(key=lambda m: m.get("downloads_count", 0), reverse=True)
    out: list[dict] = []
    for m in mods[:limit]:
        compat = (m.get("latest_release") or {}).get("info_json", {}) \
            .get("factorio_version")
        # Копия чтобы не мутировать кэш
        item = dict(m)
        item["compat_factorio"] = compat
        item["is_compatible"] = bool(
            factorio_version and compat == factorio_version
        )
        out.append(item)
    return out


def installed_index(installed: list[dict]) -> dict[str, str]:
    """name → version для уже установленных."""
    return {m["name"]: m["version"] for m in installed}


def annotate_with_updates(installed: list[dict],
                          factorio_version: str) -> list[dict]:
    """Добавить поле 'latest_in_cache' (None если кэша нет / нет в кэше)
    и 'has_update' если в кэше есть версия новее."""
    by_name = {m["name"]: m for m in load_cache_quiet()["mods"]}
    return _annotate_with_updates(installed, factorio_version, by_name)


def _annotate_with_updates(installed: list[dict], factorio_version: str,
                           by_name: dict[str, dict]) -> list[dict]:
    for m in installed:
        portal = by_name.get(m["name"])
        latest = None
        if portal:
            rel = portal.get("latest_release") or {}
            if rel.get("info_json", {}).get("factorio_version") == factorio_version:
                latest = rel.get("version")
        m["latest_in_cache"] = latest
        m["has_update"] = (
            latest is not None
            and parse_version(latest) > parse_version(m["version"])
        )
    return installed


def available_updates(server_dir: Path, factorio_version: str) -> dict:
    """Build an update report using one snapshot of the shared cache."""
    cache = load_cache_quiet()
    by_name = {m["name"]: m for m in cache["mods"]}
    installed = _annotate_with_updates(list_installed(server_dir), factorio_version, by_name)
    updates = []
    for m in installed:
        if not m.get("has_update"):
            continue
        portal = by_name.get(m["name"]) or {}
        rel = portal.get("latest_release") or {}
        updates.append({
            "name": m["name"],
            "title": m.get("title") or m["name"],
            "current": m["version"],
            "latest": m["latest_in_cache"],
            "size_kb": int((rel.get("file_size") or 0) / 1024) or None,
        })
    synced_at = int(cache.get("synced_at") or 0)
    return {
        "cache": {
            "synced_at": synced_at,
            "count": int(cache.get("count") or 0),
            "stale": bool(synced_at and (time.time() - synced_at) > 24 * 3600),
        },
        "factorio_version": factorio_version,
        "updates": updates,
    }


# ---------- mod-list.json (включён/выключен) ----------

# Эти моды поставляются с самим Factorio (Space Age DLC) и не лежат в /mods как .zip.
# Для UI это значит: их можно включать/выключать, но не «устанавливать с портала».
BUILTIN_MODS = {"base", "elevated-rails", "quality", "space-age"}


def read_modlist(server_dir: Path) -> dict[str, bool]:
    """Прочитать mod-list.json в виде {name: enabled}. Если файла нет — пусто."""
    f = server_dir / "mods" / "mod-list.json"
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, bool] = {}
    for item in data.get("mods", []):
        if isinstance(item, dict) and item.get("name"):
            out[item["name"]] = bool(item.get("enabled", True))
    return out


def write_modlist(server_dir: Path, state: dict[str, bool]) -> None:
    """Записать mod-list.json. base всегда оставляем включённым (Factorio падает иначе)."""
    state = dict(state)  # копия
    state["base"] = True
    f = server_dir / "mods" / "mod-list.json"
    f.parent.mkdir(exist_ok=True)
    payload = {"mods": [{"name": n, "enabled": e} for n, e in state.items()]}
    f.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                 encoding="utf-8")


# ---------- Save inspection ----------

_NAME_RE = re.compile(rb"^[a-zA-Z][a-zA-Z0-9_-]{2,63}$")


def _try_parse_mod_entry(buf: bytes, offset: int) -> dict | None:
    """Парсит одну запись мода в заголовке save'а:
       [u8 namelen 3..64][name][u8 major][u8 minor][u8 patch][u32 CRC].
       Возвращает {name, version, next_offset} или None если по смещению не он."""
    if offset >= len(buf):
        return None
    ln = buf[offset]
    if not (3 <= ln <= 64):
        return None
    end_name = offset + 1 + ln
    if end_name + 7 > len(buf):
        return None
    name_bytes = buf[offset + 1:end_name]
    if not _NAME_RE.fullmatch(name_bytes):
        return None
    major, minor, patch = buf[end_name], buf[end_name + 1], buf[end_name + 2]
    if major > 100:
        # Sanity-фильтр на случайные байты. У реальных модов major почти всегда
        # 0..3, но встречаются и более высокие (inventory-repair: 20.0.3).
        # Был порог 10 — отрезал хвост цепочки в больших mod-pack-сейвах.
        return None
    return {
        "name": name_bytes.decode("ascii"),
        "version": f"{major}.{minor}.{patch}",
        "next_offset": end_name + 7,
    }


def parse_save_mods(save_zip_path: Path) -> list[dict]:
    """Достать список модов из Factorio save. Стратегия:
    1) Декомпрессить level.dat0 (zlib).
    2) В первом килобайте искать максимально длинную цепочку из подряд идущих
       mod-entries, начинающуюся с 'base' (он всегда первый в списке Factorio).
    Это устойчиво к изменениям header'а между версиями: мы не пытаемся понять
    что лежит ДО списка модов, ищем сам список как цельный паттерн."""
    if not save_zip_path.is_file():
        return []
    try:
        with zipfile.ZipFile(save_zip_path) as z:
            dat = next((n for n in z.namelist() if n.endswith("/level.dat0")), None)
            if dat is None:
                return []
            raw = z.read(dat)
    except (zipfile.BadZipFile, KeyError):
        return []

    # Factorio пишет zlib-stream
    if raw[:2] in (b"\x78\x01", b"\x78\x9c", b"\x78\xda"):
        try:
            data = zlib.decompress(raw)
        except zlib.error:
            return []
    else:
        data = raw

    # 64 КБ хватает с запасом — мод-лента обычно стартует в первых сотнях байт,
    # а через всю секцию header'а нам идти и не надо. Расширили с 2 КБ потому
    # что в больших mod-packs ленту мы рискуем не зацепить если start близко к концу.
    HEADER_LIMIT = min(65536, len(data))
    best: list[dict] = []
    for start in range(HEADER_LIMIT):
        chain: list[dict] = []
        off = start
        while True:
            m = _try_parse_mod_entry(data, off)
            if m is None:
                break
            chain.append(m)
            off = m["next_offset"]
            if len(chain) >= 200:  # защита от циклов на патологическом входе
                break
        if len(chain) >= 2 and chain[0]["name"] == "base" and len(chain) > len(best):
            best = chain

    return [{"name": m["name"], "version": m["version"]} for m in best]


def find_latest_release(name: str, factorio_version: str) -> dict | None:
    """Последний релиз для указанной версии Factorio."""
    return find_release(name, None, factorio_version)


def find_release(name: str, want_version: str | None,
                 factorio_version: str) -> dict | None:
    """Найти релиз: сначала точная версия (для импорта из сейва — детерминированно),
    при её отсутствии — последний для нашей factorio-версии."""
    url = f"{MOD_API}/mods/{urllib.parse.quote(name)}/full"
    try:
        data = http_get_json(url)
    except Exception:
        return None
    candidates = [
        r for r in data.get("releases", [])
        if r.get("info_json", {}).get("factorio_version") == factorio_version
    ]
    if want_version:
        exact = [r for r in candidates if r["version"] == want_version]
        if exact:
            return exact[0]
    return candidates[-1] if candidates else None


class InvalidModArchive(ValueError):
    """An archive could not be identified as a Factorio mod."""


def install_mod_from_zip(tmp_zip: Path, target_dir: Path) -> dict:
    """Установить мод из загруженного zip'а: валидируем info.json, переименовываем
    в каноническое <name>_<version>.zip, перезаписываем старые версии этого мода."""
    info = read_mod_info(tmp_zip)
    if info is None:
        raise InvalidModArchive(tr("mods.archive_missing_info_json"))
    if not isinstance(info, dict):
        raise InvalidModArchive(tr("mods.archive_invalid"))
    name = info.get("name")
    version = info.get("version")
    if not name or not version:
        raise InvalidModArchive(tr("mods.info_json_missing_required_fields"))
    if (not isinstance(name, str) or any(ch in name for ch in "/\\:")
            or any(ord(ch) < 32 for ch in name)
            or not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)):
        raise InvalidModArchive(tr("mods.archive_invalid"))

    target_dir.mkdir(exist_ok=True)
    canonical = target_dir / f"{name}_{version}.zip"
    with tempfile.TemporaryDirectory(prefix=".mod-import-", dir=target_dir) as staging:
        uploaded = Path(staging) / "archive.zip"
        shutil.copyfile(tmp_zip, uploaded)
        with _archive_update_lock:
            uploaded.replace(canonical)
            for old in target_dir.glob("*.zip"):
                if old != canonical and old.stem.rpartition("_")[0] == name:
                    old.unlink()
    return {"name": name, "version": version, "title": info.get("title") or name,
            "factorio_version": info.get("factorio_version")}


def install_missing(wanted: list[dict],
                    target_dir: Path,
                    factorio_version: str) -> dict:
    """Установить недостающие моды из списка wanted = [{name, version|None}, ...].
    BUILTIN'ы пропускаются. Уже установленные пропускаются.

    Возвращает summary: {requested, installed[], skipped[], failed[]}."""
    installed_names = {Path(z).stem.rsplit("_", 1)[0]
                       for z in (p.name for p in target_dir.glob("*.zip"))}
    user, token = get_credentials()
    summary: dict = {
        "requested": len(wanted),
        "installed": [], "skipped": [], "failed": [],
    }
    if not user:
        # Без кредов всё равно есть смысл подсчитать что нужно — но качать нечем.
        for w in wanted:
            summary["failed"].append({"name": w["name"], "reason": "no credentials"})
        return summary

    target_dir.mkdir(exist_ok=True)
    for w in wanted:
        name = w["name"]
        version = w.get("version")
        if name in BUILTIN_MODS:
            summary["skipped"].append({"name": name, "reason": "builtin"})
            continue
        if name in installed_names:
            summary["skipped"].append({"name": name, "reason": "already-installed"})
            continue
        rel = find_release(name, version, factorio_version)
        if rel is None:
            summary["failed"].append({"name": name, "reason": "not-on-portal"})
            continue
        try:
            download_release(rel, user, token, target_dir)
            summary["installed"].append({"name": name, "version": rel["version"]})
        except Exception as exc:
            logger.warning("Mod Portal download failed (%s)", type(exc).__name__)
            summary["failed"].append({"name": name, "reason": "download_failed"})
    return summary


def update_installed_mod(current: dict, version: str, mods_dir: Path, factorio_version: str) -> str | None:
    """Install the requested release, returning a per-mod error if it cannot be applied."""
    previous = mods_dir / current["filename"]
    try:
        with tempfile.TemporaryDirectory(prefix=".mod-update-", dir=mods_dir) as staging:
            staged_dir = Path(staging)
            wanted = [{"name": current["name"], "version": version}]
            summary = install_missing(wanted, staged_dir, factorio_version)
            if not summary["installed"]:
                issues = summary["failed"] or summary["skipped"]
                reason = issues[0].get("reason") if issues else None
                return reason or "install_failed"
            if summary["installed"][0]["version"] != version:
                return "not-on-portal"
            archives = list(staged_dir.glob("*.zip"))
            if len(archives) != 1:
                return "install_failed"
            downloaded = archives[0]
            destination = mods_dir / downloaded.name

            # Downloads can overlap. Only the final changes to the live directory are serialized.
            with _archive_update_lock:
                if not previous.is_file():
                    return None if destination.is_file() else "not_installed"
                destination_existed = destination.exists()
                downloaded.replace(destination)
                if previous != destination:
                    try:
                        previous.unlink()
                    except OSError:
                        if not destination_existed:
                            destination.replace(downloaded)
                        raise
    except OSError as exc:
        logger.warning("Could not replace a mod archive (%s)", type(exc).__name__)
        return "install_failed"
    return None
