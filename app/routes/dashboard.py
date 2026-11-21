"""Дашборд: общий обзор хоста и список серверов.

Сюда же повесил /api/notifications — feed для колокольчика в topbar:
join/leave игроков за 24ч + апдейты модов и Factorio-версий."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from .. import server_data
from ..db import get_db
from ..factorio_mods import annotate_with_updates, factorio_major_minor, list_installed
from ..factorio_versions import available_versions
from ..server_data import spec_from_row
from ..runtime import ServerSpec, get_runtime
from ..runtime.docker import DockerError
from ..i18n import tr

router = APIRouter()

ROOT = Path(__file__).resolve().parents[2]


def _version_tuple(s: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in (s or "").split("."))
    except ValueError:
        return None


def _creds_warning() -> dict | None:
    """Если FACTORIO_USERNAME/TOKEN не выставлены — отдадим запись в feed
    колокольчика чтобы юзер видел при первом же открытии."""
    import os
    if os.environ.get("FACTORIO_USERNAME", "").strip() and os.environ.get("FACTORIO_TOKEN", "").strip():
        return None
    return {
        "kind": "creds-missing",
        "message": tr("dashboard.factorio_credentials_missing"),
    }


def _build_update_feed(db: sqlite3.Connection) -> list[dict]:
    """Апдейты для колокольчика: для каждого сервера в БД проверить
    Factorio-версию (Docker Hub) и моды (локальный mods_cache.json).
    Любая ошибка → пустая секция, в журнале не падаем (это polling-эндпоинт)."""
    out: list[dict] = []
    # Самое первое — варнинг про креденшалы (ставим всегда, не зависит от наличия серверов).
    cw = _creds_warning()
    if cw:
        out.append(cw)
    servers = db.execute("SELECT uuid, name, version FROM servers").fetchall()
    if not servers:
        return out

    # --- Factorio ---
    # Общий каталог версий кэшируется на час.
    latest_t: tuple[int, ...] | None = None
    latest_factorio: str | None = None
    try:
        versions = available_versions()
        if versions:
            latest_factorio = versions[0]
            latest_t = _version_tuple(latest_factorio)
    except Exception:
        pass

    if latest_t and latest_factorio:
        for s in servers:
            cur_t = _version_tuple(s["version"])
            if cur_t and cur_t < latest_t:
                out.append({
                    "kind": "factorio-update",
                    "server_uuid": s["uuid"],
                    "server_name": s["name"],
                    "from_version": s["version"],
                    "to_version": latest_factorio,
                })

    # --- Моды (только если кэш модов синхронизирован) ---
    try:
        for s in servers:
            sdir = server_data.SERVERS_DIR / s["uuid"]
            installed = list_installed(sdir)
            if not installed:
                continue
            annotated = annotate_with_updates(installed, factorio_major_minor(s["version"]))
            for m in annotated:
                if m.get("has_update"):
                    out.append({
                        "kind": "mod-update",
                        "server_uuid": s["uuid"],
                        "server_name": s["name"],
                        "mod_name": m["name"],
                        "mod_title": m.get("title") or m["name"],
                        "from_version": m["version"],
                        "to_version": m["latest_in_cache"],
                    })
    except Exception:
        pass

    return out


# Состояние для дельта-измерения CPU через /proc/stat (Linux-фоллбек без psutil).
# Хранит (idle_total, total) от прошлого замера.
_PROC_CPU_PREV: tuple[int, int] | None = None


def _proc_cpu_percent() -> float | None:
    """CPU% через /proc/stat: дельта non-idle vs total с прошлого вызова. Первый
    вызов вернёт None (нет с чем сравнивать). Только Linux."""
    global _PROC_CPU_PREV
    try:
        with open("/proc/stat", "r", encoding="ascii") as f:
            line = f.readline()
    except OSError:
        return None
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    nums = [int(x) for x in parts[1:]]
    # user nice system idle iowait irq softirq steal guest guest_nice
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
    total = sum(nums)
    prev = _PROC_CPU_PREV
    _PROC_CPU_PREV = (idle, total)
    if prev is None:
        return None
    didle = idle - prev[0]
    dtotal = total - prev[1]
    if dtotal <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - didle / dtotal) * 100.0))


def _proc_mem_percent() -> float | None:
    """MEM% через /proc/meminfo: 100 * (1 - MemAvailable / MemTotal). Linux only."""
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            data = f.read()
    except OSError:
        return None
    info: dict[str, int] = {}
    for line in data.splitlines():
        k, _, rest = line.partition(":")
        v = rest.strip().split()
        if v:
            try:
                info[k.strip()] = int(v[0])
            except ValueError:
                pass
    total = info.get("MemTotal")
    avail = info.get("MemAvailable")
    if not total or avail is None:
        return None
    return max(0.0, min(100.0, (1.0 - avail / total) * 100.0))


def _host_cpu_mem() -> tuple[float | None, float | None]:
    """psutil если есть (кросс-платформенно); иначе Linux /proc; иначе None."""
    try:
        import psutil  # type: ignore
        return psutil.cpu_percent(interval=0.1), psutil.virtual_memory().percent
    except ImportError:
        pass
    if os.path.exists("/proc/stat"):
        return _proc_cpu_percent(), _proc_mem_percent()
    return None, None


def host_stats() -> dict:
    """Снимок состояния хоста. CPU/MEM = None если ни psutil, ни /proc недоступны
    (Windows без psutil) — шаблон отрисует «—» вместо 0.0%."""
    cpu_pct, mem_pct = _host_cpu_mem()
    total, used, _free = shutil.disk_usage(str(ROOT))
    return {
        "cpu_percent": cpu_pct,
        "mem_percent": mem_pct,
        "disk_used_gb": used / (1024 ** 3),
        "disk_total_gb": total / (1024 ** 3),
    }


@router.get("/api/notifications")
def api_notifications(db: sqlite3.Connection = Depends(get_db)):
    """Лента событий за последние 24 часа: входы/выходы игроков.
    Возвращает {events: [{ts, kind, name, server_uuid, server_name}, ...], online: N}."""
    since = int(time.time()) - 24 * 3600
    rows = db.execute(
        """
        SELECT 'join' AS kind, ps.joined_at AS ts, ps.name, ps.server_uuid,
               s.name AS server_name
        FROM player_sessions ps
        LEFT JOIN servers s ON s.uuid = ps.server_uuid
        WHERE ps.joined_at >= ?
        UNION ALL
        SELECT 'leave' AS kind, ps.left_at AS ts, ps.name, ps.server_uuid,
               s.name AS server_name
        FROM player_sessions ps
        LEFT JOIN servers s ON s.uuid = ps.server_uuid
        WHERE ps.left_at IS NOT NULL AND ps.left_at >= ?
        ORDER BY ts DESC
        LIMIT 30
        """,
        (since, since),
    ).fetchall()
    online = db.execute(
        "SELECT COUNT(*) FROM player_sessions WHERE left_at IS NULL"
    ).fetchone()[0]
    return {
        "online": int(online),
        "events": [
            {
                "ts": int(r["ts"]),
                "kind": r["kind"],
                "name": r["name"],
                "server_uuid": r["server_uuid"],
                "server_name": r["server_name"] or tr("dashboard.server_deleted"),
            }
            for r in rows
        ],
        "updates": _build_update_feed(db),
    }


def _status_one(spec: ServerSpec, runtime_name: str) -> str:
    """Без исключений: вернуть state.value или 'unknown' / 'docker_err'."""
    try:
        return get_runtime(runtime_name).status(spec).state.value
    except DockerError:
        return "docker_err"
    except Exception:
        return "unknown"


def _peak_concurrent_since(db: sqlite3.Connection, since: int, until: int) -> int:
    """Sweepline по player_sessions в окне [since, until] — макс одновременных
    игроков. Считается по всем серверам (соло-админ всё равно своё хозяйство)."""
    rows = db.execute(
        "SELECT joined_at, left_at FROM player_sessions "
        "WHERE joined_at < ? AND (left_at IS NULL OR left_at >= ?)",
        (until, since),
    ).fetchall()
    events: list[tuple[int, int]] = []
    for r in rows:
        j = max(r["joined_at"], since)
        l = min(r["left_at"] if r["left_at"] is not None else until, until)
        if l <= j:
            continue
        events.append((j, +1))
        events.append((l, -1))
    events.sort()
    cur = peak = 0
    for _ts, d in events:
        cur += d
        if cur > peak:
            peak = cur
    return peak


def _today_start_local() -> int:
    """Unix-ts начала текущих суток в локальной зоне панели."""
    from datetime import datetime
    from ..panel_config import panel_tz
    now_local = datetime.now(tz=panel_tz())
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _collect_dashboard_state(db: sqlite3.Connection) -> dict:
    """Собрать всё что нужно дашборду: список серверов с состоянием/метриками,
    host stats, агрегаты, спарклайны. Используется и при full-page рендере,
    и при live-polling fragment'е — чтобы UI был один источник правды."""
    rows = db.execute("SELECT * FROM servers ORDER BY created_at").fetchall()

    # Последний сэмпл метрик per-server. SQLite допускает bare-column рядом с
    # MAX() — берёт значения из строки с этим максимумом (ABI-stable since 3.7.11).
    metrics = {r["server_uuid"]: dict(r) for r in db.execute(
        "SELECT server_uuid, MAX(ts) AS ts, cpu_pct, mem_bytes, mem_pct "
        "FROM metrics_samples GROUP BY server_uuid"
    ).fetchall()}

    # Спарклайны: последние 30 мин (sampler пишет каждые 30с → ~60 точек).
    # Сразу в один запрос — потом раскидаем по словарю чтобы не дёргать БД per-row.
    spark_since = int(time.time()) - 30 * 60
    history: dict[str, list[dict]] = {}
    for hr in db.execute(
        "SELECT server_uuid, ts, cpu_pct, mem_pct "
        "FROM metrics_samples WHERE ts >= ? ORDER BY ts ASC",
        (spark_since,),
    ).fetchall():
        history.setdefault(hr["server_uuid"], []).append({
            "ts": int(hr["ts"]),
            "cpu": float(hr["cpu_pct"]) if hr["cpu_pct"] is not None else None,
            "mem": float(hr["mem_pct"]) if hr["mem_pct"] is not None else None,
        })

    # Открытые сессии per-server.
    online_per = dict(db.execute(
        "SELECT server_uuid, COUNT(*) FROM player_sessions "
        "WHERE left_at IS NULL GROUP BY server_uuid"
    ).fetchall())

    # docker inspect параллельно — N раундтрипов сериально дают видимую задержку
    # уже на 3-4 серверах. Пул небольшой (max 8): обычно у соло-админа ≤5 серверов.
    specs = [spec_from_row(row) for row in rows]
    runtimes = [r["runtime"] for r in rows]
    if specs:
        with ThreadPoolExecutor(max_workers=min(8, len(specs))) as ex:
            states = list(ex.map(_status_one, specs, runtimes))
    else:
        states = []

    out_servers: list[dict] = []
    for r, st in zip(rows, states):
        m = metrics.get(r["uuid"])
        # max_players из server-settings.json (0 = безлимит в Factorio).
        # Файл может быть unреадабельный — sentinel 0 (отрисуется как ∞).
        max_players = 0
        try:
            ss_path = server_data.SERVERS_DIR / r["uuid"] / "config" / "server-settings.json"
            ss = json.loads(ss_path.read_text(encoding="utf-8"))
            max_players = int(ss.get("max_players") or 0)
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError, OSError):
            pass
        h = history.get(r["uuid"], [])
        out_servers.append({
            **dict(r),
            "state": st,                                    # running|stopped|starting|unknown|docker_err
            "cpu_pct": m["cpu_pct"] if m else None,
            "mem_bytes": m["mem_bytes"] if m else None,
            "mem_pct": m["mem_pct"] if m else None,
            "metrics_ts": m["ts"] if m else None,
            "players_online": int(online_per.get(r["uuid"], 0)),
            "max_players": max_players,
            # Списки последних значений для inline-spark; None → 0 при отрисовке
            "cpu_spark": [p["cpu"] if p["cpu"] is not None else 0.0 for p in h],
            "mem_spark": [p["mem"] if p["mem"] is not None else 0.0 for p in h],
        })

    online = sum(1 for s in out_servers if s["state"] == "running")
    players_total = sum(s["players_online"] for s in out_servers)
    today_start = _today_start_local()
    players_peak_today = _peak_concurrent_since(db, today_start, int(time.time()))

    # Бесхозные папки — UI-подсказка «у тебя N директорий не привязано к БД».
    known_uuids = {r["uuid"] for r in rows}
    orphans_count = 0
    if server_data.SERVERS_DIR.is_dir():
        for d in server_data.SERVERS_DIR.iterdir():
            if d.is_dir() and d.name not in known_uuids:
                orphans_count += 1
    return {
        "host": host_stats(),
        "servers": out_servers,
        "servers_online": online,
        "servers_offline": len(out_servers) - online,
        "players_total": players_total,
        "players_peak_today": players_peak_today,
        "orphans_count": orphans_count,
    }


@router.get("/")
def dashboard(request: Request, db: sqlite3.Connection = Depends(get_db)):
    state = _collect_dashboard_state(db)
    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "page": "dashboard",
            **state,
            "page_msg": request.query_params.get("msg"),
            "page_err": request.query_params.get("err"),
        },
    )


@router.get("/dashboard/live")
def dashboard_live(request: Request, db: sqlite3.Connection = Depends(get_db)):
    """Live-fragment: только содержимое stat-grid и tbody таблицы серверов.
    Используется JS'ом на дашборде для обновления без F5. Возвращает text/html
    с двумя контейнерами (id='dash-stats-grid' и id='dash-tbody') — JS свопает
    их innerHTML."""
    state = _collect_dashboard_state(db)
    return request.app.state.templates.TemplateResponse(
        request,
        "_partials/dashboard_live.html",
        {"page": "dashboard", **state},
    )
