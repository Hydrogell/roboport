"""List, upload, rename, download, and select saved games."""
from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ... import auth as auth_lib
from ...db import audit, get_db
from ...i18n import tr
from ...runtime import get_runtime
from ...runtime.docker import DockerError
from ...server_data import spec_from_row
from ...server_saves import (LOADED_SAVE_NAME, InvalidSaveArchiveError,
                             delete_save, list_saves, rename_save, safe_save_name,
                             save_path, saves_dir, set_current_save, store_uploaded_save)
from ...factorio_mods import parse_save_mods
from .auth import ApiAuthError, require_api_key
from .common import load_server

router = APIRouter()


class SaveRenameRequest(BaseModel):
    new_name: str = Field(..., min_length=5, max_length=64,
                          description="Полное новое имя файла (с .zip)")


@router.get("/servers/{uuid}/saves", summary="Список сейвов")
def api_saves_list(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Все .zip из servers/<uuid>/saves/ с размером, mtime и (если получится)
    списком модов из level.dat0."""
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    items = list_saves(spec.data_dir, parse_save_mods)
    return {"uuid": uuid, "saves": items, "count": len(items),
            "current_save": "default.zip"}


@router.get("/servers/{uuid}/saves/{name}", summary="Скачать сейв")
def api_saves_download(
    uuid: str,
    name: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> FileResponse:
    """Скачать .zip сейва. Имя валидируется через safe_save_name."""
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    try:
        fname = safe_save_name(name)
    except Exception as e:
        raise ApiAuthError(400, "bad_save_name", str(e))
    target = save_path(saves_dir(spec.data_dir), fname)
    if not target.is_file():
        raise ApiAuthError(404, "not_found", tr("servers.save_not_found", name=name))
    return FileResponse(path=str(target), filename=fname, media_type="application/zip")


@router.post("/servers/{uuid}/saves/upload", summary="Загрузить сейв (multipart)")
def api_saves_upload(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    file: UploadFile = File(...),
) -> dict:
    """Multipart upload .zip-сейва в servers/<uuid>/saves/. Имя берётся из
    file.filename, валидируется (только [A-Za-z0-9_- .], `.zip` обязателен)."""
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    fname = (file.filename or "").strip()
    try:
        fname = safe_save_name(fname)
    except Exception as e:
        raise ApiAuthError(400, "bad_save_name", str(e))
    target = save_path(saves_dir(spec.data_dir), fname)
    try:
        written = store_uploaded_save(file.file, target)
    except InvalidSaveArchiveError:
        raise ApiAuthError(400, "not_a_zip", tr("api_v1.uploaded_save_invalid_zip"))
    audit(db, "save.upload", uuid,
          summary=f"api_key={key.name}: {fname} ({written} bytes)")
    db.commit()
    return {"uuid": uuid, "name": fname, "size": written}


@router.delete("/servers/{uuid}/saves/{name}", summary="Удалить сейв")
def api_saves_delete(
    uuid: str,
    name: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    confirm: Annotated[str, Query()] = "",
) -> dict:
    if confirm != "1":
        raise ApiAuthError(412, "confirmation_required", tr("api_v1.confirmation_required"))
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    try:
        fname = safe_save_name(name)
    except Exception as e:
        raise ApiAuthError(400, "bad_save_name", str(e))
    try:
        delete_save(saves_dir(spec.data_dir), fname)
    except FileNotFoundError:
        raise ApiAuthError(404, "not_found", tr("servers.save_not_found", name=name))
    audit(db, "save.delete", uuid, summary=f"api_key={key.name}: {fname}")
    db.commit()
    return {"uuid": uuid, "name": fname, "deleted": True}


@router.post("/servers/{uuid}/saves/{name}/set-current",
             summary="Сделать сейв текущим (default.zip)")
def api_saves_set_current(
    uuid: str,
    name: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Переименовывает <name> в default.zip; текущий default.zip уезжает в
    default-prev-<timestamp>.zip. Сервер должен быть остановлен (open file handle)."""
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    try:
        fname = safe_save_name(name)
    except Exception as e:
        raise ApiAuthError(400, "bad_save_name", str(e))
    if fname == LOADED_SAVE_NAME:
        return {"uuid": uuid, "name": fname, "already_current": True}
    rt = get_runtime(row["runtime"])
    try:
        if rt.status(spec).state.value == "running":
            raise ApiAuthError(409, "server_running",
                               tr("servers.save_stop_before_current_change"))
    except DockerError as e:
        raise ApiAuthError(502, "runtime_error", tr("api_v1.docker_error", detail=e))
    try:
        archived = set_current_save(saves_dir(spec.data_dir), fname)
    except FileNotFoundError:
        raise ApiAuthError(404, "not_found", tr("servers.save_not_found", name=name))
    audit(db, "save.set_current", uuid,
          summary=f"api_key={key.name}: {fname} -> default.zip"
                  + (f"; archived old as {archived}" if archived else ""))
    db.commit()
    return {"uuid": uuid, "name": fname, "is_current": True,
            "archived_previous": archived}


@router.post("/servers/{uuid}/saves/{name}/rename", summary="Переименовать сейв")
def api_saves_rename(
    uuid: str,
    name: str,
    body: SaveRenameRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    try:
        old_fname = safe_save_name(name)
        new_fname = safe_save_name(body.new_name)
    except Exception as e:
        raise ApiAuthError(400, "bad_save_name", str(e))
    if old_fname == new_fname:
        return {"uuid": uuid, "old_name": old_fname, "new_name": new_fname,
                "renamed": False, "reason": "same_name"}
    saves_d = saves_dir(spec.data_dir)
    try:
        rename_save(saves_d, old_fname, new_fname)
    except FileNotFoundError:
        raise ApiAuthError(404, "not_found", tr("servers.save_not_found", name=old_fname))
    except FileExistsError:
        raise ApiAuthError(409, "conflict", tr("servers.save_already_exists", name=new_fname))
    audit(db, "save.rename", uuid,
          summary=f"api_key={key.name}: {old_fname} -> {new_fname}")
    db.commit()
    return {"uuid": uuid, "old_name": old_fname, "new_name": new_fname,
            "renamed": True}
