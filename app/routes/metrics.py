"""JSON-API для вкладки «Показатели»: метрики контейнера, онлайн-гистограмма,
сессии, агрегаты. Все эндпойнты под /servers/{uuid}/api/...

Параметр range — 1d/7d/30d/90d/365d (или any_int + 'd'/'h'). Дефолт 1d.
mode для online-hist: 'unique' (distinct ники в бине) или 'peak' (max concurrent).
"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import get_db
from ..i18n import tr

router = APIRouter(prefix="/servers")


def _parse_range(s: str) -> int:
    """'1d' → 86400, '12h' → 43200. Принимает только d/h."""
    s = (s or "1d").strip().lower()
    if not s:
        return 86400
    try:
        if s.endswith("h"):
            return max(60, int(s[:-1]) * 3600)
        if s.endswith("d"):
            return max(60, int(s[:-1]) * 86400)
    except ValueError:
        pass
    raise HTTPException(400, tr("metrics.bad_range", value=repr(s)))


def _bins(range_sec: int) -> tuple[int, int]:
    """Возвращает (n_bins, bin_size_sec). Целимся в ~24..52 столбца."""
    if range_sec <= 86400:        # 1 день → 24 часовых
        return 24, 3600
    if range_sec <= 7 * 86400:    # неделя → 28 шестичасовых
        return 28, 6 * 3600
    if range_sec <= 30 * 86400:   # месяц → 30 суточных
        return 30, 86400
    if range_sec <= 90 * 86400:   # 3 мес → 45 двусуточных
        return 45, 2 * 86400
    if range_sec <= 365 * 86400:  # год → 52 семидневных
        return 52, 7 * 86400
    return 60, range_sec // 60


def _server_exists(db: sqlite3.Connection, uuid: str) -> None:
    if not db.execute("SELECT 1 FROM servers WHERE uuid = ?", (uuid,)).fetchone():
        raise HTTPException(404, tr("common.server_not_found"))


@router.get("/{uuid}/api/metrics")
def api_metrics(uuid: str,
                period: str = Query("1d", alias="range"),
                db: sqlite3.Connection = Depends(get_db)):
    """CPU/Mem за период. Дауинсемплим к ~200 точкам через AVG в бинах,
    иначе при 30-секундном такте за сутки накопится 2880 точек — много для линии."""
    _server_exists(db, uuid)
    rsec = _parse_range(period)
    now = int(time.time())
    since = now - rsec
    target_points = 200
    bin_size = max(60, rsec // target_points)

    rows = db.execute(
        f"""
        SELECT
          (ts / {bin_size}) * {bin_size} AS bin,
          AVG(cpu_pct)   AS cpu,
          AVG(mem_bytes) AS mem,
          AVG(mem_pct)   AS mem_pct
        FROM metrics_samples
        WHERE server_uuid = ? AND ts >= ?
        GROUP BY bin
        ORDER BY bin
        """,
        (uuid, since),
    ).fetchall()
    return {
        "range_sec": rsec,
        "bin_sec": bin_size,
        "points": [
            {"ts": r["bin"], "cpu": round(r["cpu"], 2),
             "mem": int(r["mem"]), "mem_pct": round(r["mem_pct"], 2)}
            for r in rows
        ],
    }


def _sessions_in_range(db: sqlite3.Connection, uuid: str,
                       since: int, until: int) -> list[dict]:
    """Сессии, которые хоть как-то попадают в [since, until]."""
    rows = db.execute(
        """
        SELECT name, name_lc, joined_at, left_at
        FROM player_sessions
        WHERE server_uuid = ?
          AND joined_at < ?
          AND (left_at IS NULL OR left_at >= ?)
        ORDER BY joined_at
        """,
        (uuid, until, since),
    ).fetchall()
    return [dict(r) for r in rows]


def _open_now(db: sqlite3.Connection, uuid: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM player_sessions WHERE server_uuid = ? AND left_at IS NULL",
        (uuid,),
    ).fetchone()[0]


@router.get("/{uuid}/api/sessions")
def api_sessions(uuid: str,
                 period: str = Query("1d", alias="range"),
                 db: sqlite3.Connection = Depends(get_db)):
    _server_exists(db, uuid)
    rsec = _parse_range(period)
    now = int(time.time())
    since = now - rsec
    sess = _sessions_in_range(db, uuid, since, now + 1)
    return {
        "range_sec": rsec,
        "now": now,
        "since": since,
        "sessions": [
            {
                "name": s["name"],
                "joined_at": s["joined_at"],
                "left_at": s["left_at"],   # null = ещё онлайн
                "duration": (s["left_at"] or now) - s["joined_at"],
            }
            for s in sess
        ],
    }


@router.get("/{uuid}/api/online-hist")
def api_online_hist(uuid: str,
                    period: str = Query("1d", alias="range"),
                    mode: str = "unique",
                    db: sqlite3.Connection = Depends(get_db)):
    """Гистограмма онлайна по бинам.
       mode=unique — кол-во разных ников в бине;
       mode=peak   — макс одновременных в бине (sweepline по событиям).
    """
    _server_exists(db, uuid)
    if mode not in ("unique", "peak"):
        raise HTTPException(400, tr("metrics.invalid_online_histogram_mode"))
    rsec = _parse_range(period)
    n_bins, bin_size = _bins(rsec)
    now = int(time.time())
    end = now
    start = end - n_bins * bin_size
    sess = _sessions_in_range(db, uuid, start, end + 1)

    bins_unique: list[set[str]] = [set() for _ in range(n_bins)]
    bins_peak: list[int] = [0] * n_bins

    for s in sess:
        j = s["joined_at"]
        l = s["left_at"] if s["left_at"] is not None else end
        # Клиппим к окну
        j = max(j, start)
        l = min(l, end)
        if l <= j:
            continue
        b_lo = max(0, (j - start) // bin_size)
        b_hi = min(n_bins - 1, (l - start - 1) // bin_size)
        for b in range(b_lo, b_hi + 1):
            bins_unique[b].add(s["name_lc"])

    if mode == "peak":
        # Sweepline: для каждого бина считаем максимум одновременных.
        # Пробегаем все события (j +1, l -1) и группируем по бину.
        events_by_bin: dict[int, list[tuple[int, int]]] = {}
        for s in sess:
            j = max(s["joined_at"], start)
            l = min((s["left_at"] if s["left_at"] is not None else end), end)
            if l <= j:
                continue
            b_lo = max(0, (j - start) // bin_size)
            b_hi = min(n_bins - 1, (l - start - 1) // bin_size)
            for b in range(b_lo, b_hi + 1):
                evs = events_by_bin.setdefault(b, [])
                bin_start_ts = start + b * bin_size
                bin_end_ts = bin_start_ts + bin_size
                evs.append((max(j, bin_start_ts), +1))
                evs.append((min(l, bin_end_ts), -1))
        for b, evs in events_by_bin.items():
            evs.sort()
            cur = peak = 0
            for _ts, delta in evs:
                cur += delta
                if cur > peak:
                    peak = cur
            bins_peak[b] = peak

    out_bins = []
    for i in range(n_bins):
        out_bins.append({
            "ts_start": start + i * bin_size,
            "ts_end":   start + (i + 1) * bin_size,
            "value":    (len(bins_unique[i]) if mode == "unique" else bins_peak[i]),
        })
    return {
        "range_sec": rsec, "bin_sec": bin_size, "mode": mode,
        "bins": out_bins,
    }


@router.get("/{uuid}/api/stats")
def api_stats(uuid: str,
              period: str = Query("1d", alias="range"),
              db: sqlite3.Connection = Depends(get_db)):
    """Карточки сверху: уникальных, сессий, наиграно, пик одновр., средн. одновр."""
    _server_exists(db, uuid)
    rsec = _parse_range(period)
    now = int(time.time())
    since = now - rsec
    sess = _sessions_in_range(db, uuid, since, now + 1)

    unique = len({s["name_lc"] for s in sess})
    total_sessions = len(sess)
    total_play = 0  # секунд игрового времени, клиппнутого в окно

    # Sweepline для пика и среднего одновр.
    events: list[tuple[int, int]] = []
    for s in sess:
        j = max(s["joined_at"], since)
        l = min((s["left_at"] if s["left_at"] is not None else now), now)
        if l <= j:
            continue
        total_play += (l - j)
        events.append((j, +1))
        events.append((l, -1))
    events.sort()
    peak = cur = 0
    last_ts = since
    weighted = 0  # ∫ concurrent dt
    for ts, d in events:
        weighted += cur * (ts - last_ts)
        cur += d
        if cur > peak:
            peak = cur
        last_ts = ts
    weighted += cur * (now - last_ts)
    avg = weighted / rsec if rsec else 0.0

    return {
        "range_sec": rsec,
        "unique": unique,
        "sessions": total_sessions,
        "play_seconds": total_play,
        "peak_concurrent": peak,
        "avg_concurrent": round(avg, 2),
        "online_now": _open_now(db, uuid),
    }
