"""Web actions for browsing, editing and downloading server files."""
from __future__ import annotations

import io
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from ..db import get_db
from ..i18n import tr
from ..server_data import spec_from_row
from ..server_files import MAX_TEXT_BYTES, is_text_file, is_within, safe_resolve
from .auth_routes import stepup_check_or_redirect
from .server_common import require_server


router = APIRouter()


@router.get("/{uuid}/files/download")
def files_download(uuid: str, path: str = "",
                   db: sqlite3.Connection = Depends(get_db)):
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    target = safe_resolve(spec.data_dir, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, tr("servers.file_not_found_path", path=path))
    return FileResponse(
        path=target,
        filename=target.name,
        media_type="application/octet-stream",
    )


@router.post("/{uuid}/files/upload")
async def files_upload(uuid: str,
                       path: str = Form(""),
                       file: UploadFile = File(...),
                       db: sqlite3.Connection = Depends(get_db)):
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    target_dir = safe_resolve(spec.data_dir, path)
    if not target_dir.is_dir():
        raise HTTPException(400, tr("servers.upload_directory_missing"))

    # Имя файла: только базовое имя без любых /, чтобы клиент не подсунул "../"
    fname = Path(file.filename or "").name
    if not fname:
        raise HTTPException(400, tr("servers.upload_name_empty"))
    dest = safe_resolve(target_dir, fname)  # на всякий ещё раз проверим

    # Стримим без чтения всего в память
    with dest.open("wb") as out:
        while chunk := await file.read(1 << 20):  # 1 MB
            out.write(chunk)
    return RedirectResponse(url=f"/servers/{uuid}/files?path={path}", status_code=303)


@router.post("/{uuid}/files/mkdir")
def files_mkdir(uuid: str,
                path: str = Form(""),
                name: str = Form(...),
                db: sqlite3.Connection = Depends(get_db)):
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    parent = safe_resolve(spec.data_dir, path)
    if not parent.is_dir():
        raise HTTPException(400, tr("servers.mkdir_parent_missing"))
    name = name.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(400, tr("servers.mkdir_name_invalid"))
    new_dir = safe_resolve(parent, name)
    new_dir.mkdir(exist_ok=False)
    return RedirectResponse(url=f"/servers/{uuid}/files?path={path}", status_code=303)


@router.post("/{uuid}/files/delete")
def files_delete(request: Request, uuid: str,
                 path: str = Form(...),
                 db: sqlite3.Connection = Depends(get_db)):
    if (r := stepup_check_or_redirect(request)) is not None: return r
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    target = safe_resolve(spec.data_dir, path)
    if target == spec.data_dir.resolve():
        raise HTTPException(400, tr("servers.delete_server_root"))
    if not target.exists():
        raise HTTPException(404, tr("servers.file_or_directory_not_found"))
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    parent = "/".join(path.replace("\\", "/").split("/")[:-1])
    return RedirectResponse(url=f"/servers/{uuid}/files?path={parent}", status_code=303)


@router.get("/{uuid}/files/text")
def files_text_get(uuid: str, path: str = "",
                   db: sqlite3.Connection = Depends(get_db)):
    """Вернуть содержимое файла как UTF-8 plain text для редактора в модалке."""
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    target = safe_resolve(spec.data_dir, path)
    if not target.is_file():
        raise HTTPException(404, tr("servers.file_not_found"))
    if not is_text_file(target):
        raise HTTPException(415, tr("servers.editor_requires_text"))
    if target.stat().st_size > MAX_TEXT_BYTES:
        raise HTTPException(413, tr("servers.editor_requires_download", limit=MAX_TEXT_BYTES // 1024 // 1024))
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, tr("servers.file_invalid_utf8"))
    return PlainTextResponse(text, headers={"Cache-Control": "no-store"})


@router.post("/{uuid}/files/text")
def files_text_save(uuid: str,
                    path: str = Body(..., embed=True),
                    content: str = Body(..., embed=True),
                    db: sqlite3.Connection = Depends(get_db)):
    """Сохранить текст обратно в файл. JSON-body, не form, чтобы не упереться
    в form-size-лимиты на больших конфигах."""
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    target = safe_resolve(spec.data_dir, path)
    if not target.is_file():
        raise HTTPException(404, tr("servers.editor_file_missing"))
    if not is_text_file(target):
        raise HTTPException(415, tr("servers.editor_can_only_save_text"))
    if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise HTTPException(413, tr("servers.editor_content_too_large", limit=MAX_TEXT_BYTES // 1024 // 1024))
    target.write_text(content, encoding="utf-8")
    st = target.stat()
    return {
        "ok": True,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


@router.post("/{uuid}/files/bulk-delete")
def files_bulk_delete(request: Request, uuid: str,
                      paths: List[str] = Form(...),
                      cwd: str = Form(""),
                      db: sqlite3.Connection = Depends(get_db)):
    if (r := stepup_check_or_redirect(request)) is not None: return r
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    base = spec.data_dir.resolve()
    for p in paths:
        target = safe_resolve(spec.data_dir, p)
        if target == base or not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    return RedirectResponse(url=f"/servers/{uuid}/files?path={cwd}", status_code=303)


@router.post("/{uuid}/files/zip-download")
def files_zip_download(uuid: str,
                       paths: List[str] = Form(...),
                       db: sqlite3.Connection = Depends(get_db)):
    """Стримить выбранное в zip. Сохраняем относительные пути в архиве,
    чтобы клиент видел структуру (например saves/default.zip → roboport-XXX/saves/default.zip)."""
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    base = spec.data_dir.resolve()

    # Резолвим заранее — если что не так, отдадим 400 до начала стрима
    targets: list[tuple[str, Path]] = []
    for p in paths:
        t = safe_resolve(spec.data_dir, p)
        if t == base or not t.exists():
            continue
        rel = t.relative_to(base).as_posix()
        targets.append((rel, t))
    if not targets:
        raise HTTPException(400, tr("servers.zip_nothing_to_archive"))

    def stream_zip():
        # ZipFile умеет писать в любой file-like; пишем в BytesIO батчами
        # и yield-ом отдаём порции. Для очень больших архивов лучше через
        # промежуточный файл, но для модов/сейвов BytesIO нормально.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for rel, t in targets:
                if t.is_file():
                    zf.write(t, arcname=rel)
                else:
                    for sub in t.rglob("*"):
                        if not sub.is_symlink() and is_within(base, sub) and sub.is_file():
                            zf.write(sub, arcname=sub.relative_to(base).as_posix())
        buf.seek(0)
        while chunk := buf.read(1 << 20):
            yield chunk

    fname = f"roboport-{row['name']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        stream_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
