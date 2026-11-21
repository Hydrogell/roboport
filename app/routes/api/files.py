"""Browse, edit, upload, and remove server files."""
from __future__ import annotations

import shutil
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ... import auth as auth_lib
from ...db import audit, get_db
from ...i18n import tr
from ...server_data import spec_from_row
from ...server_files import MAX_TEXT_BYTES, is_text_file, list_dir, safe_resolve
from .auth import ApiAuthError, require_api_key
from .common import load_server

router = APIRouter()


class FileWriteRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., max_length=10 * 1024 * 1024,
                         description="UTF-8 text content; binaries — через /upload")


class MkdirRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=500)


class BulkDeleteRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1, max_length=500)


@router.get("/servers/{uuid}/files", summary="Листинг папки (scope=admin)")
def api_files_list(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    path: Annotated[str, Query(max_length=500)] = "",
) -> dict:
    """Листинг директории внутри servers/<uuid>/. path — относительный, пустой
    = корень. Каждая запись: name, is_dir, size, mtime."""
    row = load_server(db, uuid)
    base = spec_from_row(row).data_dir
    target = safe_resolve(base, path)
    if not target.is_dir():
        raise ApiAuthError(404, "not_dir", tr("api_v1.path_not_directory", path=repr(path)))
    entries = list_dir(target, base)
    return {"uuid": uuid, "path": path, "entries": entries, "count": len(entries)}


@router.get("/servers/{uuid}/files/raw", summary="Скачать файл (scope=admin)")
def api_files_raw(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    path: Annotated[str, Query(min_length=1, max_length=500)] = "",
) -> FileResponse:
    """Стрим бинарного содержимого. Для текста удобнее `GET /files/text`."""
    row = load_server(db, uuid)
    base = spec_from_row(row).data_dir
    target = safe_resolve(base, path)
    if not target.is_file():
        raise ApiAuthError(404, "not_file", tr("api_v1.path_not_file", path=repr(path)))
    return FileResponse(path=str(target), filename=target.name,
                        media_type="application/octet-stream")


@router.get("/servers/{uuid}/files/text", summary="Текстовое содержимое")
def api_files_text_get(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    path: Annotated[str, Query(min_length=1, max_length=500)] = "",
) -> dict:
    """Возвращает UTF-8 содержимое файла + базовые stat'ы."""
    row = load_server(db, uuid)
    base = spec_from_row(row).data_dir
    target = safe_resolve(base, path)
    if not target.is_file():
        raise ApiAuthError(404, "not_file", tr("api_v1.path_not_file", path=repr(path)))
    if not is_text_file(target):
        raise ApiAuthError(415, "not_text", tr("api_v1.file_not_text"))
    if target.stat().st_size > MAX_TEXT_BYTES:
        raise ApiAuthError(413, "too_large",
                           tr("api_v1.file_too_large", limit=MAX_TEXT_BYTES // (1024*1024)))
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ApiAuthError(400, "not_utf8", tr("api_v1.file_invalid_utf8"))
    st = target.stat()
    return {"uuid": uuid, "path": path, "content": content,
            "size": st.st_size, "mtime": int(st.st_mtime)}


@router.put("/servers/{uuid}/files/text", summary="Записать текстовый файл")
def api_files_text_put(
    uuid: str,
    body: FileWriteRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Перезаписывает текстовый файл целиком. Создаёт промежуточные директории.
    Бинарные файлы — через `/upload`. Контент UTF-8, max 10 МБ."""
    row = load_server(db, uuid)
    base = spec_from_row(row).data_dir
    target = safe_resolve(base, body.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    audit(db, "file.write", uuid,
          summary=f"api_key={key.name}: text {body.path} ({len(body.content)} chars)")
    db.commit()
    st = target.stat()
    return {"uuid": uuid, "path": body.path, "size": st.st_size,
            "mtime": int(st.st_mtime)}


@router.post("/servers/{uuid}/files/upload", summary="Загрузить файл (multipart)")
async def api_files_upload(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    file: UploadFile = File(...),
    path: Annotated[str, Query(max_length=500)] = "",
) -> dict:
    """Multipart upload. `path` (query) — относительная папка-назначение.
    Имя файла берётся из uploaded.filename. Перезаписывает существующий."""
    row = load_server(db, uuid)
    base = spec_from_row(row).data_dir
    dest_dir = safe_resolve(base, path) if path else base
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not dest_dir.is_dir():
        raise ApiAuthError(400, "not_dir", tr("api_v1.path_not_directory", path=repr(path)))
    fname = (file.filename or "").strip()
    if not fname or "/" in fname or "\\" in fname:
        raise ApiAuthError(400, "bad_filename", tr("common.invalid_filename"))
    target = safe_resolve(dest_dir, fname)
    # Стримим без буферизации в RAM — может быть многомегабайтный сейв.
    written = 0
    with target.open("wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk: break
            out.write(chunk)
            written += len(chunk)
    audit(db, "file.upload", uuid,
          summary=f"api_key={key.name}: {target.relative_to(base)} ({written} bytes)")
    db.commit()
    return {"uuid": uuid, "path": str(target.relative_to(base)).replace("\\", "/"),
            "size": written}


@router.post("/servers/{uuid}/files/mkdir", summary="Создать директорию")
def api_files_mkdir(
    uuid: str,
    body: MkdirRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Создаёт директорию (с промежуточными). Идемпотентно."""
    row = load_server(db, uuid)
    base = spec_from_row(row).data_dir
    target = safe_resolve(base, body.path)
    target.mkdir(parents=True, exist_ok=True)
    audit(db, "file.mkdir", uuid,
          summary=f"api_key={key.name}: {body.path}")
    db.commit()
    return {"uuid": uuid, "path": body.path, "created": True}


@router.delete("/servers/{uuid}/files", summary="Удалить файл/папку")
def api_files_delete(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    path: Annotated[str, Query(min_length=1, max_length=500)] = "",
    confirm: Annotated[str, Query()] = "",
) -> dict:
    """Удаляет файл или директорию (recursive). `?confirm=1` обязателен.
    Корень сервера удалить нельзя (для этого `DELETE /servers/{uuid}`)."""
    if confirm != "1":
        raise ApiAuthError(412, "confirmation_required", tr("api_v1.confirmation_required"))
    row = load_server(db, uuid)
    base = spec_from_row(row).data_dir
    target = safe_resolve(base, path)
    if target == base.resolve():
        raise ApiAuthError(400, "bad_request", tr("servers.delete_server_root"))
    if not target.exists():
        raise ApiAuthError(404, "not_found", tr("api_v1.path_not_found", path=repr(path)))
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    audit(db, "file.delete", uuid, summary=f"api_key={key.name}: {path}")
    db.commit()
    return {"uuid": uuid, "path": path, "deleted": True}


@router.post("/servers/{uuid}/files/bulk-delete", summary="Удалить несколько")
def api_files_bulk_delete(
    uuid: str,
    body: BulkDeleteRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Массовое удаление. Не-существующие пропускает молча. Корень нельзя."""
    row = load_server(db, uuid)
    base = spec_from_row(row).data_dir.resolve()
    deleted: list[str] = []
    skipped: list[str] = []
    for p in body.paths:
        try:
            target = safe_resolve(base, p)
        except Exception:
            skipped.append(p)
            continue
        if target == base or not target.exists():
            skipped.append(p)
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        deleted.append(p)
    audit(db, "file.bulk_delete", uuid,
          summary=f"api_key={key.name}: deleted {len(deleted)}, skipped {len(skipped)}")
    db.commit()
    return {"uuid": uuid, "deleted": deleted, "skipped": skipped}
