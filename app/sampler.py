"""Фоновый поллер метрик и игровых сессий.

Запускается на старте FastAPI как asyncio.Task, тикает каждые SAMPLE_INTERVAL
секунд. Для каждого сервера в БД:
  1) если контейнер running — `docker stats --no-stream` снимает CPU%/Mem.
  2) если RCON доступен — `/players` снимает онлайн-список и обновляет
     player_sessions: новые ники открывают сессию, исчезнувшие — закрывают.
  3) если контейнер не running — закрываем все открытые сессии этого сервера.

Все sync-вызовы (subprocess docker, blocking RCON socket) гоним через
asyncio.to_thread, чтобы не блокировать event loop. Любая ошибка на одном
сервере не должна валить тик целиком — ловим и логируем.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time

from . import auth as auth_lib
from . import webhooks as wh_lib
from .db import connect
from .runtime.base import ServerSpec, ServerState
from .runtime.docker import DockerError, DockerRuntime, _inspect
from .schedules import parse_schedule as parse_schedule  # Existing import path.
from .server_data import spec_from_row

SAMPLE_INTERVAL = 30           # секунд между тиками
RETAIN_DAYS = 30               # храним сэмплы и сессии за последние 30 дней
PRUNE_EVERY_TICKS = 120        # ~раз в час чистим старое (120 * 30s = 1ч)

# ---------- Парсеры docker stats ----------

_MEM_UNITS = {
    "B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
    "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
}


def _parse_mem(s: str) -> int:
    """Парсит '118.7MiB' / '7.764GiB' → байты. Возвращает 0 на нераспознанном."""
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B)\s*$", s or "")
    if not m:
        return 0
    return int(float(m.group(1)) * _MEM_UNITS.get(m.group(2), 1))


def _parse_pct(s: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*%?\s*$", s or "")
    return float(m.group(1)) if m else 0.0


def _docker_stats(container_name: str) -> dict | None:
    """`docker stats --no-stream --format {{json .}} <name>` → dict или None.
    None если контейнера нет или docker не отвечает."""
    try:
        p = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", container_name],
            check=False, timeout=15,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    line = (p.stdout or "").strip().splitlines()
    if not line:
        return None
    try:
        return json.loads(line[0])
    except json.JSONDecodeError:
        return None


# ---------- Sampler ----------

def _sample_metrics(spec: ServerSpec, ts: int) -> tuple | None:
    """Снимок метрик контейнера. Возвращает (ts, cpu_pct, mem_bytes, mem_pct) или None."""
    raw = _docker_stats(spec.container_name)
    if not raw:
        return None
    cpu = _parse_pct(raw.get("CPUPerc", "0%"))
    mem_pct = _parse_pct(raw.get("MemPerc", "0%"))
    mem_use = (raw.get("MemUsage") or "").split("/", 1)[0]
    mem_bytes = _parse_mem(mem_use)
    return (ts, cpu, mem_bytes, mem_pct)


def _online_set(spec: ServerSpec) -> set[str] | None:
    """RCON /players → множество имён онлайн (lower-case key, оригинал в значении).
    None если RCON недоступен (нет rconpw, контейнер только встаёт, и т.п.)."""
    try:
        rt = DockerRuntime()
        txt = rt.send_command(spec, "/players")
    except Exception:
        return None
    out: dict[str, str] = {}
    for raw in (txt or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("Players"):
            continue
        if line.endswith("(online)"):
            line = line[:-len("(online)")].strip()
            if line:
                out[line.lower()] = line
    # NB: /players без (online) рядом — это тоже online-маркер, потому что
    # в Factorio 2.x вывод иногда не помечает скобкой. Если нашли только
    # «(online)»-строки — ок, иначе в out оказалось бы пусто. Этот случай
    # ниже отрабатывает обычно (Factorio помечает (online) почти всегда).
    return set(out.values())


def _sync_sessions(conn, server_uuid: str, online: set[str], ts: int) -> None:
    """Привести player_sessions к новому состоянию: открыть для появившихся,
    закрыть для исчезнувших. Имена нормализуем CI (Factorio пишет lower-case).
    Side-effect (Phase 4): эмитим webhook'и player.joined / player.left."""
    cur = conn.execute(
        "SELECT id, name, name_lc FROM player_sessions "
        "WHERE server_uuid = ? AND left_at IS NULL",
        (server_uuid,),
    ).fetchall()
    open_lc = {r["name_lc"]: r["id"] for r in cur}
    open_names = {r["name_lc"]: r["name"] for r in cur}
    online_idx = {n.lower(): n for n in online}
    # Имя сервера для payload'а webhook'а
    srv_row = conn.execute(
        "SELECT name FROM servers WHERE uuid = ?", (server_uuid,),
    ).fetchone()
    srv_name = srv_row["name"] if srv_row else None
    # Закрыть тех, кого больше нет в online
    left_lcs = list(open_lc.keys() - online_idx.keys())
    closed = [open_lc[lc] for lc in left_lcs]
    if closed:
        conn.executemany(
            "UPDATE player_sessions SET left_at = ? WHERE id = ?",
            [(ts, sid) for sid in closed],
        )
    for lc in left_lcs:
        wh_lib.emit("player.left", server_uuid=server_uuid,
                    server_name=srv_name, player=open_names.get(lc, lc))
    # Открыть новые
    joined_lcs = list(online_idx.keys() - open_lc.keys())
    new = [(server_uuid, online_idx[lc], lc, ts) for lc in joined_lcs]
    if new:
        conn.executemany(
            "INSERT INTO player_sessions (server_uuid, name, name_lc, joined_at) "
            "VALUES (?, ?, ?, ?)",
            new,
        )
    for lc in joined_lcs:
        wh_lib.emit("player.joined", server_uuid=server_uuid,
                    server_name=srv_name, player=online_idx[lc])


