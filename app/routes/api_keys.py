"""HTML-действия API-ключей в разделе integrations."""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import auth as auth_lib
from ..db import audit, get_db
from ..i18n import tr
from ._settings_redirect import settings_redirect as _settings_redirect

router = APIRouter()


# Plaintext существует только до первого GET одноразовой страницы показа.
_api_key_plain_to_show: dict[str, dict] = {}


@router.post("/settings/api-keys/new")
def api_key_create(request: Request,
                   name: str = Form(...),
                   scope_read: str = Form(""),
                   scope_command: str = Form(""),
                   scope_control: str = Form(""),
                   scope_admin: str = Form(""),
                   expires_in_days: int = Form(0),
                   rate_limit_per_min: int = Form(60),
                   db: sqlite3.Connection = Depends(get_db)):
    """Создать новый API-ключ и перенаправить на одноразовый показ plain."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))

    scopes = set()
    if scope_read.strip().lower() in ("1", "true", "on", "yes"):
        scopes.add("read")
    if scope_command.strip().lower() in ("1", "true", "on", "yes"):
        scopes.add("command")
    if scope_control.strip().lower() in ("1", "true", "on", "yes"):
        scopes.add("control")
    if scope_admin.strip().lower() in ("1", "true", "on", "yes"):
        scopes.add("admin")

    if not scopes:
        return _settings_redirect(err=tr("auth_routes.api_key_scope_required"), tab="integrations")

    expires_at: int | None = None
    if expires_in_days > 0:
        if expires_in_days > 3650:
            return _settings_redirect(err=tr("auth_routes.api_key_maximum_lifetime"), tab="integrations")
        expires_at = int(time.time()) + expires_in_days * 86400

    try:
        key_id, plain = auth_lib.create_api_key(
            db, owner_user_id=user.user_id,
            name=name, scopes=scopes,
            expires_at=expires_at,
            rate_limit_per_min=rate_limit_per_min,
        )
    except auth_lib.ApiKeyError as e:
        return _settings_redirect(err=str(e), tab="integrations")

    audit(db, "api_key.create", None,
          summary=f"{user.username} created key '{name}' "
                  f"scopes={sorted(scopes)} "
                  f"expires={'never' if expires_at is None else expires_at}")
    db.commit()

    _api_key_plain_to_show[user.id] = {
        "key_id": key_id,
        "name": name,
        "scopes": sorted(scopes),
        "plain": plain,
        "expires_at": expires_at,
        "ts": int(time.time()),
    }
    return RedirectResponse(
        url=f"/settings/api-keys/{key_id}/show", status_code=303,
    )


@router.get("/settings/api-keys/{key_id}/show")
def api_key_show(key_id: int, request: Request,
                 db: sqlite3.Connection = Depends(get_db)):
    """Одноразовый показ plaintext только что созданного ключа."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))

    info = _api_key_plain_to_show.pop(user.id, None)
    if info is None or info["key_id"] != key_id:
        return _settings_redirect(
            err=tr("auth_routes.api_key_shown_once"), tab="integrations")
    if int(time.time()) - info["ts"] > 5 * 60:
        return _settings_redirect(err=tr("auth_routes.api_key_display_expired"), tab="integrations")

    return request.app.state.templates.TemplateResponse(
        request, "api_key_show.html",
        {
            "page": "settings",
            "username": user.username,
            "key_id": info["key_id"],
            "name": info["name"],
            "scopes": info["scopes"],
            "plain": info["plain"],
            "expires_at": info["expires_at"],
        },
    )


@router.get("/settings/api-keys/{key_id}/requests")
def api_key_requests(key_id: int, request: Request,
                     db: sqlite3.Connection = Depends(get_db)):
    """Последние 100 запросов ключа; ключи чужого пользователя не раскрываем."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))

    key_meta = auth_lib.get_api_key_by_id(db, key_id, user.user_id)
    if not key_meta:
        raise HTTPException(404, tr("auth_routes.api_key_not_found"))

    requests_log = auth_lib.list_api_requests_for_key(db, key_id, limit=100)
    return request.app.state.templates.TemplateResponse(
        request, "api_key_requests.html",
        {
            "page": "settings",
            "username": user.username,
            "key": key_meta,
            "requests": requests_log,
        },
    )


@router.post("/settings/api-keys/{key_id}/revoke")
def api_key_revoke(key_id: int, request: Request,
                   db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))

    key_meta = auth_lib.get_api_key_by_id(db, key_id, user.user_id)
    if not key_meta:
        return _settings_redirect(err=tr("auth_routes.api_key_not_found"), tab="integrations")
    if key_meta.get("revoked_at") is not None:
        return _settings_redirect(msg=tr("auth_routes.api_key_already_revoked", name=key_meta['name']), tab="integrations")
    if not auth_lib.revoke_api_key(db, key_id, user.user_id):
        return _settings_redirect(err=tr("auth_routes.api_key_revoke_failed"), tab="integrations")
    audit(db, "api_key.revoke", None,
          summary=f"{user.username} revoked key '{key_meta['name']}'")
    db.commit()
    return _settings_redirect(
        msg=tr("auth_routes.api_key_revoked", name=key_meta['name']), tab="integrations")


@router.post("/settings/api-keys/{key_id}/delete")
def api_key_hard_delete(key_id: int, request: Request,
                        db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))

    key_meta = auth_lib.get_api_key_by_id(db, key_id, user.user_id)
    if not key_meta:
        return _settings_redirect(err=tr("auth_routes.api_key_not_found"), tab="integrations")
    if key_meta.get("revoked_at") is None:
        return _settings_redirect(
            err=tr("auth_routes.api_key_revoke_before_delete", name=key_meta['name']),
            tab="integrations",
        )
    if not auth_lib.hard_delete_api_key(db, key_id, user.user_id):
        return _settings_redirect(err=tr("auth_routes.api_key_delete_failed"), tab="integrations")
    audit(db, "api_key.purge", None,
          summary=f"{user.username} purged revoked key '{key_meta['name']}'")
    db.commit()
    return _settings_redirect(
        msg=tr("auth_routes.api_key_deleted", name=key_meta['name']), tab="integrations")
