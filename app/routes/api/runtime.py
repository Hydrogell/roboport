"""Commands and operations on running Factorio instances."""
from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ... import auth as auth_lib
from ...db import audit, get_db
from ...i18n import tr
from ...runtime import get_runtime
from ...runtime.docker import DockerError
from ...runtime.rcon import RconAuthError, RconError
from ...server_data import spec_from_row
from .auth import ApiAuthError, require_api_key
from .common import StateResponse, load_server, state_response

router = APIRouter()


class CommandRequest(BaseModel):
    """RCON-команда для отправки серверу."""
    cmd: str = Field(..., min_length=1, max_length=2000,
                     description="RCON-команда. Полный root-доступ внутри игры.")


class CommandResponse(BaseModel):
    uuid: str
    name: str
    cmd: str
    output: str = Field(..., description="Сырой ответ Factorio. Пустая строка "
                                         "для команд без output.")


@router.post("/servers/{uuid}/command", response_model=CommandResponse,
             summary="Отправить RCON-команду")
def api_command(
    uuid: str,
    body: CommandRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("command"))],
) -> dict:
    """RCON-команда серверу. Сервер должен быть `running`, иначе 409.
    Каждая команда пишется в audit_log с именем API-ключа."""

    row = load_server(db, uuid)
    spec = spec_from_row(row)
    rt = get_runtime(row["runtime"])
    try:
        st = rt.status(spec)
    except DockerError as e:
        raise ApiAuthError(502, "runtime_error", tr("api_v1.docker_error", detail=e))
    if st.state.value != "running":
        raise ApiAuthError(409, "server_not_running",
                           tr("api_v1.rcon_server_offline", state=st.state.value))

    cmd = body.cmd.strip()
    try:
        output = rt.send_command(spec, cmd)
    except RconAuthError as e:
        raise ApiAuthError(502, "rcon_auth", tr("api_v1.rcon_auth_error", detail=e))
    except (RconError, DockerError, ConnectionError, OSError) as e:
        raise ApiAuthError(502, "rcon_error", tr("api_v1.rcon_error", detail=e))

    audit(db, "rcon.cmd", uuid, summary=f"api_key={key.name}: {cmd[:200]}")
    db.commit()
    return {
        "uuid": uuid, "name": row["name"], "cmd": cmd,
        "output": (output or "").rstrip("\n"),
    }


@router.post("/servers/{uuid}/start", response_model=StateResponse,
             summary="Запустить сервер")
def api_start(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("control"))],
) -> dict:
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    rt = get_runtime(row["runtime"])
    try:
        st = rt.start(spec)
    except DockerError as e:
        raise ApiAuthError(502, "runtime_error", tr("api_v1.docker_error", detail=e))
    if st.container_id and st.container_id != row["container_id"]:
        db.execute("UPDATE servers SET container_id = ? WHERE uuid = ?",
                   (st.container_id, uuid))
    audit(db, "server.start", uuid,
          summary=f"api_key={key.name}: start {row['name']}")
    db.commit()
    return state_response(uuid, row, rt)


@router.post("/servers/{uuid}/stop", response_model=StateResponse,
             summary="Остановить сервер")
def api_stop(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("control"))],
) -> dict:
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    rt = get_runtime(row["runtime"])
    try:
        rt.stop(spec)
    except DockerError as e:
        raise ApiAuthError(502, "runtime_error", tr("api_v1.docker_error", detail=e))
    audit(db, "server.stop", uuid,
          summary=f"api_key={key.name}: stop {row['name']}")
    db.commit()
    return state_response(uuid, row, rt)


@router.post("/servers/{uuid}/restart", response_model=StateResponse,
             summary="Перезапустить сервер")
def api_restart(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("control"))],
) -> dict:
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    rt = get_runtime(row["runtime"])
    try:
        rt.restart(spec)
    except DockerError as e:
        raise ApiAuthError(502, "runtime_error", tr("api_v1.docker_error", detail=e))
    audit(db, "server.restart", uuid,
          summary=f"api_key={key.name}: restart {row['name']}")
    db.commit()
    return state_response(uuid, row, rt)


@router.post("/servers/{uuid}/recreate", response_model=StateResponse,
             summary="Пересоздать контейнер (применить config-изменения)")
def api_recreate(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("control"))],
) -> dict:
    """Снести старый контейнер и поднять новый со свежим spec'ом — нужно
    после смены port/version/network_mode через config-update."""
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    rt = get_runtime(row["runtime"])
    try:
        st = rt.recreate(spec)
    except DockerError as e:
        raise ApiAuthError(502, "runtime_error", tr("api_v1.docker_error", detail=e))
    if st.container_id and st.container_id != row["container_id"]:
        db.execute("UPDATE servers SET container_id = ? WHERE uuid = ?",
                   (st.container_id, uuid))
    audit(db, "server.recreate", uuid,
          summary=f"api_key={key.name}: recreate {row['name']}")
    db.commit()
    return state_response(uuid, row, rt)


# -----------------------------------------------------------------------------
# saves/save-now (scope: control)
# -----------------------------------------------------------------------------

@router.post("/servers/{uuid}/saves/save-now", summary="RCON /server-save")
def api_save_now(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("control"))],
) -> dict:
    """Атомарный snapshot текущего мира в saves/default.zip. Сервер должен
    быть running — иначе 409."""
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    rt = get_runtime(row["runtime"])
    try:
        st = rt.status(spec)
    except DockerError as e:
        raise ApiAuthError(502, "runtime_error", tr("api_v1.docker_error", detail=e))
    if st.state.value != "running":
        raise ApiAuthError(409, "server_not_running",
                           tr("api_v1.save_server_offline", state=st.state.value))
    try:
        resp = rt.send_command(spec, "/server-save")
    except RconAuthError as e:
        raise ApiAuthError(502, "rcon_auth", tr("api_v1.rcon_auth_error", detail=e))
    except (RconError, DockerError, ConnectionError, OSError) as e:
        raise ApiAuthError(502, "rcon_error", tr("api_v1.rcon_error", detail=e))
    audit(db, "save.now", uuid, summary=f"api_key={key.name}: /server-save")
    db.commit()
    return {"uuid": uuid, "name": row["name"],
            "output": (resp or "").rstrip("\n") or "saved"}
