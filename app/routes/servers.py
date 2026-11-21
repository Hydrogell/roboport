"""Factorio server overview, detail pages and runtime lifecycle."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import urllib.parse
import uuid as uuid_lib
from pathlib import Path

from babel.dates import get_day_names
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from .. import factorio_mods, server_data
from ..db import audit, get_db
from ..factorio_versions import available_versions
from ..i18n import cldr_locale, current_language, tr
from ..runtime import ServerSpec, get_runtime
from ..runtime.docker import DockerError, DockerRuntime
from ..schedules import VALID_DOWS, list_tasks
from ..server_data import NAME_RE, delete_server_records, remove_directory_via_docker, spec_from_row, unique_conflict_message
from ..server_files import breadcrumbs, directory_size, humanize_size, list_dir, safe_resolve
from ..server_saves import LOADED_SAVE_NAME, list_saves
from . import server_config, server_creation, server_files, server_players, server_saves, server_schedule
from .auth_routes import stepup_check_or_redirect
from .server_common import require_server
from .server_players import player_view, read_name_list, whitelist_paths


router = APIRouter(prefix="/servers")
router.include_router(server_creation.router)


_LAN_RE = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.|169\.254\.)")
_OWN_ADDR_RE = re.compile(r"Own address is .*?\{(\d+\.\d+\.\d+\.\d+):(\d+)\}.*?confirmed", re.IGNORECASE)
_CONN_REQ_RE = re.compile(r"\{(\d+\.\d+\.\d+\.\d+):\d+\}")
_REFUSE_RE = re.compile(r"Refusing connection.*?\{(\d+\.\d+\.\d+\.\d+):\d+\}.*?username \(([^)]*)\)\.\s*(\w+)")


def analyze_factorio_logs(logs: str) -> dict:
    """Парсит docker logs Factorio и собирает статус публичной видимости.
    Возвращает dict с полями:
      external_ip — IP подтверждённый pingpong'ами Factorio (None если STUN провалился)
      stun_ok    — bool: STUN-запрос дошёл до pingpong-серверов
      stun_failed — bool: были явные провалы запроса external addr
      external_conns — int: число попыток подключения с не-LAN IP (= порт открыт)
      refusals   — последние ~5 отказов (для диагностики credentials/passwords)
    """
    out = {
        "external_ip": None,
        "stun_ok": False,
        "stun_failed": False,
        "external_conns": 0,
        "refusals": [],
    }
    if not logs:
        return out
    seen_ext_ips: set[str] = set()
    refusals: list[dict] = []
    for line in logs.splitlines():
        m = _OWN_ADDR_RE.search(line)
        if m:
            out["external_ip"] = m.group(1)
            out["stun_ok"] = True
            continue
        if "Failed to query own external address" in line or "STUN" in line and "fail" in line.lower():
            out["stun_failed"] = True
            continue
        if "connectionRequest" in line or "Refusing connection" in line:
            for ip_m in _CONN_REQ_RE.finditer(line):
                ip = ip_m.group(1)
                if not _LAN_RE.match(ip):
                    seen_ext_ips.add(ip)
        rm = _REFUSE_RE.search(line)
        if rm:
            ip, username, reason = rm.group(1), rm.group(2), rm.group(3)
            if not _LAN_RE.match(ip):
                refusals.append({"ip": ip, "username": username, "reason": reason})
    out["external_conns"] = len(seen_ext_ips)
    out["refusals"] = refusals[-5:]  # последние пять
    return out


TABS = {"console", "docker", "files", "mods", "saves", "players", "metrics", "modconfig", "schedule", "config"}
TAIL_CHOICES = {"100", "500", "2000", "all"}


@router.get("/{uuid}")
def server_root(uuid: str):
    # Главная страница сервера = первая вкладка (Терминал).
    return RedirectResponse(url=f"/servers/{uuid}/console", status_code=303)


@router.get("/{uuid}/docker.txt")
def server_docker_logs_download(uuid: str, tail: str = "all",
                                db: sqlite3.Connection = Depends(get_db)):
    from fastapi.responses import PlainTextResponse
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    if tail not in TAIL_CHOICES:
        tail = "all"
    try:
        text = DockerRuntime().logs(spec, tail=tail, timestamps=True)
    except DockerError as e:
        raise HTTPException(502, tr("api_v1.docker_error", detail=e))
    fname = f"roboport-{row['name']}-{tail}.log"
    return PlainTextResponse(
        text or tr("servers.no_container_logs"),
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        media_type="text/plain; charset=utf-8",
    )


# In-memory кэш размеров папок серверов: {uuid: (size_bytes, ts)}.
# 60-сек TTL. На больших серверах (сейвы 100+ МБ) рекурсивный обход —
# заметная нагрузка; кэш + 5с-polling = редкий пересчёт.
_DIR_SIZE_CACHE: dict[str, tuple[int, float]] = {}
_DIR_SIZE_TTL = 60.0


def _dir_size_cached(path: Path, key: str) -> int | None:
    """Сумма размеров файлов в path рекурсивно, с TTL-кэшем по key.
    Возвращает None если папка не существует или прочитать не удалось."""
    import time as _t
    now = _t.time()
    cached = _DIR_SIZE_CACHE.get(key)
    if cached and (now - cached[1]) < _DIR_SIZE_TTL:
        return cached[0]
    if not path.is_dir():
        return None
    try:
        total = directory_size(path)
        _DIR_SIZE_CACHE[key] = (total, now)
        return total
    except OSError:
        return None


@router.get("/{uuid}/players-completion")
def server_players_completion(uuid: str,
                              db: sqlite3.Connection = Depends(get_db)) -> list[str]:
    """Лёгкий список ников игроков для автокомплита в RCON-консоли:
    онлайн-сессии + последние 50 уникальных по истории. Cookie-based,
    в отличие от /api/v1/servers/<uuid>/players (тот требует API-key).
    Возвращает плоский массив строк, отсортированный: сначала онлайн,
    потом история по убыванию last-seen."""
    require_server(db, uuid)
    online_rows = db.execute(
        "SELECT DISTINCT name FROM player_sessions "
        "WHERE server_uuid = ? AND left_at IS NULL "
        "ORDER BY joined_at DESC", (uuid,),
    ).fetchall()
    online = [r["name"] for r in online_rows]
    online_set = set(online)
    hist_rows = db.execute(
        "SELECT name, MAX(COALESCE(left_at, joined_at)) AS last_seen "
        "FROM player_sessions WHERE server_uuid = ? "
        "GROUP BY name ORDER BY last_seen DESC LIMIT 50", (uuid,),
    ).fetchall()
    history = [r["name"] for r in hist_rows if r["name"] not in online_set]
    return online + history


def _read_server_settings(spec: ServerSpec) -> tuple[dict, str | None]:
    try:
        raw = (spec.data_dir / "config" / "server-settings.json").read_text(encoding="utf-8")
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}, None
    except (OSError, ValueError) as error:
        return {}, f"server-settings.json: {error}"


def _server_meta(db: sqlite3.Connection, row, spec: ServerSpec, settings: dict) -> dict:
    try:
        rt_status = get_runtime(row["runtime"]).status(spec)
        rt_error = None
    except DockerError as e:
        rt_status = None
        rt_error = str(e)
    try:
        max_players = int(settings.get("max_players") or 0)
    except (ValueError, TypeError):
        max_players = 0
    players_online_count = int(db.execute(
        "SELECT COUNT(*) FROM player_sessions "
        "WHERE server_uuid = ? AND left_at IS NULL", (spec.uuid,),
    ).fetchone()[0])
    return {
        "server": dict(row), "rt_status": rt_status, "rt_error": rt_error,
        "max_players": max_players, "players_online_count": players_online_count,
        "dir_size_h": humanize_size(_dir_size_cached(spec.data_dir, spec.uuid)),
    }


@router.get("/{uuid}/meta-fragment")
def server_meta_fragment(request: Request, uuid: str,
                         db: sqlite3.Connection = Depends(get_db)):
    """Refresh the live server header independently of the selected tab."""
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    settings, _ = _read_server_settings(spec)
    return request.app.state.templates.TemplateResponse(
        request, "_partials/server_meta.html", _server_meta(db, row, spec, settings),
    )


@router.get("/{uuid}/{tab}")
def server_tab(request: Request, uuid: str, tab: str,
               tail: str = "500",
               db: sqlite3.Connection = Depends(get_db)):
    if tab not in TABS:
        raise HTTPException(404, tr("servers.unknown_tab", tab=tab))
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    settings, settings_error = _read_server_settings(spec)
    meta = _server_meta(db, row, spec, settings)
    rt_status = meta["rt_status"]
    msg = (request.query_params.get("msg") or "").strip() or None
    err = (request.query_params.get("err") or "").strip() or None
    extra = {"page_msg": msg, "page_err": err}
    # Анализ публичной видимости — нужен на ВСЕХ вкладках, чтобы алёрт
    # «порт не открыт / STUN провалился» был виден сразу. Дёргаем только если
    # сервер running (иначе логи устаревшие или пустые) и visibility=public.
    visibility_status = None
    if rt_status and rt_status.state.value == "running":
        visibility = settings.get("visibility")
        if isinstance(visibility, dict) and visibility.get("public"):
            try:
                logs_text = DockerRuntime().logs(spec, tail=500)
                visibility_status = analyze_factorio_logs(logs_text)
                visibility_status["is_public"] = True
            except DockerError:
                pass
    extra["visibility_status"] = visibility_status

    if tab == "docker" and meta["rt_error"] is None:
        if tail not in TAIL_CHOICES:
            tail = "500"
        try:
            extra["docker_logs"] = DockerRuntime().logs(spec, tail=tail, timestamps=True)
            extra["docker_logs_error"] = None
        except DockerError as e:
            extra["docker_logs"] = ""
            extra["docker_logs_error"] = str(e)
        extra["tail"] = tail
        extra["tail_choices"] = ["100", "500", "2000", "all"]
    elif tab == "files":
        rel_path = (request.query_params.get("path") or "").strip()
        target = safe_resolve(spec.data_dir, rel_path)
        if not target.exists():
            raise HTTPException(404, tr("servers.path_not_found", path=rel_path))
        if target.is_file():
            # клик на файл в листинге → редирект на download
            return RedirectResponse(
                url=f"/servers/{uuid}/files/download?path={rel_path}",
                status_code=303,
            )
        extra["files_path"] = rel_path
        extra["files_crumbs"] = breadcrumbs(rel_path)
        extra["files_listing"] = list_dir(target, spec.data_dir)
        extra["files_at_root"] = (target == spec.data_dir.resolve())
    elif tab == "saves":
        extra["saves_listing"] = list_saves(spec.data_dir, factorio_mods.parse_save_mods)
        extra["saves_loaded_name"] = LOADED_SAVE_NAME
        extra["saves_msg"] = msg
        extra["saves_err"] = err
    elif tab == "players":
        extra.update(player_view(spec, rt_status))
        extra["players_msg"] = msg
        extra["players_err"] = err
    elif tab == "modconfig":
        from .. import mod_settings as ms
        from . import modconfig as modcfg_route
        path = spec.data_dir / "mods" / "mod-settings.dat"
        if not path.is_file():
            extra["modcfg_view"] = None
            extra["modcfg_err"] = (tr("servers.mod_settings_not_created"))
        else:
            try:
                parsed = ms.parse(path.read_bytes())
                specs = modcfg_route.build_setting_specs(spec.data_dir / "mods")
                extra["modcfg_view"] = modcfg_route.collect_view(parsed, specs)
                extra["modcfg_err"] = None
            except ms.ModSettingsError as e:
                extra["modcfg_view"] = None
                extra["modcfg_err"] = tr("modconfig.file_parse_failed", detail=e)
    elif tab == "config":
        wl_active_p, wl_disabled_p = whitelist_paths(spec.data_dir / "config")
        extra["cfg_settings"] = settings
        extra["cfg_versions"] = available_versions()
        extra["cfg_whitelist_enabled"] = wl_active_p.exists()
        extra["cfg_whitelist_count"] = len(read_name_list(
            wl_active_p if wl_active_p.exists() else wl_disabled_p
        ))
        extra["cfg_msg"] = msg
        extra["cfg_err"] = err or settings_error
    elif tab == "mods":
        factorio_v = factorio_mods.factorio_major_minor(row["version"])
        installed = factorio_mods.list_installed(spec.data_dir)
        installed = factorio_mods.annotate_with_updates(installed, factorio_v)
        # mod-list.json: enabled-флаги для установленных
        modlist_state = factorio_mods.read_modlist(spec.data_dir)
        for m in installed:
            m["enabled"] = modlist_state.get(m["name"], True)
        cache = factorio_mods.load_cache_quiet()
        q = (request.query_params.get("q") or "").strip()
        results = factorio_mods.search_cache(q, factorio_version=factorio_v, limit=25) if q else []
        installed_names = {m["name"] for m in installed}
        results = [r for r in results if r["name"] not in installed_names]
        import time as _time
        extra["mods_factorio_v"] = factorio_v
        extra["mods_installed"] = installed
        extra["mods_query"] = q
        extra["mods_results"] = results
        extra["mods_cache_synced_at"] = cache["synced_at"]
        extra["mods_cache_count"] = cache["count"]
        extra["mods_now"] = int(_time.time())
    elif tab == "schedule":
        import time as _time
        fields = ("id", "kind", "schedule", "command", "enabled", "warn_seconds", "last_run", "last_status")
        extra["sched_tasks"] = [{field: task[field] for field in fields} for task in list_tasks(db, uuid)]
        day_names = get_day_names("abbreviated", locale=cldr_locale(current_language()))
        extra["sched_weekdays"] = [(code, day_names[i]) for i, code in enumerate(VALID_DOWS)]
        extra["sched_now"] = int(_time.time())
        extra["sched_msg"] = msg
        extra["sched_err"] = err

    return request.app.state.templates.TemplateResponse(
        request, "server_detail.html",
        {"page": "server", "tab": tab, **meta, **extra},
    )


def _udp_port_busy(port: int) -> str | None:
    """Pre-flight: попытка bind на 0.0.0.0:port/udp. Если в EADDRINUSE — порт
    уже занят кем-то на хосте (не нашим контейнером — этим вызвали start, и
    docker сам пропустит running-контейнер). Возвращает имя ошибки или None."""
    import errno
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # SO_REUSEADDR на Linux не позволяет двум процессам биндиться на тот же
        # UDP-порт; на Windows наоборот — бывает что bind() удаётся даже когда
        # порт занят. Проверка не идеальная, но для типичного «забыл стопнуть
        # второй сервер» детектит в 99% случаев.
        s.bind(("0.0.0.0", port))
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)):
            return tr("servers.udp_port_busy", port=port, errno=e.errno)
        return tr("servers.udp_bind_failed", port=port, detail=e)
    finally:
        s.close()
    return None


@router.post("/{uuid}/start")
def server_start(request: Request, uuid: str, db: sqlite3.Connection = Depends(get_db)):
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    runtime = get_runtime(row["runtime"])

    # Если контейнер уже running — кнопка start идемпотентна, не проверяем
    # порт (он наш). В остальных случаях — pre-flight bind: даёт человеческую
    # ошибку до того как docker run упадёт с длинным cli stderr.
    try:
        cur = runtime.status(spec)
    except DockerError as e:
        raise HTTPException(502, tr("api_v1.docker_error", detail=e))
    if cur.state.value != "running":
        busy = _udp_port_busy(int(row["port"]))
        if busy:
            raise HTTPException(409, tr(
                "servers.start_port_busy",
                detail=busy,
            ))

    try:
        st = runtime.start(spec)
    except DockerError as e:
        raise HTTPException(502, tr("api_v1.docker_error", detail=e))
    if st.container_id and st.container_id != row["container_id"]:
        db.execute("UPDATE servers SET container_id = ? WHERE uuid = ?",
                   (st.container_id, uuid))
    audit(db, "server.start", uuid, summary=f"start {row['name']}")
    db.commit()
    return RedirectResponse(url=f"/servers/{uuid}", status_code=303)


@router.post("/{uuid}/stop")
def server_stop(request: Request, uuid: str, db: sqlite3.Connection = Depends(get_db)):
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    try:
        get_runtime(row["runtime"]).stop(spec)
    except DockerError as e:
        raise HTTPException(502, tr("api_v1.docker_error", detail=e))
    audit(db, "server.stop", uuid, summary=f"stop {row['name']}")
    db.commit()
    return RedirectResponse(url=f"/servers/{uuid}", status_code=303)


@router.post("/{uuid}/restart")
def server_restart(request: Request, uuid: str, db: sqlite3.Connection = Depends(get_db)):
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    try:
        get_runtime(row["runtime"]).restart(spec)
    except DockerError as e:
        raise HTTPException(502, tr("api_v1.docker_error", detail=e))
    audit(db, "server.restart", uuid, summary=f"restart {row['name']}")
    db.commit()
    return RedirectResponse(url=f"/servers/{uuid}", status_code=303)


@router.post("/{uuid}/recreate")
def server_recreate(request: Request, uuid: str, db: sqlite3.Connection = Depends(get_db)):
    """Снести контейнер и поднять заново. Нужно после смены порта/версии или
    добавления новых docker-флагов в коде (как было с RCON-проброс)."""
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    try:
        st = get_runtime(row["runtime"]).recreate(spec)
    except DockerError as e:
        raise HTTPException(502, tr("api_v1.docker_error", detail=e))
    if st.container_id and st.container_id != row["container_id"]:
        db.execute("UPDATE servers SET container_id = ? WHERE uuid = ?",
                   (st.container_id, uuid))
    audit(db, "server.recreate", uuid, summary=f"recreate {row['name']}")
    db.commit()
    return RedirectResponse(url=f"/servers/{uuid}", status_code=303)


@router.post("/{uuid}/clone")
def server_clone(uuid: str,
                 new_name: str = Form(...),
                 new_port: int = Form(...),
                 db: sqlite3.Connection = Depends(get_db)):
    """Создать копию сервера: новый uuid, новый name+port, всё остальное (моды,
    сейвы, конфиг) — копируем как есть. Контейнер не запускается; источник должен
    быть остановлен, иначе сейв может скопироваться в неконсистентном состоянии."""
    row = require_server(db, uuid)
    new_name = new_name.strip()
    if not NAME_RE.match(new_name):
        raise HTTPException(400, tr("servers.name_format"))
    if not (1024 <= new_port <= 65535):
        raise HTTPException(400, tr("servers.port_out_of_range"))
    if new_name == row["name"]:
        raise HTTPException(400, tr("servers.clone_same_name"))
    if db.execute("SELECT 1 FROM servers WHERE name = ?", (new_name,)).fetchone():
        raise HTTPException(409, tr("servers.server_already_exists", name=new_name))
    if db.execute("SELECT 1 FROM servers WHERE port = ?", (new_port,)).fetchone():
        raise HTTPException(409, tr("servers.port_taken_by_another_server", port=new_port))

    spec = spec_from_row(row)
    try:
        if get_runtime(row["runtime"]).status(spec).state.value == "running":
            raise HTTPException(409, tr("servers.clone_source_running"))
    except DockerError as e:
        raise HTTPException(502, tr("api_v1.docker_error", detail=e))

    new_id = str(uuid_lib.uuid4())
    dst_dir = server_data.SERVERS_DIR / new_id
    try:
        src_dir = server_data.direct_server_dir(server_data.SERVERS_DIR, uuid)
        server_data.copy_server_tree(src_dir, dst_dir)
    except ValueError:
        raise HTTPException(400, tr("servers.forbidden_path", path=uuid))
    except OSError:
        raise HTTPException(500, tr("servers.clone_source_not_found", directory=uuid))

    # Подправить meta.json и server-settings.json (имя), если есть
    meta_path = dst_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        meta = {}
    meta.update({"uuid": new_id, "name": new_name,
                 "version": row["version"], "port": new_port})
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    settings_path = dst_dir / "config" / "server-settings.json"
    try:
        ss = json.loads(settings_path.read_text(encoding="utf-8"))
        ss["name"] = new_name
        settings_path.write_text(json.dumps(ss, indent=2, ensure_ascii=False), encoding="utf-8")
    except (FileNotFoundError, json.JSONDecodeError):
        pass  # бывает у битых конфигов — продолжаем, юзер починит из UI

    try:
        db.execute(
            "INSERT INTO servers (uuid, name, version, port, runtime) VALUES (?, ?, ?, ?, ?)",
            (new_id, new_name, row["version"], new_port, row["runtime"]),
        )
    except sqlite3.IntegrityError as e:
        # Откатываем созданную папку: иначе при попытке клонировать снова
        # с тем же именем после фикса конфликта — папка уже на месте.
        try: shutil.rmtree(dst_dir)
        except OSError: pass
        raise HTTPException(409, unique_conflict_message(e))
    audit(db, "server.clone", new_id,
          summary=f"clone «{row['name']}» → «{new_name}» :{new_port}")
    db.commit()
    return RedirectResponse(
        url=f"/servers/{new_id}?msg=" + urllib.parse.quote(
            tr("servers.clone_complete", name=row['name'])
        ),
        status_code=303,
    )


@router.post("/{uuid}/delete")
def server_delete(request: Request, uuid: str,
                  confirm_name: str = Form(...),
                  db: sqlite3.Connection = Depends(get_db)):
    # Step-up: critical action — нужно свежее подтверждение пароля/TOTP
    if (r := stepup_check_or_redirect(request)) is not None: return r
    """Полное удаление сервера: docker rm -f, rm -rf servers/<uuid>/, чистка БД.
    Подтверждение вводом имени — операция необратимо удаляет моды/сейвы/историю.

    Падение docker не блокирует чистку: иначе сервер останется «битым» в UI
    (БД-строка осталась, контейнера нет, Docker недоступен — start не сработает)."""
    row = require_server(db, uuid)
    if confirm_name.strip() != row["name"]:
        raise HTTPException(400, tr("servers.delete_name_mismatch", name=row['name']))
    spec = spec_from_row(row)

    docker_err: str | None = None
    try:
        get_runtime(row["runtime"]).remove(spec)
    except DockerError as e:
        docker_err = str(e)

    # FK не объявлены в схеме — чистим связанные таблицы вручную.
    delete_server_records(db, uuid)
    audit(db, "server.delete", None,
          summary=f"delete «{row['name']}» ({uuid})")
    db.commit()

    # На Windows docker может ещё держать write-handle на logs/; ignore_errors
    # позволяет довести удаление БД до конца. На Linux — типовой косяк: образ
    # factoriotools пишет файлы под uid 845 (PUID по умолчанию), а панель
    # запущена под non-root юзером — rmtree падает с PermissionError. Фоллбек ниже
    # делает chown через временный alpine-контейнер (root внутри docker'а),
    # после чего обычный rmtree уже справляется.
    sdir = server_data.SERVERS_DIR / uuid
    fs_leftover = False
    fallback_msg: str | None = None
    if sdir.exists():
        shutil.rmtree(sdir, ignore_errors=True)
        if sdir.exists():
            fallback_msg = remove_directory_via_docker(sdir)
            fs_leftover = sdir.exists()

    msgs = [tr("servers.server_deleted", name=row['name'])]
    if docker_err:
        msgs.append(tr("api_v1.docker_error", detail=docker_err))
    if fs_leftover:
        extra = f" ({fallback_msg})" if fallback_msg else ""
        msgs.append(tr(
            "servers.delete_directory_leftover",
            directory=sdir,
            detail=extra,
        ))
    qs = "?msg=" + urllib.parse.quote(" ".join(msgs))
    return RedirectResponse(url=f"/{qs}", status_code=303)


router.include_router(server_files.router)
router.include_router(server_saves.router)
router.include_router(server_players.router)
router.include_router(server_schedule.router)
router.include_router(server_config.router)
