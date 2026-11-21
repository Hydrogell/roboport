"""HTML-действия вебхуков в разделе integrations."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from .. import webhooks as webhook_service
from ..db import audit, get_db
from ..i18n import tr
from ._settings_redirect import settings_redirect as _settings_redirect

router = APIRouter()


@router.post("/settings/webhooks/new")
def webhook_create(request: Request,
                   name: str = Form(...),
                   url: str = Form(...),
                   events: list[str] = Form(default=[]),
                   db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    if "*" in events:
        events_set = set(webhook_service.EVENTS_ALL)
    else:
        events_set = {e for e in events if e in webhook_service.EVENTS_ALL}

    try:
        webhook_service.create_webhook(
            db, owner_user_id=user.user_id,
            name=name, url=url, events=events_set,
        )
    except webhook_service.WebhookError as e:
        return _settings_redirect(err=str(e), tab="integrations")
    audit(db, "webhook.create", None,
          summary=f"{user.username} created webhook {name!r} → {url}")
    db.commit()
    return _settings_redirect(msg=tr("auth_routes.webhook_created", name=name), tab="integrations")


@router.post("/settings/webhooks/{hook_id}/delete")
def webhook_delete(hook_id: int, request: Request,
                   db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    hook = webhook_service.get_webhook(db, hook_id, user.user_id)
    if not hook:
        return _settings_redirect(err=tr("auth_routes.webhook_not_found"), tab="integrations")
    webhook_service.delete_webhook(db, hook_id, user.user_id)
    audit(db, "webhook.delete", None,
          summary=f"{user.username} deleted webhook {hook['name']!r}")
    db.commit()
    return _settings_redirect(msg=tr("auth_routes.webhook_deleted", name=hook['name']), tab="integrations")


@router.post("/settings/webhooks/{hook_id}/toggle")
def webhook_toggle(hook_id: int, request: Request,
                   enabled: str = Form("0"),
                   db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    new_val = enabled.strip().lower() in ("1", "true", "on", "yes")
    webhook_service.update_webhook(db, hook_id, user.user_id, enabled=new_val)
    audit(db, "webhook.toggle", None,
          summary=f"{user.username} webhook id={hook_id} enabled={new_val}")
    db.commit()
    return _settings_redirect(msg=tr("auth_routes.webhook_updated"), tab="integrations")


@router.post("/settings/webhooks/{hook_id}/resume")
def webhook_resume(hook_id: int, request: Request,
                   db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    if not webhook_service.resume_webhook(db, hook_id, user.user_id):
        return _settings_redirect(err=tr("auth_routes.webhook_not_found"), tab="integrations")
    audit(db, "webhook.resume", None,
          summary=f"{user.username} resumed webhook id={hook_id}")
    db.commit()
    return _settings_redirect(msg=tr("auth_routes.webhook_resumed"), tab="integrations")


@router.post("/settings/webhooks/{hook_id}/test")
async def webhook_test(hook_id: int, request: Request,
                       db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    hook = webhook_service.get_webhook(db, hook_id, user.user_id)
    if not hook:
        return _settings_redirect(err=tr("auth_routes.webhook_not_found"), tab="integrations")
    result = await webhook_service.test_ping(hook)
    if result["ok"]:
        msg = tr(
            "auth_routes.webhook_test_passed",
            name=hook['name'],
            status=result['status'],
            duration=result['duration_ms'],
        )
        return _settings_redirect(msg=msg, tab="integrations")
    err = result.get("error") or f"HTTP {result.get('status')}"
    return _settings_redirect(
        err=f"Webhook «{hook['name']}»: {err}", tab="integrations")
