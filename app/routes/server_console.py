"""Stream Factorio logs and RCON replies over the authenticated WebSocket."""
from __future__ import annotations

import asyncio
import queue as queue_mod
import threading
import time
from contextvars import copy_context

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import server_data
from .. import auth
from ..db import audit, connect
from ..i18n import tr
from ..runtime import ServerSpec
from ..runtime.docker import DockerError, DockerRuntime
from ..server_data import spec_from_row


router = APIRouter()


# A console can remain open for hours.  Recheck its server-side authority often
# enough to bound delivery after a logout/ban, but never once per log line.
SESSION_CHECK_INTERVAL_SEC = 15


def _session_is_authorized(session_id: str, ip: str) -> bool:
    """Return whether an already-open console may continue to receive data.

    Unlike ``auth.load_session`` this deliberately does not refresh
    ``last_seen_at`` or ``expires_at``: a passive log subscription must not keep
    a session alive forever.  Database trouble is an authorization failure.
    """
    if not session_id:
        return False
    try:
        with connect() as conn:
            if auth.is_ip_banned(conn, ip):
                conn.commit()  # persist lazy cleanup of an expired ban
                return False
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND expires_at >= ?",
                (session_id, int(time.time())),
            ).fetchone()
            conn.commit()
            return row is not None
    except Exception as e:
        print(f"[ws] console session DB error: {e}", flush=True)
        return False


def _spec_from_uuid(uuid: str) -> ServerSpec | None:
    """Достать spec из БД, минуя FastAPI dependency (ws-эндпоинты её не получают)."""
    with connect() as conn:
        row = server_data.find_server(conn, uuid)
    return spec_from_row(row) if row else None


