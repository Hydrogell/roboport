"""Web actions for Factorio save archives."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from ..db import get_db
from ..i18n import tr
from ..runtime import get_runtime
from ..runtime.docker import DockerError
from ..server_data import spec_from_row
from ..server_saves import (LOADED_SAVE_NAME, InvalidSaveArchiveError,
                            delete_save, rename_save, safe_save_name, saves_dir,
                            save_path, set_current_save, store_uploaded_save)
from .auth_routes import stepup_check_or_redirect
from .server_common import require_server, server_tab_redirect


router = APIRouter()


@router.post("/{uuid}/saves/save-now")
def saves_save_now(uuid: str, db: sqlite3.Connection = Depends(get_db)):
    """RCON /server-save — сервер сам атомарно сохранит текущий мир в свой save-файл
    (для нашего образа — saves/default.zip, см. SAVE_NAME=default в DockerRuntime)."""
    from ..runtime.rcon import RconAuthError, RconError
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    rt = get_runtime(row["runtime"])
    try:
        st = rt.status(spec)
    except DockerError as e:
        return server_tab_redirect(uuid, "saves", err=tr("api_v1.docker_error", detail=e))
    if st.state.value != "running":
        return server_tab_redirect(uuid, "saves", err=tr("servers.save_server_not_running"))
    try:
        resp = rt.send_command(spec, "/server-save")
    except RconAuthError as e:
        return server_tab_redirect(uuid, "saves", err=tr("api_v1.rcon_auth_error", detail=e))
    except (RconError, DockerError, ConnectionError, OSError) as e:
        return server_tab_redirect(uuid, "saves", err=tr("api_v1.rcon_error", detail=e))
    txt = (resp or "").strip() or tr("servers.save_completed")
    return server_tab_redirect(uuid, "saves", msg=f"/server-save: {txt}")


@router.post("/{uuid}/saves/upload")
def saves_upload(uuid: str,
                 file: UploadFile = File(...),
                 db: sqlite3.Connection = Depends(get_db)):
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    fname = Path(file.filename or "").name
    try:
        fname = safe_save_name(fname)
    except HTTPException as e:
        return server_tab_redirect(uuid, "saves", err=str(e.detail))
    dest = save_path(saves_dir(spec.data_dir), fname)
    try:
        store_uploaded_save(file.file, dest)
    except InvalidSaveArchiveError:
        return server_tab_redirect(uuid, "saves", err=tr("servers.save_invalid_zip", name=fname))
    except Exception as e:
        return server_tab_redirect(uuid, "saves", err=tr("servers.save_upload_failed", detail=e))
    return server_tab_redirect(uuid, "saves", msg=tr("servers.save_uploaded", name=fname))


@router.get("/{uuid}/saves/download")
def saves_download(uuid: str, name: str = "",
                   db: sqlite3.Connection = Depends(get_db)):
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    fname = safe_save_name(name)
    target = save_path(saves_dir(spec.data_dir), fname)
    if not target.is_file():
        raise HTTPException(404, tr("servers.save_not_found", name=fname))
    return FileResponse(path=target, filename=target.name, media_type="application/zip")


@router.post("/{uuid}/saves/delete")
def saves_delete(request: Request, uuid: str,
                 name: str = Form(...),
                 db: sqlite3.Connection = Depends(get_db)):
    if (r := stepup_check_or_redirect(request)) is not None: return r
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    try:
        fname = safe_save_name(name)
    except HTTPException as e:
        return server_tab_redirect(uuid, "saves", err=str(e.detail))
    try:
        delete_save(saves_dir(spec.data_dir), fname)
    except FileNotFoundError:
        return server_tab_redirect(uuid, "saves", err=tr("servers.save_not_found", name=fname))
    return server_tab_redirect(uuid, "saves", msg=tr("servers.save_deleted", name=fname))


@router.post("/{uuid}/saves/rename")
def saves_rename(uuid: str,
                 old: str = Form(...),
                 new: str = Form(...),
                 db: sqlite3.Connection = Depends(get_db)):
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    try:
        old_fname = safe_save_name(old)
        new_fname = safe_save_name(new)
    except HTTPException as e:
        return server_tab_redirect(uuid, "saves", err=str(e.detail))
    if old_fname == new_fname:
        return server_tab_redirect(uuid, "saves", msg=tr("servers.save_name_unchanged"))
    saves_d = saves_dir(spec.data_dir)
    try:
        rename_save(saves_d, old_fname, new_fname)
    except FileNotFoundError:
        return server_tab_redirect(uuid, "saves", err=tr("servers.save_not_found", name=old_fname))
    except FileExistsError:
        return server_tab_redirect(uuid, "saves", err=tr("servers.save_already_exists", name=new_fname))
    return server_tab_redirect(uuid, "saves", msg=tr("servers.save_renamed", old_name=old_fname, new_name=new_fname))


@router.post("/{uuid}/saves/set-current")
def saves_set_current(request: Request, uuid: str,
                      name: str = Form(...),
                      db: sqlite3.Connection = Depends(get_db)):
    """Сделать выбранный сейв тем, который грузится при следующем старте контейнера.
    Реализация: переименовываем целевой сейв в `default.zip`, а текущий default.zip
    (если есть) — в `default-prev-<timestamp>.zip`, чтобы не потерять. Сервер должен
    быть остановлен — Factorio держит open-handle на загруженный сейв."""
    if (r := stepup_check_or_redirect(request)) is not None: return r
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    try:
        fname = safe_save_name(name)
    except HTTPException as e:
        return server_tab_redirect(uuid, "saves", err=str(e.detail))
    if fname == LOADED_SAVE_NAME:
        return server_tab_redirect(uuid, "saves", msg=tr("servers.save_already_current"))
    rt = get_runtime(row["runtime"])
    try:
        if rt.status(spec).state.value == "running":
            return server_tab_redirect(uuid, "saves", err=tr("servers.save_stop_before_current_change"))
    except DockerError as e:
        return server_tab_redirect(uuid, "saves", err=tr("api_v1.docker_error", detail=e))
    try:
        set_current_save(saves_dir(spec.data_dir), fname)
    except FileNotFoundError:
        return server_tab_redirect(uuid, "saves", err=tr("servers.save_not_found", name=fname))
    return server_tab_redirect(uuid, "saves", msg=tr("servers.save_next_start", name=fname, loaded_name=LOADED_SAVE_NAME))