def _close_all_sessions(conn, server_uuid: str, ts: int) -> None:
    conn.execute(
        "UPDATE player_sessions SET left_at = ? "
        "WHERE server_uuid = ? AND left_at IS NULL",
        (ts, server_uuid),
    )


# Crash-detection: запоминаем последнее состояние per-server. Если переход
# running → не-running и ExitCode != 0 → эмитим server.crashed.
# Reset на старте процесса — приемлемо для соло-панели (если упал во время
# рестарта панели, событие пропустим).
_LAST_STATE: dict[str, str] = {}


async def _tick_one(row, ts: int) -> None:
    """Один цикл сбора по одному серверу. Все ошибки гасим — не должны валить тик."""
    spec = spec_from_row(row)
    server_uuid = row["uuid"]

    # Состояние контейнера
    try:
        insp = await asyncio.to_thread(_inspect, spec.container_name)
    except DockerError:
        insp = None
    state = (insp or {}).get("State", {}).get("Status", "") if insp else ""
    is_running = state == "running"

    # Crash-detection: running → exited с ExitCode != 0 (не пользовательский stop).
    prev = _LAST_STATE.get(server_uuid)
    if prev == "running" and state in ("exited", "dead"):
        exit_code = (insp or {}).get("State", {}).get("ExitCode", 0) if insp else 0
        oom_killed = (insp or {}).get("State", {}).get("OOMKilled", False) if insp else False
        if exit_code != 0 or oom_killed:
            wh_lib.emit("server.crashed", server_uuid=server_uuid,
                        server_name=row["name"], exit_code=exit_code,
                        oom_killed=bool(oom_killed))
    _LAST_STATE[server_uuid] = state or "missing"

    if not is_running:
        # Контейнер не работает — закрываем висящие сессии и не пишем метрики.
        with connect() as conn:
            _close_all_sessions(conn, server_uuid, ts)
            conn.commit()
        return

    # Метрики (могут быть None при гонке остановки/старта — это ок).
    metrics = await asyncio.to_thread(_sample_metrics, spec, ts)
    online = await asyncio.to_thread(_online_set, spec)

    with connect() as conn:
        if metrics:
            conn.execute(
                "INSERT INTO metrics_samples "
                "(server_uuid, ts, cpu_pct, mem_bytes, mem_pct) VALUES (?, ?, ?, ?, ?)",
                (server_uuid, *metrics),
            )
        # Если RCON не ответил — не трогаем сессии (не знаем кто онлайн).
        # Иначе — открываем/закрываем по диффу.
        if online is not None:
            _sync_sessions(conn, server_uuid, online, ts)
        conn.commit()