@router.websocket("/ws/servers/{uuid}/logs")
async def ws_logs(ws: WebSocket, uuid: str):
    """Двунаправленный WS:
      server → client: live строки `docker logs -f` (живые логи Factorio).
      client → server: текст команды → отправляем в RCON → ответ обратно как
                       строки с префиксом `[rcon]`.

    Аутентификация: ws_auth_or_close проверяет cookie/IP-ban/Origin ДО
    accept(); если что-то не так — handshake закрывается с code=1008,
    фронт по этому коду редиректнет на /login.
    """
    from . import auth_routes as _ar
    sess = await _ar.ws_auth_or_close(ws)
    if sess is None:
        return

    await ws.accept()
    session_id = sess.id
    client_ip = _ar.client_ip(ws)
    spec = _spec_from_uuid(uuid)
    if spec is None:
        await ws.send_text("[panel] " + tr("servers.ws_server_not_found"))
        await ws.close(code=1000)
        return

    runtime = DockerRuntime()
    try:
        rt_status = runtime.status(spec)
    except DockerError as e:
        await ws.send_text("[panel] " + tr("servers.ws_docker_unavailable_line", detail=e))
        await ws.close(code=1000)
        return

    # Контейнер не работает — отдадим snapshot последних логов и закроем WS
    # с code=1000 (normal). Без этого фронт реконнектил, бэк запускал новый
    # `docker logs -f --tail 100` для stopped-контейнера → последние 100 строк
    # выводятся и поток завершается → reconnect → повтор: бесконечный спам.
    if rt_status.state.value != "running":
        if rt_status.container_id is None:
            await ws.send_text("[panel] " + tr("servers.ws_container_not_created_line"))
        else:
            try:
                snap = await asyncio.to_thread(runtime.logs, spec, 100)
            except DockerError as e:
                snap = "[panel] " + tr("api_v1.docker_error", detail=e)
            # A snapshot can take long enough for logout, expiry or an IP ban.
            # Do not release already collected log history on a stale session.
            if not await asyncio.to_thread(_session_is_authorized, session_id, client_ip):
                await ws.close(code=1008)
                return
            for line in (snap or "").rstrip("\n").splitlines():
                await ws.send_text(line)
            await ws.send_text(
                "[panel] " + tr("servers.ws_container_not_running_line", state=rt_status.state.value)
            )
        await ws.close(code=1000)
        return

    # Очередь в которую и продьюсер логов, и обработчик команд кладут строки.
    q: queue_mod.Queue = queue_mod.Queue(maxsize=1000)
    stop_flag = threading.Event()

    def push(line: str) -> None:
        try:
            q.put_nowait(line)
        except queue_mod.Full:
            try: q.get_nowait()
            except queue_mod.Empty: pass
            q.put_nowait(line)

    def log_producer():
        try:
            for line in runtime.stream_logs(spec, stop_event=stop_flag):
                if stop_flag.is_set():
                    break
                push(line)
        except DockerError as e:
            push("[panel] " + tr("api_v1.docker_error", detail=e))
        finally:
            push("__SENTINEL__")

    log_context = copy_context()
    t = threading.Thread(target=log_context.run, args=(log_producer,), daemon=True)
    t.start()

    loop = asyncio.get_event_loop()

    async def pump_outgoing():
        while True:
            line = await loop.run_in_executor(None, q.get)
            if line == "__SENTINEL__":
                await ws.send_text("[panel] " + tr("servers.ws_log_stream_ended_line"))
                return
            await ws.send_text(line)

    async def pump_incoming():
        import traceback
        from ..runtime.rcon import RconAuthError, RconError
        while True:
            cmd = (await ws.receive_text()).strip()
            if not cmd:
                continue
            # Commands are never forwarded based on a stale handshake.
            if not await asyncio.to_thread(_session_is_authorized, session_id, client_ip):
                await ws.close(code=1008)
                return
            await ws.send_text(f"[rcon] > {cmd}")
            # Audit: кто и какую команду отправил. RCON в Factorio = root,
            # это самый чувствительный лог-trail в панели.
            try:
                with connect() as _ac:
                    audit(_ac, "rcon.cmd", uuid,
                          summary=f"{sess.username}: {cmd[:200]}")
                    _ac.commit()
            except Exception:
                pass
            try:
                # RCON-вызов синхронный, гоняем в треде чтобы не блокировать loop
                resp = await asyncio.to_thread(runtime.send_command, spec, cmd)
            except RconAuthError as e:
                await ws.send_text("[rcon] " + tr("api_v1.rcon_auth_error", detail=e))
                continue
            except (RconError, DockerError, ConnectionError, OSError) as e:
                await ws.send_text(
                    "[rcon] " + tr("api_v1.rcon_error", detail=e)
                    + "\n[panel] " + tr("servers.ws_rcon_port_hint")
                )
                continue
            except Exception as e:
                # неожиданное — отдаём в WS чтобы увидеть, и шлём traceback в stdout сервера
                await ws.send_text("[rcon] " + tr(
                    "servers.ws_rcon_unexpected_error", type=type(e).__name__, detail=e,
                ))
                traceback.print_exc()
                continue
            # Factorio часто оканчивает ответ \n — снимем его, чтобы не плодить
            # пустую "[rcon]" строку в терминале. Если ответа нет вовсе (как у /admins
            # без админов) — покажем хотя бы маркер что команда отработала.
            lines = (resp or "").rstrip("\n").splitlines()
            if not lines:
                await ws.send_text("[rcon] " + tr("servers.ws_rcon_empty_response_line"))
            else:
                for line in lines:
                    await ws.send_text(f"[rcon] {line}")

    async def watch_authority():
        """Bound log-stream access independently of client input activity."""
        while True:
            await asyncio.sleep(SESSION_CHECK_INTERVAL_SEC)
            if not await asyncio.to_thread(_session_is_authorized, session_id, client_ip):
                await ws.close(code=1008)
                return

    out_task = asyncio.create_task(pump_outgoing())
    in_task = asyncio.create_task(pump_incoming())
    auth_task = asyncio.create_task(watch_authority())
    try:
        done, pending = await asyncio.wait(
            {out_task, in_task, auth_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for p in pending:
            p.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        out_task.cancel()
        in_task.cancel()
        auth_task.cancel()
        stop_flag.set()
        await asyncio.gather(out_task, in_task, auth_task, return_exceptions=True)
        await asyncio.to_thread(t.join, 2)
