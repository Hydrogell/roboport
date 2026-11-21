"""Create, edit, list, and remove scheduled tasks."""
from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ... import auth as auth_lib
from ...db import audit, get_db
from ...i18n import tr
from ...schedules import create_task, delete_task, get_task, list_tasks, parse_schedule, update_task
from .auth import ApiAuthError, require_api_key, require_key_scope
from .common import load_server

router = APIRouter()


class ScheduleItem(BaseModel):
    id: int
    kind: str = Field(..., description="command | restart")
    schedule: str = Field(..., description="every:Nm | every:Nh | daily:HH:MM | weekly:dow,...:HH:MM")
    command: str | None = None
    enabled: bool
    warn_seconds: int
    last_run: int | None = None
    last_status: str | None = None
    created_at: str | None = None


class ScheduleCreateRequest(BaseModel):
    """`command` обязателен только для kind=command. `warn_seconds` имеет смысл
    только для kind=restart (анонс игрокам)."""
    kind: str = Field(..., pattern="^(command|restart)$")
    schedule: str = Field(..., min_length=3, max_length=120,
                          description="every:30m | every:6h | daily:03:30 | weekly:mon,fri:03:30")
    command: str | None = Field(None, max_length=500)
    warn_seconds: int = Field(0, ge=0, le=600)


class ScheduleUpdateRequest(BaseModel):
    """Patch-update. Все поля опциональны; передавай только меняемое."""
    enabled: bool | None = None
    schedule: str | None = Field(None, min_length=3, max_length=120)
    command: str | None = Field(None, max_length=500)
    warn_seconds: int | None = Field(None, ge=0, le=600)


def _schedule_dict(row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "schedule": row["schedule"],
        "command": row["command"],
        "enabled": bool(row["enabled"]),
        "warn_seconds": row["warn_seconds"] or 0,
        "last_run": row["last_run"],
        "last_status": row["last_status"],
        "created_at": row["created_at"],
    }


@router.get("/servers/{uuid}/schedule", summary="Список расписаний")
def api_schedule_list(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("read"))],
) -> dict:
    load_server(db, uuid)
    rows = list_tasks(db, uuid)
    return {"uuid": uuid, "tasks": [_schedule_dict(r) for r in rows],
            "count": len(rows)}


@router.post("/servers/{uuid}/schedule", summary="Создать расписание")
def api_schedule_create(
    uuid: str,
    body: ScheduleCreateRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("control"))],
) -> dict:
    """Format `schedule`:
    - `every:30m` / `every:6h` — каждые N минут/часов (минимум 1 минута)
    - `daily:03:30` — каждый день в HH:MM (PANEL_TZ)
    - `weekly:mon,fri:03:30` — по дням недели (mon..sun)

    Для kind=command — `command` обязательно. Для kind=restart — `warn_seconds`
    опционален (анонс игрокам через /server-message)."""
    load_server(db, uuid)

    if body.kind == "command":
        require_key_scope(key, "command")

    if parse_schedule(body.schedule) is None:
        raise ApiAuthError(400, "invalid_schedule",
                           tr("api_v1.schedule_invalid", schedule=body.schedule))

    cmd_value: str | None = None
    if body.kind == "command":
        cmd_value = (body.command or "").strip()
        if not cmd_value:
            raise ApiAuthError(400, "missing_command",
                               tr("api_v1.schedule_command_required"))

    warn = body.warn_seconds if body.kind == "restart" else 0

    new_id = create_task(db, uuid, body.kind, body.schedule, cmd_value, warn)
    audit(db, "schedule.add", uuid,
          summary=f"api_key={key.name}: + {body.kind} {body.schedule}"
                  + (f" cmd={cmd_value!r}" if cmd_value else ""))
    row = get_task(db, new_id, uuid)
    db.commit()
    return _schedule_dict(row)


@router.patch("/servers/{uuid}/schedule/{task_id}", summary="Обновить расписание")
def api_schedule_update(
    uuid: str,
    task_id: int,
    body: ScheduleUpdateRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("control"))],
) -> dict:
    """Patch-update: передавай только изменяемые поля. `enabled=false` отключает
    (но не удаляет)."""
    load_server(db, uuid)
    row = get_task(db, task_id, uuid)
    if not row:
        raise ApiAuthError(404, "not_found",
                           tr("api_v1.schedule_not_found", id=task_id))

    # ``kind`` is immutable through this API.  Authorize from the persisted
    # row, rather than only the PATCH payload, so an old command task cannot
    # be retimed or re-enabled with a control-only key.
    if row["kind"] == "command":
        require_key_scope(key, "command")

    changes_by_field: dict[str, object] = {}
    changes: list[str] = []

    if body.enabled is not None:
        changes_by_field["enabled"] = 1 if body.enabled else 0
        changes.append(f"enabled={body.enabled}")
    if body.schedule is not None:
        if parse_schedule(body.schedule) is None:
            raise ApiAuthError(400, "invalid_schedule",
                               tr("api_v1.schedule_invalid", schedule=body.schedule))
        changes_by_field["schedule"] = body.schedule
        changes.append(f"schedule={body.schedule!r}")
    if body.command is not None and row["kind"] == "command":
        command = body.command.strip() or None
        changes_by_field["command"] = command
        changes.append(f"command={command!r}")
    if body.warn_seconds is not None:
        changes_by_field["warn_seconds"] = body.warn_seconds
        changes.append(f"warn={body.warn_seconds}")

    if not changes_by_field:
        return _schedule_dict(row)

    if not update_task(db, task_id, uuid, changes_by_field):
        raise ApiAuthError(404, "not_found", tr("api_v1.schedule_not_found", id=task_id))
    audit(db, "schedule.update", uuid,
          summary=f"api_key={key.name}: id={task_id} {', '.join(changes)}")
    new_row = get_task(db, task_id, uuid)
    db.commit()
    return _schedule_dict(new_row)


@router.delete("/servers/{uuid}/schedule/{task_id}", summary="Удалить расписание")
def api_schedule_delete(
    uuid: str,
    task_id: int,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("control"))],
) -> dict:
    load_server(db, uuid)
    row = get_task(db, task_id, uuid)
    if not row or not delete_task(db, task_id, uuid):
        raise ApiAuthError(404, "not_found",
                           tr("api_v1.schedule_not_found", id=task_id))
    audit(db, "schedule.delete", uuid,
          summary=f"api_key={key.name}: id={task_id} {row['kind']} {row['schedule']}")
    db.commit()
    return {"deleted": True, "id": task_id}
