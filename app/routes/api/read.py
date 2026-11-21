"""Public liveness check and read-only server and audit routes."""
from __future__ import annotations

import sqlite3
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ... import auth as auth_lib
from ...db import get_db
from ...i18n import tr
from ...runtime import get_runtime
from ...runtime.docker import DockerError
from ...server_data import spec_from_row
from .auth import ApiAuthError, require_api_key
from .common import load_server

router = APIRouter()


@router.get("/health", summary="Health check (публичный)", openapi_extra={"security": []})
def api_health() -> dict:
    """Liveness check для мониторинга. Не требует ключа.
    Эндпоинт намеренно ничего не делает с БД, чтобы не падать при её залоке."""
    return {"ok": True, "ts": int(time.time()), "version": "v1"}


def _server_dict(row, runtime_status: str | None = None) -> dict:
    """Стандартное представление сервера в API-ответах."""
    return {
        "uuid": row["uuid"],
        "name": row["name"],
        "version": row["version"],
        "port": row["port"],
        "runtime": row["runtime"],
        "network_mode": row["network_mode"] if "network_mode" in row.keys() else "bridge",
        "autostart": bool(row["autostart"]) if "autostart" in row.keys() else False,
        "status": runtime_status,
        "created_at": row["created_at"] if "created_at" in row.keys() else None,
    }


def _runtime_status(row) -> str:
    """Безопасно получить runtime-state. На любую ошибку → 'unknown'."""
    try:
        spec = spec_from_row(row)
        rt = get_runtime(row["runtime"])
        return rt.status(spec).state.value
    except Exception:
        return "unknown"


@router.get("/servers", summary="Список серверов")
def api_servers_list(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("read"))],
) -> dict:
    """Список всех Factorio-серверов панели + их runtime-статус."""
    rows = db.execute("SELECT * FROM servers ORDER BY name").fetchall()
    out = []
    for r in rows:
        out.append(_server_dict(r, runtime_status=_runtime_status(r)))
    return {"servers": out, "count": len(out)}


@router.get("/servers/{uuid}", summary="Детали сервера")
def api_server_detail(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("read"))],
) -> dict:
    """Полные детали одного сервера + последний сэмпл метрик."""
    row = load_server(db, uuid)
    server = _server_dict(row, runtime_status=_runtime_status(row))
    # Последний сэмпл метрик (если есть).
    m = db.execute(
        "SELECT ts, cpu_pct, mem_bytes, mem_pct FROM metrics_samples "
        "WHERE server_uuid = ? ORDER BY ts DESC LIMIT 1",
        (uuid,),
    ).fetchone()
    server["last_metrics"] = dict(m) if m else None
    # Подсчёт онлайн-игроков (открытые сессии).
    online_n = db.execute(
        "SELECT COUNT(*) FROM player_sessions "
        "WHERE server_uuid = ? AND left_at IS NULL",
        (uuid,),
    ).fetchone()[0]
    server["players_online"] = int(online_n)
    return server


@router.get("/servers/{uuid}/status", summary="Короткий runtime-статус")
def api_server_status(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("read"))],
) -> dict:
    """Минимальный ответ для healthcheck'ов бота: state + container_id."""
    row = load_server(db, uuid)
    try:
        spec = spec_from_row(row)
        rt = get_runtime(row["runtime"])
        st = rt.status(spec)
        return {
            "uuid": uuid, "name": row["name"],
            "state": st.state.value,
            "container_id": st.container_id,
            "started_at": st.started_at,
        }
    except DockerError as e:
        raise ApiAuthError(502, "runtime_error", tr("api_v1.docker_error", detail=e))


@router.get("/servers/{uuid}/players", summary="Онлайн-игроки")
def api_server_players(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("read"))],
) -> dict:
    """Список ников онлайн (по player_sessions where left_at IS NULL)."""
    load_server(db, uuid)
    rows = db.execute(
        "SELECT name, joined_at FROM player_sessions "
        "WHERE server_uuid = ? AND left_at IS NULL "
        "ORDER BY joined_at",
        (uuid,),
    ).fetchall()
    return {
        "uuid": uuid,
        "online": [{"name": r["name"], "joined_at": r["joined_at"]} for r in rows],
        "count": len(rows),
    }


@router.get("/servers/{uuid}/metrics", summary="История CPU/MEM")
def api_server_metrics(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("read"))],
    period: Annotated[str, Query(pattern="^(1h|6h|24h|7d)$")] = "1h",
) -> dict:
    """Сэмплы метрик за период. Шаг ~30 сек (как пишет sampler)."""
    load_server(db, uuid)
    period_sec = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600,
                  "7d": 7 * 24 * 3600}[period]
    since = int(time.time()) - period_sec
    rows = db.execute(
        "SELECT ts, cpu_pct, mem_bytes, mem_pct FROM metrics_samples "
        "WHERE server_uuid = ? AND ts >= ? ORDER BY ts",
        (uuid, since),
    ).fetchall()
    return {
        "uuid": uuid,
        "period": period,
        "samples": [dict(r) for r in rows],
        "count": len(rows),
    }


@router.get("/servers/{uuid}/audit", summary="События по серверу")
def api_server_audit(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("read"))],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict:
    """Последние N audit-events для конкретного сервера."""
    load_server(db, uuid)
    rows = db.execute(
        "SELECT id, ts, kind, summary FROM audit_log "
        "WHERE server_uuid = ? ORDER BY ts DESC LIMIT ?",
        (uuid, limit),
    ).fetchall()
    return {"uuid": uuid, "events": [dict(r) for r in rows], "count": len(rows)}


@router.get("/audit", summary="Глобальный audit-feed")
def api_audit(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("read"))],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    """Последние N audit-events со всей панели (server.start, auth.login.fail,
    rcon.cmd, и т.п.). Для бот-нотификатора."""
    rows = db.execute(
        "SELECT a.id, a.ts, a.kind, a.summary, a.server_uuid, s.name AS server_name "
        "FROM audit_log a LEFT JOIN servers s ON s.uuid = a.server_uuid "
        "ORDER BY a.ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {"events": [dict(r) for r in rows], "count": len(rows)}


@router.get("/keys/me/requests", summary="Свои последние API-запросы")
def api_keys_me_requests(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("read"))],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    """Последние N запросов сделанных ИМЕННО ЭТИМ ключом. Self-service для
    бота: «что и когда я сам читал/писал». Чужие ключи не показываем —
    приходи в UI/settings под админом для глобального обзора."""
    rows = auth_lib.list_api_requests_for_key(db, key.id, limit=limit)
    return {"key_id": key.id, "key_name": key.name,
            "requests": rows, "count": len(rows)}
