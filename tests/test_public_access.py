import asyncio
from contextlib import closing
from urllib.parse import urlsplit

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth, cli, db, http_errors, main, templating
from app.routes import auth_routes
from app.routes.api.auth import require_api_key


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()


def sample_request(path="/", accept="text/html"):
    return Request({"type": "http", "method": "GET", "scheme": "http", "path": path,
                    "query_string": b"", "app": main.app, "router": main.app.router,
                    "client": ("192.0.2.5", 5050),
                    "server": ("testserver", 80), "headers": [(b"accept", accept.encode())]})


def test_guest_layout_needs_no_server_list(monkeypatch):
    def unexpected_connection():
        pytest.fail("Guest template context should not query game servers")

    monkeypatch.setattr(templating, "connect", unexpected_connection)
    assert templating._request_context(sample_request())["sidebar_servers"] == []


@pytest.mark.parametrize("accept", ["text/html", "application/json"])
def test_internal_error_details_stay_in_server_logs(accept, capsys, database):
    detail = "private diagnostic marker"
    try:
        raise RuntimeError(detail)
    except RuntimeError as exc:
        response = asyncio.run(http_errors.panel_unhandled(sample_request(accept=accept), exc))
    assert response.status_code == 500
    assert detail in capsys.readouterr().out
    assert detail.encode() not in response.body
    assert b"Traceback" not in response.body
    if accept == "text/html":
        assert b'href="/login"' in response.body


def test_only_service_metadata_is_public_in_api():
    public = {"/api/v1/health", "/api/v1/panel/version"}
    key_guard = require_api_key("read").__code__

    def has_key_guard(node):
        return (getattr(node.call, "__code__", None) is key_guard
                or any(has_key_guard(child) for child in node.dependencies))

    for route in main.app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/"):
            assert has_key_guard(route.dependant) == (route.path not in public), route.path


def test_public_status_omits_installation_details(database, monkeypatch):
    monkeypatch.setenv("PANEL_UPDATE_REPO", "example/private-fixture")
    with closing(db.connect()) as conn, conn:
        owner = auth.create_user(conn, "sample-operator", "test operator password")
        session = auth.create_session(conn, owner)
    with closing(TestClient(main.app, follow_redirects=False)) as client:
        assert set(client.get("/health").json()) == {"status", "ts"}
        assert set(client.get("/api/v1/panel/version").json()) == {"version"}
        assert client.get("/health/sampler", headers={"Accept": "application/json"}).status_code == 401
        client.cookies.set(auth.SESSION_COOKIE, session.id)
        sampler = client.get("/health/sampler")
        assert sampler.status_code == 200
        assert {"ts", "sampler_alive", "last_sample_ts"} <= set(sampler.json())
        assert "no-store" in sampler.headers["cache-control"]


def test_guest_recovery_and_error_pages_keep_accounts_and_servers_private(database):
    with closing(db.connect()) as conn, conn:
        auth.create_user(conn, "sample-private-operator", "test operator password")
        conn.execute("INSERT INTO servers(uuid, name, version, port) VALUES (?, ?, ?, ?)",
                     ("fixture-world-id", "Fixture world", "2.0", 34197))
    with closing(TestClient(main.app)) as client:
        page = client.get("/recovery")
        error = client.get("/setup", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert error.status_code == 404 and 'href="/login"' in error.text
    for response in (page, error):
        for value in ("sample-private-operator", "fixture-world-id", "Fixture world"):
            assert value not in response.text
    assert 'name="username"' in page.text and 'autocomplete="username"' in page.text


def test_first_admin_uses_current_local_setup_code(database):
    fields = {"username": "sample-owner", "password": "first owner password", "password2": "first owner password"}
    with closing(db.connect()) as conn, conn:
        first = auth.issue_setup_code(conn)
        current = auth.issue_setup_code(conn)
    assert first and current and first != current
    with closing(TestClient(main.app, follow_redirects=False)) as client:
        for supplied in ("", first):
            denied = client.post("/setup", data={**fields, "setup_code": supplied})
            assert urlsplit(denied.headers["location"]).path == "/setup"
            assert auth.SESSION_COOKIE not in client.cookies
        mismatch = client.post("/setup", data={**fields, "setup_code": current, "password2": "different"})
        assert urlsplit(mismatch.headers["location"]).path == "/setup"
        created = client.post("/setup", data={**fields, "setup_code": current})
        assert created.status_code == 303 and created.headers["location"] == "/"
        assert auth.SESSION_COOKIE in client.cookies
        assert client.get("/setup").status_code == 404
    with closing(db.connect()) as conn, conn:
        assert auth.count_users(conn) == 1 and auth.issue_setup_code(conn) is None


def test_setup_code_expires(database, monkeypatch):
    with closing(db.connect()) as conn, conn:
        code = auth.issue_setup_code(conn)
        after_expiry = auth.time.time() + 601
        monkeypatch.setattr(auth.time, "time", lambda: after_expiry)
        assert not auth.consume_setup_code(conn, code)


def test_local_cli_issues_a_usable_setup_code(database, capsys):
    assert cli.main(["setup-code"]) == 0
    code = capsys.readouterr().out.strip().split()[-1]
    with closing(db.connect()) as conn, conn:
        assert auth.consume_setup_code(conn, code)
        assert not auth.consume_setup_code(conn, code)


def test_connection_identity_uses_server_normalized_scope():
    req = sample_request()
    req.scope["headers"].extend([(b"x-forwarded-for", b"198.51.100.11"), (b"x-forwarded-proto", b"https")])
    assert auth_routes.client_ip(req) == req.client.host
    assert not auth_routes._is_https(req)
