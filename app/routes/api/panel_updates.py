"""Panel version, updates, and rollback."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field

from ... import auth as auth_lib
from ... import panel_update
from ...db import audit, get_db
from ...i18n import tr
from .auth import ApiAuthError, require_api_key

router = APIRouter()


@router.get("/panel/version", summary="Текущая версия панели (публичный)", openapi_extra={"security": []})
def api_panel_version() -> dict:
    """Возвращает установленную версию панели для мониторинга."""
    return {"version": panel_update.read_version()}


@router.get("/panel/update/check", summary="Проверить latest release на GitHub")
def api_panel_update_check(
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    repo: Annotated[str | None, Query(max_length=120)] = None,
) -> dict:
    """Дёргает GitHub Releases API. По умолчанию использует репо из .env
    (PANEL_UPDATE_REPO) или дефолтный."""
    use_repo = (repo or panel_update.get_repo()).strip()
    try:
        rel = panel_update.check_github(use_repo)
    except RuntimeError as e:
        raise ApiAuthError(502, "github_check_failed", str(e))
    current = panel_update.read_version()
    return {
        "current": current,
        "repo": use_repo,
        "latest": {
            "tag": rel.tag,
            "name": rel.name,
            "body": rel.body,
            "published_at": rel.published_at,
            "html_url": rel.html_url,
        },
        "update_available": panel_update.is_newer(rel.tag, current),
    }


class PanelInstallRequest(BaseModel):
    source: str = Field(..., description="'github' или 'upload'")
    tag: str | None = Field(None, description="Тег релиза если source=github")
    upload_filename: str | None = Field(None, description="Имя файла из /panel/update/upload")


@router.post("/panel/update/install", summary="Установить апдейт",
             status_code=202)
def api_panel_update_install(
    body: PanelInstallRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Запустить апдейт. Возвращает 202 + update_id для polling'а status'а.
    Через ~10-60 сек панель перезапустится."""
    if body.source == "github":
        if not body.tag:
            raise ApiAuthError(400, "missing_tag", tr("settings.update_tag_required"))
        try:
            plan = panel_update.prepare_from_github(panel_update.get_repo(), body.tag)
        except RuntimeError as e:
            raise ApiAuthError(502, "download_failed", str(e))
    elif body.source == "upload":
        if not body.upload_filename:
            raise ApiAuthError(400, "missing_filename",
                               tr("settings.update_upload_filename_required"))
        archive = panel_update.STAGING_DIR / body.upload_filename
        if not archive.exists():
            raise ApiAuthError(404, "upload_not_found",
                               tr("settings.update_upload_missing", filename=body.upload_filename))
        plan = panel_update.prepare_from_upload(archive)
    else:
        raise ApiAuthError(400, "bad_source", tr("api_v1.update_source_invalid"))

    try:
        update_id = panel_update.execute_install(
            db, plan,
            initiated_by=f"api:{key.name}",
        )
    except RuntimeError as e:
        raise ApiAuthError(500, "install_failed", str(e))

    audit(db, action="panel.update.started", server_uuid=None,
          actor=f"api:{key.name}", details={"update_id": update_id, "source": plan.source})
    return {"update_id": update_id, "status": "running",
            "message": tr("api_v1.update_install_started")}


@router.post("/panel/update/upload", summary="Загрузить tar.gz/zip-архив",
             status_code=200)
def api_panel_update_upload(
    file: Annotated[UploadFile, File(...)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Принять multipart-файл, положить в .update-staging/. После — отдельный
    POST /panel/update/install с source=upload и upload_filename."""
    if not file.filename:
        raise ApiAuthError(400, "no_filename", tr("settings.update_file_not_selected"))
    fname = Path(file.filename).name  # strip path
    if not (fname.endswith(".tar.gz") or fname.endswith(".tgz") or fname.endswith(".zip")):
        raise ApiAuthError(400, "bad_format", tr("settings.update_file_type_invalid"))

    panel_update.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    dst = panel_update.STAGING_DIR / fname
    with open(dst, "wb") as f:
        while chunk := file.file.read(64 * 1024):
            f.write(chunk)
    return {"filename": fname, "size": dst.stat().st_size}


@router.get("/panel/update/status/{update_id}", summary="Статус апдейта")
def api_panel_update_status(
    update_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    rec = panel_update.get_history(db, update_id)
    if not rec:
        raise ApiAuthError(404, "not_found", tr("api_v1.update_not_found", id=update_id))
    return rec


@router.get("/panel/update/history", summary="История апдейтов")
def api_panel_update_history(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    return {"updates": panel_update.list_history(db, limit=limit)}


@router.post("/panel/update/rollback/{update_id}", summary="Откатиться к бэкапу",
             status_code=202)
def api_panel_update_rollback(
    update_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Восстановить файлы из бэкапа указанного апдейта. Создаёт новую запись
    panel_updates с source='rollback:<id>'."""
    try:
        rb_id = panel_update.execute_rollback(
            db, src_update_id=update_id,
            initiated_by=f"api:{key.name}",
        )
    except RuntimeError as e:
        raise ApiAuthError(400, "rollback_failed", str(e))
    audit(db, action="panel.update.rollback", server_uuid=None,
          actor=f"api:{key.name}", details={"src_update_id": update_id, "rollback_id": rb_id})
    return {"update_id": rb_id, "status": "running",
            "message": tr("api_v1.update_rollback_started")}
