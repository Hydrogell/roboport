"""Startup reconciliation and background tasks owned by the running panel."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import webhooks
from .db import connect, init_db
from .runtime import get_runtime
from .runtime.docker import DockerError
from .sampler import sampler_loop
from .schedule_runner import schedule_loop
from .server_data import spec_from_row


def _autostart_rows() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT uuid, name, version, port, runtime, network_mode FROM servers WHERE autostart = 1"
        ).fetchall()


async def _autostart_servers() -> None:
    try:
        rows = await asyncio.to_thread(_autostart_rows)
    except sqlite3.Error as exc:
        print(f"[autostart] failed to read servers: {exc}", flush=True)
        return
    for row in rows:
        try:
            runtime = get_runtime(row["runtime"])
            await asyncio.to_thread(runtime.start, spec_from_row(row))
            print(f"[autostart] {row['name']}: started")
        except DockerError as exc:
            print(f"[autostart] {row['name']}: docker error: {exc}")
        except Exception as exc:
            print(f"[autostart] {row['name']}: unexpected: {exc!r}")


def _settle_pending_updates() -> None:
    # A separate transient watchdog writes success/rollback after its local
    # health check.  Do not mark a just-restarted update successful here.
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            "UPDATE panel_updates SET status = 'failed', finished_at = COALESCE(finished_at, ?), "
            "error = COALESCE(error, 'watchdog did not finish') "
            "WHERE status = 'running' AND started_at < ?",
            (now, now - 600),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        _settle_pending_updates()
    except sqlite3.Error as exc:
        print(f"[startup] _settle_pending_updates: {exc!r}", flush=True)

    autostart = asyncio.create_task(_autostart_servers())
    app.state.sampler_task = asyncio.create_task(sampler_loop())
    app.state.schedule_task = asyncio.create_task(schedule_loop())
    app.state.webhook_dispatcher = asyncio.create_task(webhooks.dispatcher_loop())
    tasks = (autostart, app.state.sampler_task, app.state.schedule_task, app.state.webhook_dispatcher)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
