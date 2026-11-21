"""HTML and browser actions for installed Factorio mods."""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from .. import factorio_mods, server_data
from ..db import get_db
from ..i18n import tr
from .server_common import require_server

router = APIRouter(prefix="/servers")


@router.post("/{uuid}/mods/install")
def mods_install(uuid: str, name: str = Form(...),
                 db: sqlite3.Connection = Depends(get_db)):
    """Установить (или обновить) мод name. Тянет последний релиз для factorio-версии сервера."""
    row = require_server(db, uuid)
    factorio_v = factorio_mods.factorio_major_minor(row["version"])
    user, token = factorio_mods.get_credentials()
    if not user:
        raise HTTPException(400, tr("mods.factorio_credentials_required"))

    rel = factorio_mods.find_latest_release(name, factorio_v)
    if not rel:
        raise HTTPException(404, tr("mods.release_not_found", name=name, version=factorio_v))

    target_dir = server_data.SERVERS_DIR / uuid / "mods"
    target_dir.mkdir(exist_ok=True)
    # Если уже стоит этот мод (любой версии) — снести старые zip перед установкой
    for old in target_dir.glob(f"{name}_*.zip"):
        old.unlink()

    factorio_mods.download_release(rel, user, token, target_dir)
    return RedirectResponse(url=f"/servers/{uuid}/mods", status_code=303)


@router.post("/{uuid}/mods/remove")
def mods_remove(uuid: str, filename: str = Form(...),
                db: sqlite3.Connection = Depends(get_db)):
    """Удалить файл мода по имени файла (без traversal)."""
    require_server(db, uuid)
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise HTTPException(400, tr("common.invalid_filename"))
    target = server_data.SERVERS_DIR / uuid / "mods" / filename
    if not target.is_file() or target.suffix != ".zip":
        raise HTTPException(404, tr("mods.file_not_found"))
    target.unlink()
    return RedirectResponse(url=f"/servers/{uuid}/mods", status_code=303)


@router.post("/{uuid}/mods/upload")
async def mods_upload(uuid: str, file: UploadFile = File(...),
                      db: sqlite3.Connection = Depends(get_db)):
    """Загрузить произвольный .zip в mods/ (для модов вне портала)."""
    require_server(db, uuid)
    fname = Path(file.filename or "").name
    if not fname.endswith(".zip"):
        raise HTTPException(400, tr("mods.zip_files_only"))
    target_dir = server_data.SERVERS_DIR / uuid / "mods"
    target_dir.mkdir(exist_ok=True)
    dest = target_dir / fname
    with dest.open("wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    # Базовая валидация: можем ли вообще распарсить info.json
    if factorio_mods.read_mod_info(dest) is None:
        dest.unlink()
        raise HTTPException(400, tr("mods.archive_invalid"))
    return RedirectResponse(url=f"/servers/{uuid}/mods", status_code=303)


@router.post("/{uuid}/mods/toggle")
def mods_toggle(uuid: str, name: str = Form(...), enabled: str = Form("1"),
                db: sqlite3.Connection = Depends(get_db)):
    """Включить/выключить мод через mod-list.json. Чекбокс в форме шлёт enabled='1'/'0'."""
    require_server(db, uuid)
    server_dir = server_data.SERVERS_DIR / uuid
    state = factorio_mods.read_modlist(server_dir)
    state[name] = (enabled == "1")
    factorio_mods.write_modlist(server_dir, state)
    return RedirectResponse(url=f"/servers/{uuid}/mods", status_code=303)


def _detect_zip_kind(zf: zipfile.ZipFile) -> str:
    """Что это: 'save' (есть */level.dat0) / 'mod' (есть */info.json в верхней папке) / 'unknown'."""
    names = zf.namelist()
    if any(n.endswith("/level.dat0") for n in names):
        return "save"
    if any(n.endswith("/info.json") and n.count("/") == 1 for n in names):
        return "mod"
    return "unknown"


@router.post("/{uuid}/mods/import")
async def mods_import(uuid: str, file: UploadFile = File(...),
                      db: sqlite3.Connection = Depends(get_db)):
    """Универсальный приёмник для drop-zone: автодетект типа файла → нужный обработчик.

    Поддерживается:
      - .json как mod-list (есть ключ 'mods': [...]) → ставим все enabled через портал
      - .zip как save (есть */level.dat0) → парсим список модов → ставим недостающие;
        также копируем сам сейв в saves/<filename>
      - .zip как mod (есть */info.json в верхней папке) → переписываем в mods/

    Возвращает JSON с полями: ok, kind, message, details. Без редиректов —
    фронт сам обновит UI после batch'а.
    """
    row = require_server(db, uuid)
    factorio_v = factorio_mods.factorio_major_minor(row["version"])
    server_dir = server_data.SERVERS_DIR / uuid
    fname = Path(file.filename or "").name or "upload"

    raw = await file.read()
    if not raw:
        return {"ok": False, "kind": "unknown", "message": tr("mods.empty_file")}

    # 1) Пробуем как JSON (mod-list)
    if fname.endswith(".json") or raw[:1] in (b"{", b"["):
        try:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("mods"), list):
                wanted = [{"name": m["name"], "version": None}
                          for m in data["mods"]
                          if isinstance(m, dict) and m.get("name")
                          and m.get("enabled", True)]
                s = factorio_mods.install_missing(wanted, server_dir / "mods", factorio_v)
                msg = (tr(
                    "mods.import_mod_list",
                    installed=len(s['installed']),
                    skipped=len(s['skipped']),
                )
                       + (tr("mods.import_failed_count", failed=len(s['failed'])) if s["failed"] else ""))
                return {"ok": True, "kind": "modlist", "message": msg, "details": s}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"ok": False, "kind": "unknown", "message": tr("mods.invalid_json")}

    # 2) ZIP — может быть save или mod. Записываем во временный seekable.
    if not (fname.endswith(".zip") or raw[:4] == b"PK\x03\x04"):
        return {"ok": False, "kind": "unknown",
                "message": tr("mods.import_unsupported_file")}
    tmp = server_dir / f".upload-{int(time.time() * 1000)}.zip"
    tmp.write_bytes(raw)
    try:
        try:
            zf = zipfile.ZipFile(tmp)
        except zipfile.BadZipFile:
            return {"ok": False, "kind": "unknown", "message": tr("mods.corrupt_zip")}
        with zf:
            kind = _detect_zip_kind(zf)
        if kind == "save":
            save_mods = factorio_mods.parse_save_mods(tmp)
            if not save_mods:
                return {"ok": False, "kind": "save",
                        "message": tr("mods.import_save_mod_list_missing")}
            s = factorio_mods.install_missing(save_mods, server_dir / "mods", factorio_v)
            # Сейв всегда копируем в saves/<original-name> — пользователь может
            # потом на нём запустить сервер. Перезапись допускается.
            saves_dir = server_dir / "saves"
            saves_dir.mkdir(exist_ok=True)
            shutil.copyfile(tmp, saves_dir / fname)
            msg = (tr(
                "mods.import_save",
                found=len(save_mods),
                installed=len(s['installed']),
                skipped=len(s['skipped']),
            )
                   + (tr("mods.import_failed_count", failed=len(s['failed'])) if s["failed"] else "")
                   + tr("mods.import_save_written", filename=fname))
            return {"ok": True, "kind": "save", "message": msg, "details": s}
        elif kind == "mod":
            try:
                m = factorio_mods.install_mod_from_zip(tmp, server_dir / "mods")
            except ValueError as e:
                return {"ok": False, "kind": "mod", "message": str(e)}
            return {"ok": True, "kind": "mod",
                    "message": tr("mods.import_mod_installed", title=m['title'], version=m['version']),
                    "details": m}
        else:
            return {"ok": False, "kind": "unknown",
                    "message": tr("mods.import_zip_unrecognized")}
    finally:
        tmp.unlink(missing_ok=True)


