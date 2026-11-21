"""Webhook configuration, testing, and delivery history."""
from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ... import auth as auth_lib
from ... import webhooks as webhook_service
from ...db import audit, get_db
from ...i18n import tr
from .auth import ApiAuthError, require_api_key

router = APIRouter()


class WebhookCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    url: str = Field(..., min_length=8, max_length=500,
                     pattern=r"^https?://")
    events: list[str] = Field(...,
                              description="Список событий или ['*'] = все. "
                                          "Доступные см. /api/v1/webhooks/events")


class WebhookPatchRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    url: str | None = Field(None, min_length=8, max_length=500,
                            pattern=r"^https?://")
    events: list[str] | None = None
    enabled: bool | None = None


def _webhook_owner_id(db: sqlite3.Connection) -> int | None:
    """The v1 API uses the first panel account as its webhook owner."""
    row = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def _require_webhook_owner_id(db: sqlite3.Connection) -> int:
    owner_id = _webhook_owner_id(db)
    if owner_id is None:
        raise ApiAuthError(500, "internal_error", tr("api_v1.webhook_no_users"))
    return owner_id


def _webhook_dict(row: dict, *, redact_secret: bool = False) -> dict:
    """Standard webhook representation для API ответа."""
    d = dict(row)
    if redact_secret:
        d["secret"] = "***"
    return d


@router.get("/webhooks/events", summary="Список доступных событий")
def api_webhook_events(
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Все события на которые может подписаться webhook. Используй `*` чтобы
    подписаться на все сразу."""
    return {"events": list(webhook_service.EVENTS_ALL), "wildcard": "*"}


@router.get("/webhooks", summary="Список веб-хуков (scope=admin)")
def api_webhooks_list(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Все веб-хуки текущего юзера (owner). Secret виден — он shared с ботом
    и нужен для верификации подписи."""
    owner_id = _webhook_owner_id(db)
    if owner_id is None:
        return {"webhooks": [], "count": 0}
    rows = webhook_service.list_webhooks(db, owner_id)
    return {"webhooks": rows, "count": len(rows)}


@router.post("/webhooks", summary="Создать веб-хук")
def api_webhooks_create(
    body: WebhookCreateRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Создаёт новый веб-хук. Secret генерится автоматически и возвращается
    в ответе — сохрани его, бот будет с ним проверять HMAC-подпись.
    Если в `events` есть `*` — подписка на все события."""
    owner_id = _require_webhook_owner_id(db)
    # Парсинг events
    events_raw = body.events
    if "*" in events_raw:
        events_set = set(webhook_service.EVENTS_ALL)
    else:
        bogus = [e for e in events_raw if e not in webhook_service.EVENTS_ALL]
        if bogus:
            raise ApiAuthError(400, "bad_events",
                               tr(
                                   "api_v1.webhook_unknown_events",
                                   events=bogus,
                               ))
        events_set = set(events_raw)
    try:
        hook_id, secret = webhook_service.create_webhook(
            db, owner_user_id=owner_id,
            name=body.name, url=body.url, events=events_set,
        )
    except webhook_service.WebhookError as e:
        raise ApiAuthError(400, "bad_request", str(e))
    audit(db, "webhook.create", None,
          summary=f"api_key={key.name}: + webhook {body.name!r} → {body.url}")
    db.commit()
    row = db.execute("SELECT * FROM webhooks WHERE id = ?", (hook_id,)).fetchone()
    return _webhook_dict(dict(row))


@router.patch("/webhooks/{hook_id}", summary="Обновить веб-хук")
def api_webhooks_patch(
    hook_id: int,
    body: WebhookPatchRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    owner_id = _require_webhook_owner_id(db)

    events_set: set[str] | None = None
    if body.events is not None:
        if "*" in body.events:
            events_set = set(webhook_service.EVENTS_ALL)
        else:
            bogus = [e for e in body.events if e not in webhook_service.EVENTS_ALL]
            if bogus:
                raise ApiAuthError(400, "bad_events", tr("api_v1.webhook_unknown_events", events=bogus))
            events_set = set(body.events)
    try:
        ok = webhook_service.update_webhook(
            db, hook_id, owner_id,
            name=body.name, url=body.url, events=events_set, enabled=body.enabled,
        )
    except webhook_service.WebhookError as e:
        raise ApiAuthError(400, "bad_request", str(e))
    if not ok:
        # Может не найдено или нечего менять — отличаем через get_webhook
        if webhook_service.get_webhook(db, hook_id, owner_id) is None:
            raise ApiAuthError(404, "not_found",
                               tr("api_v1.webhook_not_found", id=hook_id))
    audit(db, "webhook.update", None,
          summary=f"api_key={key.name}: webhook id={hook_id}")
    db.commit()
    row = db.execute("SELECT * FROM webhooks WHERE id = ?", (hook_id,)).fetchone()
    return _webhook_dict(dict(row))


@router.delete("/webhooks/{hook_id}", summary="Удалить веб-хук")
def api_webhooks_delete(
    hook_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    owner_id = _require_webhook_owner_id(db)
    if not webhook_service.delete_webhook(db, hook_id, owner_id):
        raise ApiAuthError(404, "not_found",
                           tr("api_v1.webhook_not_found", id=hook_id))
    audit(db, "webhook.delete", None,
          summary=f"api_key={key.name}: webhook id={hook_id}")
    db.commit()
    return {"deleted": True, "id": hook_id}


@router.post("/webhooks/{hook_id}/resume", summary="Возобновить broken webhook")
def api_webhooks_resume(
    hook_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Снять broken_at — после 3 failure'ов рассылка прекращается, юзер вручную
    возобновляет (после фикса URL/IP бота)."""
    owner_id = _require_webhook_owner_id(db)
    if not webhook_service.resume_webhook(db, hook_id, owner_id):
        raise ApiAuthError(404, "not_found", tr("api_v1.webhook_not_found", id=hook_id))
    audit(db, "webhook.resume", None,
          summary=f"api_key={key.name}: webhook id={hook_id}")
    db.commit()
    return {"resumed": True, "id": hook_id}


@router.post("/webhooks/{hook_id}/test", summary="Послать тестовый ping")
async def api_webhooks_test(
    hook_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Отправляет webhook.ping на этот хук синхронно. Возвращает
    HTTP-статус, длительность и ошибку если была. Полезно после смены URL
    чтобы убедиться что бот доступен."""
    owner_id = _require_webhook_owner_id(db)
    hook = webhook_service.get_webhook(db, hook_id, owner_id)
    if not hook:
        raise ApiAuthError(404, "not_found", tr("api_v1.webhook_not_found", id=hook_id))
    result = await webhook_service.test_ping(hook)
    return result


@router.get("/webhooks/{hook_id}/deliveries", summary="Лента доставок")
def api_webhooks_deliveries(
    hook_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> dict:
    """Последние N попыток доставки для этого хука. Каждая запись —
    (delivery_id, event, ts, status, duration_ms, error, attempt)."""
    owner_id = _require_webhook_owner_id(db)
    if not webhook_service.get_webhook(db, hook_id, owner_id):
        raise ApiAuthError(404, "not_found", tr("api_v1.webhook_not_found", id=hook_id))
    deliveries = webhook_service.list_recent_deliveries(db, hook_id, limit=limit)
    return {"webhook_id": hook_id, "deliveries": deliveries,
            "count": len(deliveries)}
