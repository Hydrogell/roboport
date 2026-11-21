"""Web actions for Factorio server schedules."""
from __future__ import annotations

import sqlite3
from typing import List

from fastapi import APIRouter, Depends, Form

from ..db import audit, get_db
from ..i18n import tr
from ..schedules import (
    SCHEDULE_KINDS, VALID_DOWS, create_task, delete_task, parse_schedule,
    update_task,
)
from .server_common import require_server, server_tab_redirect


router = APIRouter()


@router.post("/{uuid}/schedule/add")
def schedule_add(uuid: str,
                 kind: str = Form(...),
                 schedule_type: str = Form(...),  # 'every' | 'daily' | 'weekly'
                 every_n: int = Form(0),
                 every_unit: str = Form("m"),     # 'm' | 'h'
                 daily_time: str = Form(""),      # 'HH:MM'
                 weekly_time: str = Form(""),     # 'HH:MM'
                 weekly_dows: List[str] = Form(default=[]),  # ['mon','wed',...]
                 command: str = Form(""),
                 warn_seconds: int = Form(0),
                 db: sqlite3.Connection = Depends(get_db)):
    """Создать новую задачу. warn_seconds работает только для kind='restart'."""
    require_server(db, uuid)
    if kind not in SCHEDULE_KINDS:
        return server_tab_redirect(uuid, "schedule", err=tr("servers.schedule_kind_invalid", kinds=SCHEDULE_KINDS))
    if schedule_type == "every":
        if every_unit not in ("m", "h") or every_n <= 0:
            return server_tab_redirect(uuid, "schedule", err=tr("servers.schedule_interval_invalid"))
        schedule = f"every:{every_n}{every_unit}"
    elif schedule_type == "daily":
        schedule = f"daily:{(daily_time or '').strip()}"
    elif schedule_type == "weekly":
        dows = [d for d in weekly_dows if d in VALID_DOWS]
        if not dows:
            return server_tab_redirect(uuid, "schedule", err=tr("servers.schedule_no_weekdays"))
        # Канонизируем порядок (mon первым) — для красивого хранения
        dows_sorted = [d for d in VALID_DOWS if d in dows]
        schedule = f"weekly:{','.join(dows_sorted)}:{(weekly_time or '').strip()}"
    else:
        return server_tab_redirect(uuid, "schedule", err=tr("servers.schedule_type_invalid"))

    if parse_schedule(schedule) is None:
        return server_tab_redirect(
            uuid, "schedule", err=tr(
                "servers.schedule_invalid",
                schedule=schedule,
            ),
        )

    cmd_value: str | None = None
    if kind == "command":
        cmd_value = (command or "").strip()
        if not cmd_value:
            return server_tab_redirect(uuid, "schedule", err=tr("servers.schedule_command_required"))
        if len(cmd_value) > 500:
            return server_tab_redirect(uuid, "schedule", err=tr("servers.schedule_command_too_long"))

    warn = 0
    if kind == "restart":
        try:
            warn = max(0, min(600, int(warn_seconds)))  # cap 10 минут — разумно
        except (TypeError, ValueError):
            warn = 0

    create_task(db, uuid, kind, schedule, cmd_value, warn)
    audit(db, "schedule.add", uuid,
          summary=f"+ {kind} {schedule}" + (f" cmd={cmd_value!r}" if cmd_value else ""))
    db.commit()
    desc = f"{kind} {cmd_value!r}" if kind == "command" else kind
    if warn:
        desc += f" (warn {warn}s)"
    return server_tab_redirect(uuid, "schedule", msg=tr("servers.schedule_added", description=desc, schedule=schedule))


@router.post("/{uuid}/schedule/toggle")
def schedule_toggle(uuid: str, task_id: int = Form(...),
                    enabled: str = Form("0"),
                    db: sqlite3.Connection = Depends(get_db)):
    require_server(db, uuid)
    new_val = 1 if enabled == "1" else 0
    update_task(db, task_id, uuid, {"enabled": new_val})
    db.commit()
    return server_tab_redirect(uuid, "schedule")


@router.post("/{uuid}/schedule/delete")
def schedule_delete(uuid: str, task_id: int = Form(...),
                    db: sqlite3.Connection = Depends(get_db)):
    require_server(db, uuid)
    delete_task(db, task_id, uuid)
    audit(db, "schedule.delete", uuid, summary=f"- task #{task_id}")
    db.commit()
    return server_tab_redirect(uuid, "schedule", msg=tr("servers.schedule_deleted"))
