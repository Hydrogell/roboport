"""Shared schedule parsing, timing, and SQLite task queries.

Routes own their presentation-specific validation and audit messages.  The
sampler owns Docker/RCON execution; this module is deliberately free of those
runtime dependencies so schedule management does not import the sampler.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Mapping

from .panel_config import panel_tz


SCHEDULE_KINDS = ("command", "restart")
VALID_DOWS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DOW_MAP = {day: index for index, day in enumerate(VALID_DOWS)}

_SCHEDULE_EVERY_RE = re.compile(r"^every:(\d+)([mh])$")
_SCHEDULE_DAILY_RE = re.compile(r"^daily:(\d{1,2}):(\d{2})$")
_SCHEDULE_WEEKLY_RE = re.compile(r"^weekly:([a-z,]+):(\d{1,2}):(\d{2})$")


def parse_schedule(schedule: str) -> dict | None:
    """Parse a supported stored schedule, or return ``None`` if invalid."""
    match = _SCHEDULE_EVERY_RE.match(schedule)
    if match:
        count = int(match.group(1))
        unit = match.group(2)
        if count <= 0:
            return None
        interval_sec = count * (60 if unit == "m" else 3600)
        if interval_sec < 60:
            return None
        return {"type": "every", "interval_sec": interval_sec, "raw": schedule}

    match = _SCHEDULE_DAILY_RE.match(schedule)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return {"type": "daily", "hour": hour, "minute": minute, "raw": schedule}

    match = _SCHEDULE_WEEKLY_RE.match(schedule)
    if match:
        days = set()
        for day in match.group(1).split(","):
            if day not in DOW_MAP:
                return None
            days.add(DOW_MAP[day])
        if not days:
            return None
        hour, minute = int(match.group(2)), int(match.group(3))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return {
            "type": "weekly", "dows": days, "hour": hour, "minute": minute,
            "raw": schedule,
        }
    return None


def should_fire(parsed: dict, last_run: int | None, now: int) -> bool:
    """Return whether a parsed task is due at ``now`` in the panel timezone."""
    if parsed["type"] == "every":
        return last_run is None or now - last_run >= parsed["interval_sec"]
    if parsed["type"] not in ("daily", "weekly"):
        return False

    tz = panel_tz()
    now_local = datetime.fromtimestamp(now, tz=tz)
    if parsed["type"] == "weekly" and now_local.weekday() not in parsed["dows"]:
        return False
    target_today = now_local.replace(
        hour=parsed["hour"], minute=parsed["minute"], second=0, microsecond=0,
    )
    if now_local < target_today:
        return False
    return last_run is None or datetime.fromtimestamp(last_run, tz=tz) < target_today


def list_tasks(conn: sqlite3.Connection, server_uuid: str):
    """Return every persisted task for one server, in UI/API display order."""
    return conn.execute(
        "SELECT * FROM scheduled_tasks WHERE server_uuid = ? ORDER BY id",
        (server_uuid,),
    ).fetchall()


def get_task(conn: sqlite3.Connection, task_id: int, server_uuid: str):
    """Return a task only if it belongs to ``server_uuid``."""
    return conn.execute(
        "SELECT * FROM scheduled_tasks WHERE id = ? AND server_uuid = ?",
        (task_id, server_uuid),
    ).fetchone()


def create_task(conn: sqlite3.Connection, server_uuid: str, kind: str,
                schedule: str, command: str | None, warn_seconds: int) -> int:
    cur = conn.execute(
        "INSERT INTO scheduled_tasks (server_uuid, kind, schedule, command, warn_seconds) "
        "VALUES (?, ?, ?, ?, ?)",
        (server_uuid, kind, schedule, command, warn_seconds),
    )
    return int(cur.lastrowid)


_UPDATABLE_TASK_FIELDS = frozenset({"enabled", "schedule", "command", "warn_seconds"})


def update_task(conn: sqlite3.Connection, task_id: int, server_uuid: str,
                changes: Mapping[str, object]) -> int:
    """Apply named, supported field changes to a task owned by ``server_uuid``."""
    unknown = changes.keys() - _UPDATABLE_TASK_FIELDS
    if unknown:
        raise ValueError(f"unsupported scheduled task fields: {sorted(unknown)!r}")
    if not changes:
        return 0
    assignments = [f"{field} = ?" for field in changes]
    cur = conn.execute(
        f"UPDATE scheduled_tasks SET {', '.join(assignments)} "
        "WHERE id = ? AND server_uuid = ?",
        [*changes.values(), task_id, server_uuid],
    )
    return cur.rowcount


def delete_task(conn: sqlite3.Connection, task_id: int, server_uuid: str) -> int:
    cur = conn.execute(
        "DELETE FROM scheduled_tasks WHERE id = ? AND server_uuid = ?",
        (task_id, server_uuid),
    )
    return cur.rowcount


def due_tasks(conn: sqlite3.Connection):
    """Return enabled tasks with the server fields required by the runner."""
    return conn.execute(
        "SELECT t.id, t.kind, t.schedule, t.command, t.last_run, t.warn_seconds, "
        "       s.uuid, s.name, s.version, s.port, s.network_mode "
        "FROM scheduled_tasks t "
        "JOIN servers s ON s.uuid = t.server_uuid "
        "WHERE t.enabled = 1 ORDER BY t.id"
    ).fetchall()


def record_task_run(conn: sqlite3.Connection, task_id: int, now: int,
                    status: str) -> None:
    conn.execute(
        "UPDATE scheduled_tasks SET last_run = ?, last_status = ? WHERE id = ?",
        (now, status, task_id),
    )
