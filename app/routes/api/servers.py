"""Create, configure, clone, or delete a server."""
from __future__ import annotations

import json
import shutil
import sqlite3
import uuid as uuid_lib
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ... import auth as auth_lib, server_data
from ...db import audit, get_db
from ...i18n import tr
from ...runtime import get_runtime
from ...runtime.docker import DockerError
from ...server_data import (
    default_server_settings, delete_server_records, find_server, remove_directory_via_docker, spec_from_row, unique_conflict_message,
)
from .auth import ApiAuthError, require_api_key
from .common import StateResponse, load_server, state_response

router = APIRouter()


class ServerCreateRequest(BaseModel):
    """Минимальное создание сервера через API. Расширенные опции (initial_save,
    map-gen-settings, peaceful, seed, public-visibility) — через UI / Phase 3.1
    если понадобится. По умолчанию: lan visibility, нет пароля, autosave 10/5,
    autostart выключен."""
    name: str = Field(..., min_length=1, max_length=40,
                      pattern=r"^[A-Za-z0-9_\- ]{1,40}$")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    port: int = Field(..., ge=1024, le=65535)
    description: str = Field("", max_length=200)
    password: str = Field("", max_length=64)
    max_players: int = Field(0, ge=0, le=1000)
    visibility: str = Field("lan", pattern=r"^(lan|public|hidden)$")
    autostart: bool = False
    network_mode: str = Field("bridge", pattern=r"^(bridge|host)$")


class ServerPatchRequest(BaseModel):
    """Patch-update конфига сервера. Все поля опциональны."""
    name: str | None = Field(None, min_length=1, max_length=40,
                              pattern=r"^[A-Za-z0-9_\- ]{1,40}$")
    version: str | None = Field(None, pattern=r"^\d+\.\d+\.\d+$")
    port: int | None = Field(None, ge=1024, le=65535)
    description: str | None = Field(None, max_length=200)
    password: str | None = Field(None, max_length=64)
    max_players: int | None = Field(None, ge=0, le=1000)
    visibility: str | None = Field(None, pattern=r"^(lan|public|hidden)$")
    autostart: bool | None = None
    network_mode: str | None = Field(None, pattern=r"^(bridge|host)$")


class ServerCloneRequest(BaseModel):
    new_name: str = Field(..., min_length=1, max_length=40,
                          pattern=r"^[A-Za-z0-9_\- ]{1,40}$")
    new_port: int = Field(..., ge=1024, le=65535)


@router.post("/servers", response_model=StateResponse,
             summary="Создать сервер (scope=admin)")
