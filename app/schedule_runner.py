"""Execute Factorio server schedules independently of the metrics sampler."""
from __future__ import annotations

import asyncio
import threading
import time
from itertools import pairwise

from . import webhooks
from .db import connect
from .runtime.base import ServerSpec
from .runtime.docker import DockerError, DockerRuntime, _inspect
from .schedules import due_tasks, parse_schedule, record_task_run, should_fire
from .server_data import spec_from_row

SCHEDULE_INTERVAL = 30


def _run_command(spec: ServerSpec, runtime: DockerRuntime, command: str,
                 stop: threading.Event) -> str | None:
    """Произвольная RCON-команда. Если сервер не running — пропуск."""
    if not (command or "").strip():
        return "error: empty command"
    if stop.is_set():
        return None
    insp = _inspect(spec.container_name)
    state = (insp or {}).get("State", {}).get("Status", "") if insp else ""
    if stop.is_set():
        return None
    if state != "running":
        return "skipped: not running"
    try:
        runtime.send_command(spec, command.strip())
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"[:200]
    return "ok"


def _run_restart(spec: ServerSpec, runtime: DockerRuntime, warn_seconds: int,
                 stop: threading.Event) -> str | None:
    """Warn online players at the requested time and at 30, 10 and 5 seconds."""
    if stop.is_set():
        return None
    insp = _inspect(spec.container_name)
    state = (insp or {}).get("State", {}).get("Status", "") if insp else ""
    if warn_seconds > 0 and state == "running":
        marks = [warn_seconds, *(n for n in (30, 10, 5) if n < warn_seconds), 0]
        for remaining, next_mark in pairwise(marks):
            if stop.is_set():
                return None
            try:
                runtime.send_command(spec, f"/server-message Перезапуск через {remaining} сек")
            except Exception:
                pass  # A missed announcement should not prevent the scheduled restart.
            if stop.wait(remaining - next_mark):
                return None
    if stop.is_set():
        return None
    try:
        runtime.restart(spec)
    except DockerError as e:
        return f"error: {e}"[:200]
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"[:200]
    return "ok"


def _tick_schedule(now: int, stop: threading.Event) -> None:
    """Run due tasks in one worker; shutdown never starts a later task."""
    runtime = DockerRuntime()
    with connect() as conn:
        rows = due_tasks(conn)

    for r in rows:
        if stop.is_set():
            return
        parsed = parse_schedule(r["schedule"])
        if parsed is None:
            continue
        if not should_fire(parsed, r["last_run"], now):
            continue
        spec = spec_from_row(r)
        kind = r["kind"]
        if kind == "command":
            status = _run_command(spec, runtime, r["command"] or "", stop)
        elif kind == "restart":
            warn = max(0, int(r["warn_seconds"] or 0))
            status = _run_restart(spec, runtime, warn, stop)
        else:
            status = f"error: unknown kind {kind!r}"
        if status is None:
            return
        with connect() as conn:
            record_task_run(conn, r["id"], int(time.time()), status)
        label = f"{kind}({r['command']})" if kind == "command" else kind
        print(f"[schedule] {spec.name}/{label} ({r['schedule']}) → {status}")
        # Webhook: schedule.fired
        webhooks.emit("schedule.fired",
                    server_uuid=spec.uuid, server_name=spec.name,
                    task_id=r["id"], kind=kind, schedule=r["schedule"],
                    command=r["command"], status=status)


async def schedule_loop() -> None:
    """Run schedules independently from metrics. Cancellation stops countdowns."""
    stop = threading.Event()
    while True:
        worker = asyncio.create_task(asyncio.to_thread(_tick_schedule, int(time.time()), stop))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop.set()
            await asyncio.shield(worker)
            raise
        except Exception as e:
            print(f"[schedule] tick failed: {e!r}")
        await asyncio.sleep(SCHEDULE_INTERVAL)
