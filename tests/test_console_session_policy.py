"""Session lifetime policy for the privileged console WebSocket."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth, db
from app import main
from app.routes import server_console
from app.runtime import ServerSpec, ServerState, ServerStatus


def _session(tmp_path, monkeypatch):
    test_db = tmp_path / "panel.sqlite"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    with db.connect() as conn:
        user_id = auth.create_user(conn, "console-admin", "correct horse battery staple")
        session = auth.create_session(conn, user_id, ip="127.0.0.1")
        conn.commit()
    return session


def test_console_session_check_allows_active_unbanned_session(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch)

    assert server_console._session_is_authorized(session.id, "127.0.0.1") is True
    with db.connect() as conn:
        expires_at = conn.execute(
            "SELECT expires_at FROM sessions WHERE id = ?", (session.id,)
        ).fetchone()["expires_at"]
    assert expires_at == session.expires_at


@pytest.mark.parametrize("change", ["revoked", "banned", "expired"])
def test_console_session_check_denies_revoked_banned_or_expired_session(tmp_path, monkeypatch, change):
    session = _session(tmp_path, monkeypatch)
    with db.connect() as conn:
        if change == "revoked":
            auth.revoke_session(conn, session.id)
        elif change == "banned":
            auth.ban_ip(conn, "127.0.0.1", duration_sec=60)
        else:
            conn.execute("UPDATE sessions SET expires_at = 0 WHERE id = ?", (session.id,))
        conn.commit()

    assert server_console._session_is_authorized(session.id, "127.0.0.1") is False


def test_console_session_check_fails_closed_when_database_is_unavailable(monkeypatch):
    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(server_console, "connect", unavailable)

    assert server_console._session_is_authorized("still-looking-valid", "127.0.0.1") is False


def _signed_in_client(tmp_path, monkeypatch):
    test_db = tmp_path / "panel.sqlite"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    with db.connect() as conn:
        setup_code = auth.issue_setup_code(conn)
        conn.commit()
    client = TestClient(main.app, raise_server_exceptions=True)
    response = client.post("/setup", data={
        "username": "console-admin", "password": "correct horse battery staple",
        "password2": "correct horse battery staple", "setup_code": setup_code,
    }, follow_redirects=False)
    assert response.status_code == 303
    return client


def test_console_denies_command_when_session_was_revoked_after_open(tmp_path, monkeypatch):
    client = _signed_in_client(tmp_path, monkeypatch)
    spec = ServerSpec("console-test", "Console test", "2.0", 34197, tmp_path / "server")

    class Runtime:
        commands = []

        def status(self, _spec):
            return ServerStatus(ServerState.RUNNING, container_id="container")

        def stream_logs(self, _spec, stop_event):
            stop_event.wait(5)
            return
            yield  # pragma: no cover - makes this a generator

        def send_command(self, _spec, command):
            self.commands.append(command)
            return "should never run"

    runtime = Runtime()
    monkeypatch.setattr(server_console, "DockerRuntime", lambda: runtime)
    monkeypatch.setattr(server_console, "_spec_from_uuid", lambda _uuid: spec)

    session_id = client.cookies.get(auth.SESSION_COOKIE)
    with client.websocket_connect("/ws/servers/console-test/logs", headers={"Origin": "http://testserver"}) as socket:
        with db.connect() as conn:
            auth.revoke_session(conn, session_id)
            conn.commit()
        socket.send_text("/save")
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == 1008
    assert runtime.commands == []


def test_stopped_container_returns_snapshot_without_starting_follow_stream(tmp_path, monkeypatch):
    client = _signed_in_client(tmp_path, monkeypatch)
    spec = ServerSpec("stopped-test", "Stopped test", "2.0", 34197, tmp_path / "server")

    class Runtime:
        stream_started = False

        def status(self, _spec):
            return ServerStatus(ServerState.STOPPED, container_id="container")

        def logs(self, _spec, _tail):
            return "last line\n"

        def stream_logs(self, _spec, stop_event):
            self.stream_started = True
            return iter(())

    runtime = Runtime()
    monkeypatch.setattr(server_console, "DockerRuntime", lambda: runtime)
    monkeypatch.setattr(server_console, "_spec_from_uuid", lambda _uuid: spec)

    with client.websocket_connect("/ws/servers/stopped-test/logs", headers={"Origin": "http://testserver"}) as socket:
        assert socket.receive_text() == "last line"
        assert "stopped" in socket.receive_text()
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == 1000
    assert runtime.stream_started is False


def test_quiet_console_stream_is_periodically_revalidated_without_commands(monkeypatch):
    """A client which never sends must still lose access on the bounded timer."""
    from app.routes import auth_routes

    class Socket:
        headers = {}
        client = SimpleNamespace(host="127.0.0.1")

        def __init__(self):
            self.closed = []
            self.sent = []

        async def accept(self):
            pass

        async def close(self, code):
            self.closed.append(code)

        async def send_text(self, text):
            self.sent.append(text)

        async def receive_text(self):
            # Keep the inbound side busy: this must not postpone the watchdog.
            await asyncio.sleep(0)
            return ""

    class Runtime:
        stopped = False

        def status(self, _spec):
            return ServerStatus(ServerState.RUNNING, container_id="container")

        def stream_logs(self, _spec, stop_event):
            stop_event.wait(1)
            self.stopped = stop_event.is_set()
            return
            yield  # pragma: no cover - makes this a generator

    async def authenticated(_ws):
        return SimpleNamespace(id="session", username="console-admin")

    runtime = Runtime()
    socket = Socket()
    monkeypatch.setattr(auth_routes, "ws_auth_or_close", authenticated)
    monkeypatch.setattr(auth_routes, "client_ip", lambda _ws: "127.0.0.1")
    monkeypatch.setattr(server_console, "DockerRuntime", lambda: runtime)
    monkeypatch.setattr(server_console, "_spec_from_uuid", lambda _uuid: SimpleNamespace())
    monkeypatch.setattr(server_console, "_session_is_authorized", lambda *_args: False)
    monkeypatch.setattr(server_console, "SESSION_CHECK_INTERVAL_SEC", 0.01)

    asyncio.run(asyncio.wait_for(server_console.ws_logs(socket, "quiet"), timeout=0.5))

    assert socket.closed == [1008]
    assert runtime.stopped is True


def test_stopped_snapshot_is_not_sent_after_authority_is_lost(monkeypatch):
    """A slow snapshot may not cross an intervening logout or ban."""
    from app.routes import auth_routes

    class Socket:
        headers = {}
        client = SimpleNamespace(host="127.0.0.1")

        def __init__(self):
            self.closed = []
            self.sent = []

        async def accept(self):
            pass

        async def close(self, code):
            self.closed.append(code)

        async def send_text(self, text):
            self.sent.append(text)

    class Runtime:
        def status(self, _spec):
            return ServerStatus(ServerState.STOPPED, container_id="container")

        def logs(self, _spec, _tail):
            return "private previous log\n"

    async def authenticated(_ws):
        return SimpleNamespace(id="session", username="console-admin")

    socket = Socket()
    monkeypatch.setattr(auth_routes, "ws_auth_or_close", authenticated)
    monkeypatch.setattr(auth_routes, "client_ip", lambda _ws: "127.0.0.1")
    monkeypatch.setattr(server_console, "DockerRuntime", Runtime)
    monkeypatch.setattr(server_console, "_spec_from_uuid", lambda _uuid: SimpleNamespace())
    monkeypatch.setattr(server_console, "_session_is_authorized", lambda *_args: False)

    asyncio.run(server_console.ws_logs(socket, "stopped"))

    assert socket.sent == []
    assert socket.closed == [1008]
