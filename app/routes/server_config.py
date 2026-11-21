"""Save Factorio server configuration and runtime preferences."""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Form, Request

from ..db import audit, get_db
from ..i18n import tr
from ..panel_config import has_factorio_credentials
from ..server_data import NAME_RE, spec_from_row, unique_conflict_message
from .auth_routes import stepup_check_or_redirect
from .server_common import require_server, server_tab_redirect
from .server_players import whitelist_paths


router = APIRouter()


VISIBILITY_CHOICES = ("public", "lan", "none")
ALLOW_CMDS_CHOICES = ("admins-only", "true", "false")


def _form_bool(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "on", "yes")


@router.post("/{uuid}/config/update")
def config_update(uuid: str,
                  request: Request,
                  name: str = Form(...),
                  version: str = Form(...),
                  port: int = Form(...),
                  description: str = Form(""),
                  password: str = Form(""),
                  max_players: int = Form(0),
                  visibility: str = Form("lan"),
                  allow_commands: str = Form("admins-only"),
                  autosave_interval: int = Form(10),
                  autosave_slots: int = Form(5),
                  afk_autokick_interval: int = Form(0),
                  auto_pause: str = Form(""),
                  only_admins_pause: str = Form(""),
                  non_blocking_saving: str = Form(""),
                  enable_whitelist: str = Form(""),
                  autostart: str = Form(""),
                  network_mode: str = Form("bridge"),
                  db: sqlite3.Connection = Depends(get_db)):
    if (r := stepup_check_or_redirect(request)) is not None: return r
    row = require_server(db, uuid)
    spec = spec_from_row(row)

    name = name.strip()
    if not NAME_RE.match(name):
        return server_tab_redirect(uuid, "config", err=tr("servers.name_format"))
    if visibility not in VISIBILITY_CHOICES:
        return server_tab_redirect(uuid, "config", err=tr("servers.visibility_invalid"))
    if allow_commands not in ALLOW_CMDS_CHOICES:
        return server_tab_redirect(uuid, "config", err=tr("servers.allow_commands_invalid"))
    if not (1024 <= port <= 65535):
        return server_tab_redirect(uuid, "config", err=tr("servers.port_out_of_range"))
    if visibility == "public" and not has_factorio_credentials():
        return server_tab_redirect(uuid, "config", err=tr("servers.public_visibility_needs_credentials"))
    if network_mode not in ("bridge", "host"):
        return server_tab_redirect(uuid, "config", err=tr("servers.network_mode_invalid"))

    # Уникальность name/port — кроме самого этого сервера.
    other_name = db.execute(
        "SELECT 1 FROM servers WHERE name = ? AND uuid != ?", (name, uuid),
    ).fetchone()
    if other_name:
        return server_tab_redirect(uuid, "config", err=tr("servers.name_taken_by_another_server", name=name))
    other_port = db.execute(
        "SELECT 1 FROM servers WHERE port = ? AND uuid != ?", (port, uuid),
    ).fetchone()
    if other_port:
        return server_tab_redirect(uuid, "config", err=tr("servers.port_taken_by_another_server", port=port))

    # Что изменилось — определяем класс «дальнейших действий», который сообщим юзеру.
    cur_nm = "bridge"
    try:
        cur_nm = row["network_mode"] or "bridge"
    except (KeyError, IndexError):
        pass
    needs_recreate = ((port != row["port"]) or (version != row["version"])
                      or (network_mode != cur_nm))
    needs_restart = not needs_recreate  # любая правка settings.json требует хотя бы рестарта

    # Тогл whitelist: переименовываем server-whitelist.json ↔ .disabled,
    # чтобы сохранить содержимое. Применяется при следующем старте контейнера.
    want_wl = _form_bool(enable_whitelist)
    wl_active, wl_disabled = whitelist_paths(spec.data_dir / "config")
    is_wl_on = wl_active.exists()
    if want_wl and not is_wl_on:
        if wl_disabled.exists():
            wl_disabled.rename(wl_active)
        else:
            wl_active.parent.mkdir(parents=True, exist_ok=True)
            wl_active.write_text("[]", encoding="utf-8")
        needs_restart = True
    elif (not want_wl) and is_wl_on:
        if wl_disabled.exists():
            wl_disabled.unlink()  # перезаписываем — оставляем самое свежее
        wl_active.rename(wl_disabled)
        needs_restart = True

    # 1) server-settings.json: подгружаем текущий, мерджим, пишем.
    settings_path = spec.data_dir / "config" / "server-settings.json"
    try:
        cur = json.loads(settings_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cur = {}
    # username/token синхронизируем из .env при каждом сохранении — иначе
    # старый сейв с пустым token'ом не зарегистрируется в master-сервере, даже
    # если юзер уже ввёл креды в Settings.
    import os as _os
    cur.update({
        "name": name,
        "description": description,
        "max_players": max_players,
        "visibility": {
            "public": visibility == "public",
            "lan": visibility in ("public", "lan"),
        },
        "username": _os.environ.get("FACTORIO_USERNAME", "").strip(),
        "token": _os.environ.get("FACTORIO_TOKEN", "").strip(),
        "game_password": password,
        "require_user_verification": visibility == "public",
        "allow_commands": allow_commands,
        "autosave_interval": autosave_interval,
        "autosave_slots": autosave_slots,
        "afk_autokick_interval": afk_autokick_interval,
        "auto_pause": _form_bool(auto_pause),
        "only_admins_can_pause_the_game": _form_bool(only_admins_pause),
        "non_blocking_saving": _form_bool(non_blocking_saving),
    })
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        settings_path.chmod(0o600)
    except OSError:
        pass

    # 2) meta.json — синхронизируем то что туда писалось при создании.
    meta_path = spec.data_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        meta = {"uuid": uuid}
    meta.update({"name": name, "version": version, "port": port, "visibility": visibility})
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # 3) DB row.
    try:
        db.execute(
            "UPDATE servers SET name = ?, version = ?, port = ?, autostart = ?, "
            "network_mode = ? WHERE uuid = ?",
            (name, version, port, 1 if _form_bool(autostart) else 0, network_mode, uuid),
        )
    except sqlite3.IntegrityError as e:
        return server_tab_redirect(uuid, "config", err=unique_conflict_message(e))
    changes = []
    if name != row["name"]: changes.append(f"name «{row['name']}»→«{name}»")
    if version != row["version"]: changes.append(f"v{row['version']}→v{version}")
    if port != row["port"]: changes.append(f":{row['port']}→:{port}")
    audit(db, "server.config", uuid,
          summary=("config: " + ", ".join(changes)) if changes else "config: settings")
    db.commit()

    if needs_recreate:
        return server_tab_redirect(uuid, "config", msg=tr("servers.config_saved_recreate"))
    return server_tab_redirect(uuid, "config", msg=tr("servers.config_saved_restart"))