def api_server_create(
    body: ServerCreateRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Минимальное создание: имя/версия/порт + опции через JSON. UNIQUE-индексы
    на name/port в БД ловят гонки → 409 conflict с человеческим сообщением.
    Контейнер сразу не запускается — стартануть отдельным `POST /start`."""

    new_uuid = str(uuid_lib.uuid4())
    data_dir = server_data.SERVERS_DIR / new_uuid
    cfg_dir = data_dir / "config"
    try:
        cfg_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ApiAuthError(500, "internal_error",
                           tr("api_v1.server_directory_exists", path=data_dir))
    (data_dir / "mods").mkdir(exist_ok=True)
    (data_dir / "saves").mkdir(exist_ok=True)

    # server-settings.json
    settings = default_server_settings(
        name=body.name, description=body.description, password=body.password,
        max_players=body.max_players, visibility=body.visibility, verify_users=True,
    )
    (cfg_dir / "server-settings.json").write_text(
        json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    # meta.json
    meta = {"uuid": new_uuid, "name": body.name, "version": body.version,
            "port": body.port, "runtime": "docker"}
    (data_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8",
    )

    try:
        db.execute(
            "INSERT INTO servers (uuid, name, version, port, runtime, autostart, "
            "network_mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_uuid, body.name, body.version, body.port, "docker",
             1 if body.autostart else 0, body.network_mode),
        )
    except sqlite3.IntegrityError as e:
        # Откатим папку — иначе при следующей попытке `data dir already exists`
        shutil.rmtree(data_dir, ignore_errors=True)
        raise ApiAuthError(409, "conflict", unique_conflict_message(e))

    audit(db, "server.create", new_uuid,
          summary=f"api_key={key.name}: create {body.name!r} v{body.version} :{body.port}")
    db.commit()
    row = find_server(db, new_uuid)
    return state_response(new_uuid, row, get_runtime("docker"))


@router.patch("/servers/{uuid}", summary="Обновить config сервера (scope=admin)")
def api_server_patch(
    uuid: str,
    body: ServerPatchRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Patch-update основных полей. После смены port/version/network_mode
    нужно дёрнуть `POST /recreate` чтобы изменения применились к контейнеру.
    server-settings.json мёрджится с текущим (не перезаписывается полностью)."""

    row = load_server(db, uuid)

    # 1) Соберём изменения для DB
    db_fields: list[str] = []
    db_values: list = []
    changes: list[str] = []

    if body.name is not None and body.name != row["name"]:
        db_fields.append("name = ?")
        db_values.append(body.name)
        changes.append(f"name {row['name']!r}->{body.name!r}")
    if body.version is not None and body.version != row["version"]:
        db_fields.append("version = ?")
        db_values.append(body.version)
        changes.append(f"v{row['version']}->v{body.version}")
    if body.port is not None and body.port != row["port"]:
        db_fields.append("port = ?")
        db_values.append(body.port)
        changes.append(f":{row['port']}->:{body.port}")
    if body.autostart is not None:
        db_fields.append("autostart = ?")
        db_values.append(1 if body.autostart else 0)
        changes.append(f"autostart={body.autostart}")
    if body.network_mode is not None and body.network_mode != (row["network_mode"] or "bridge"):
        db_fields.append("network_mode = ?")
        db_values.append(body.network_mode)
        changes.append(f"network_mode={body.network_mode}")

    if db_fields:
        db_values.append(uuid)
        try:
            db.execute(
                f"UPDATE servers SET {', '.join(db_fields)} WHERE uuid = ?",
                db_values,
            )
        except sqlite3.IntegrityError as e:
            raise ApiAuthError(409, "conflict", unique_conflict_message(e))

    # 2) server-settings.json — мёрдж только переданных полей
    spec = spec_from_row(row)
    settings_path = spec.data_dir / "config" / "server-settings.json"
    try:
        cur = json.loads(settings_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cur = {}
    sett_changes: list[str] = []
    if body.name is not None:
        cur["name"] = body.name
    if body.description is not None:
        cur["description"] = body.description
        sett_changes.append("description")
    if body.password is not None:
        cur["game_password"] = body.password
        sett_changes.append("password")
    if body.max_players is not None:
        cur["max_players"] = body.max_players
        sett_changes.append(f"max_players={body.max_players}")
    if body.visibility is not None:
        cur["visibility"] = {
            "public": body.visibility == "public",
            "lan": body.visibility in ("public", "lan"),
        }
        sett_changes.append(f"visibility={body.visibility}")
    if sett_changes or body.name is not None:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        changes.extend(sett_changes)

    if not changes:
        # Нечего менять — отдадим текущий state
        return state_response(uuid, row, get_runtime(row["runtime"]))

    audit(db, "server.config", uuid,
          summary=f"api_key={key.name}: " + ", ".join(changes))
    db.commit()
    row = find_server(db, uuid)
    return state_response(uuid, row, get_runtime(row["runtime"]))


@router.delete("/servers/{uuid}", summary="Удалить сервер целиком (scope=admin)")
def api_server_delete(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
    confirm: Annotated[str, Query(description="Должен совпадать с именем сервера")] = "",
) -> dict:
    """Полное удаление: docker rm -f + rm -rf servers/<uuid>/ + DELETE из БД
    (servers/metrics/sessions/scheduled_tasks). НЕОБРАТИМО — нет undo.

    Безопасник: `?confirm=<exact-name>` обязателен. Если не совпало — 412
    `confirmation_required`. Это аналог typed-confirm в UI."""

    row = load_server(db, uuid)
    if confirm != row["name"]:
        raise ApiAuthError(412, "confirmation_required",
                           tr("api_v1.server_delete_confirm", name=repr(row['name'])))

    spec = spec_from_row(row)
    rt = get_runtime(row["runtime"])
    # 1) docker rm -f (idempotent — если контейнера нет, пропустим)
    try:
        rt.remove(spec)
    except DockerError:
        pass

    # 2) Снос data_dir. Если что-то держится на open file (rconpw в контейнере),
    # пробуем _force_remove_via_docker как в UI-роуте.
    data_dir = spec.data_dir
    if data_dir.is_dir():
        try:
            shutil.rmtree(data_dir)
        except OSError:
            err = remove_directory_via_docker(data_dir)
            if err:
                raise ApiAuthError(500, "internal_error",
                                   tr("api_v1.server_delete_failed", detail=err))

    # 3) DB cleanup
    delete_server_records(db, uuid)
    audit(db, "server.delete", uuid,
          summary=f"api_key={key.name}: deleted {row['name']!r}")
    db.commit()
    return {"deleted": True, "uuid": uuid, "name": row["name"]}


@router.post("/servers/{uuid}/clone", response_model=StateResponse,
             summary="Клонировать сервер (scope=admin)")
def api_server_clone(
    uuid: str,
    body: ServerCloneRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Копирует servers/<uuid>/ целиком в новый uuid (без rconpw — Factorio
    регенерит при первом старте). Источник должен быть остановлен — иначе
    sicюра-копия может оказаться битой."""

    row = load_server(db, uuid)
    if body.new_name == row["name"]:
        raise ApiAuthError(400, "bad_request",
                           tr("api_v1.clone_same_name"))

    spec = spec_from_row(row)
    rt = get_runtime(row["runtime"])
    try:
        st = rt.status(spec)
    except DockerError as e:
        raise ApiAuthError(502, "runtime_error", tr("api_v1.docker_error", detail=e))
    if st.state.value == "running":
        raise ApiAuthError(409, "server_running",
                           tr("api_v1.clone_stop_source"))

    new_id = str(uuid_lib.uuid4())
    dst_dir = server_data.SERVERS_DIR / new_id
    try:
        src_dir = server_data.direct_server_dir(server_data.SERVERS_DIR, uuid)
        server_data.copy_server_tree(src_dir, dst_dir)
    except ValueError:
        raise ApiAuthError(400, "bad_request", tr("servers.forbidden_path", path=uuid))
    except OSError:
        raise ApiAuthError(500, "internal_error", tr("api_v1.clone_source_missing", path=uuid))

    # Подправить meta + server-settings.json
    meta_path = dst_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        meta = {}
    meta.update({"uuid": new_id, "name": body.new_name,
                 "version": row["version"], "port": body.new_port})
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    settings_path = dst_dir / "config" / "server-settings.json"
    try:
        ss = json.loads(settings_path.read_text(encoding="utf-8"))
        ss["name"] = body.new_name
        settings_path.write_text(json.dumps(ss, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    try:
        db.execute(
            "INSERT INTO servers (uuid, name, version, port, runtime) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_id, body.new_name, row["version"], body.new_port, row["runtime"]),
        )
    except sqlite3.IntegrityError as e:
        shutil.rmtree(dst_dir, ignore_errors=True)
        raise ApiAuthError(409, "conflict", unique_conflict_message(e))

    audit(db, "server.clone", new_id,
          summary=f"api_key={key.name}: clone {row['name']!r}->{body.new_name!r}")
    db.commit()
    new_row = find_server(db, new_id)
    return state_response(new_id, new_row, rt)