@router.get("/{uuid}/mods/updates-check")
def mods_updates_check(uuid: str, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Список модов сервера у которых есть обновление в локальном кэше.
    Используется UI'ем для модалки «Проверить обновления»: возвращает только
    моды с has_update=True. Не дёргает mod portal — работает на уже
    закэшированном листинге (синхронизация — отдельная кнопка ↻ Обновить кэш).

    Возвращает:
        {
            "cache": {"synced_at": int, "count": int, "stale": bool},
            "factorio_version": "2.0",
            "updates": [{"name", "title", "current", "latest", "size_kb"}, ...]
        }
    cache.stale=True если синк старше 24 часов — UI предложит сначала ↻ Обновить.
    """
    row = require_server(db, uuid)
    factorio_v = factorio_mods.factorio_major_minor(row["version"])
    return factorio_mods.available_updates(server_data.SERVERS_DIR / uuid, factorio_v)


@router.post("/{uuid}/mods/sync")
def mods_sync(request: Request, uuid: str,
              db: sqlite3.Connection = Depends(get_db)):
    """Перетянуть полный листинг с mods.factorio.com. Делается синхронно — на проде
    стоит вынести в фоновую задачу, но для соло-админа сейчас норм (~30 сек, 12 МБ).

    Content-negotiation: для AJAX (Accept: application/json или X-Requested-With:
    fetch) возвращаем JSON-результат; для обычного form-submit'а — 303 redirect
    обратно на страницу модов (legacy-флоу через кнопку «↻ Обновить кэш»)."""
    require_server(db, uuid)
    try:
        payload = factorio_mods.refresh_catalog()
    except factorio_mods.PortalListingError as e:
        # AJAX-ветка: 502 с JSON. Иначе — re-raise HTTPException для form-flow.
        accept = request.headers.get("accept", "")
        if "application/json" in accept:
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": str(e)},
            )
        raise HTTPException(502, tr("mods.listing_download_failed", detail=e))
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "synced_at": payload["synced_at"], "count": payload["count"]}
    return RedirectResponse(url=f"/servers/{uuid}/mods", status_code=303)