def _prune(now: int) -> None:
    """Часовая чистка БД от мусора. Гоняется через asyncio.to_thread каждые
    PRUNE_EVERY_TICKS sampler-тиков. Чистит две группы:

      1) Метрики и закрытые игровые сессии старше RETAIN_DAYS.
      2) Auth-мусор: истёкшие сессии/pending-2FA/баны, старые auth_attempts
         и recovery-токены. У каждого свой TTL внутри auth_lib, sampler
         только запускает sweep по расписанию.

    Логируем одной строкой если что-то реально удалилось — в idle-режиме
    в журнал ничего не сыпется."""
    cutoff = now - RETAIN_DAYS * 86400
    with connect() as conn:
        cur = conn.execute("DELETE FROM metrics_samples WHERE ts < ?", (cutoff,))
        n_metrics = cur.rowcount or 0
        # Закрытые сессии старше cutoff — удаляем; открытые не трогаем.
        cur = conn.execute(
            "DELETE FROM player_sessions WHERE left_at IS NOT NULL AND left_at < ?",
            (cutoff,),
        )
        n_psessions = cur.rowcount or 0

        # Auth-мусор. Lazy-cleanup внутри auth-роутов покрывает только
        # ту строку, которую сейчас читают; здесь делаем общий sweep.
        n_sessions = auth_lib.cleanup_expired_sessions(conn)
        n_attempts = auth_lib.cleanup_old_attempts(conn)
        n_pending = auth_lib.cleanup_expired_pending_2fa(conn)
        n_recovery = auth_lib.cleanup_old_recovery_tokens(conn)
        n_bans = auth_lib.cleanup_expired_bans(conn)
        n_apikeys = auth_lib.cleanup_expired_api_keys(conn)
        n_apilog = auth_lib.cleanup_old_api_requests(conn)
        n_deliveries = wh_lib.cleanup_old_deliveries(conn)

        conn.commit()

    parts = []
    if n_metrics:   parts.append(f"metrics={n_metrics}")
    if n_psessions: parts.append(f"player_sessions={n_psessions}")
    if n_sessions:  parts.append(f"sessions={n_sessions}")
    if n_attempts:  parts.append(f"auth_attempts={n_attempts}")
    if n_pending:   parts.append(f"pending_2fa={n_pending}")
    if n_recovery:  parts.append(f"recovery_tokens={n_recovery}")
    if n_bans:      parts.append(f"ip_bans={n_bans}")
    if n_apikeys:   parts.append(f"api_keys={n_apikeys}")
    if n_apilog:    parts.append(f"api_request_log={n_apilog}")
    if n_deliveries: parts.append(f"webhook_deliveries={n_deliveries}")
    if parts:
        print(f"[prune] removed: {', '.join(parts)}", flush=True)


async def sampler_loop() -> None:
    """Главный цикл. Бесконечно тикает с шагом SAMPLE_INTERVAL."""
    tick = 0
    while True:
        tick += 1
        try:
            now = int(time.time())
            with connect() as conn:
                rows = conn.execute("SELECT * FROM servers").fetchall()
            await asyncio.gather(*(_tick_one(r, now) for r in rows), return_exceptions=True)
            if tick % PRUNE_EVERY_TICKS == 0:
                await asyncio.to_thread(_prune, now)
        except Exception as e:
            # Логируем, но цикл не валим — фоновая задача должна жить.
            print(f"[sampler] tick failed: {e!r}")
        await asyncio.sleep(SAMPLE_INTERVAL)
