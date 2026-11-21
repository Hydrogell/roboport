"""HTTP-регрессии CSRF для обычных действий панели.

Все пути к данным подменяются временными: TestClient запускается без lifespan,
поэтому sampler, Docker и webhook-dispatcher не стартуют.
"""

import pytest
from fastapi.testclient import TestClient

from app import auth, db, factorio_versions, panel_update
from app.routes import settings
from app import main


def _signed_in_client(tmp_path, monkeypatch):
    test_db = tmp_path / "panel.sqlite"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(settings, "DB_PATH", test_db)
    monkeypatch.setattr(panel_update, "UPLOADS_DIR", tmp_path / ".uploads")
    db.init_db()

    client = TestClient(main.app, raise_server_exceptions=True)
    password = "correct horse battery staple"
    with db.connect() as conn:
        setup_code = auth.issue_setup_code(conn)
    response = client.post("/setup", data={
        "username": "csrf-admin", "password": password, "password2": password, "setup_code": setup_code,
    }, follow_redirects=False)
    assert response.status_code == 303

    session_id = client.cookies.get(auth.SESSION_COOKIE)
    with db.connect() as conn:
        session = auth.load_session(conn, session_id)
        assert session is not None
        user = auth.get_user_by_id(conn, session.user_id)
        conn.commit()
    return client, session.csrf_token, user


def test_signed_out_settings_requests_redirect_to_login(tmp_path, monkeypatch):
    client, _, _ = _signed_in_client(tmp_path, monkeypatch)
    client.cookies.clear()

    get_response = client.get("/settings", follow_redirects=False)
    post_response = client.post("/settings/clear-versions-cache", follow_redirects=False)

    assert get_response.status_code == 303
    assert get_response.headers["location"] == "/login?next=/settings"
    assert post_response.status_code == 303
    assert post_response.headers["location"] == "/login?next=/settings/clear-versions-cache"


def test_authenticated_multipart_upload_accepts_form_csrf_into_temp_uploads(tmp_path, monkeypatch):
    client, csrf, _ = _signed_in_client(tmp_path, monkeypatch)

    response = client.post(
        "/settings/panel-update/upload",
        data={"_csrf": csrf},
        files={"file": ("demo.zip", b"safe demo bytes", "application/zip")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("#updates")
    assert (panel_update.UPLOADS_DIR / "demo.zip").read_bytes() == b"safe demo bytes"


def test_authenticated_header_csrf_allows_javascript_style_post(tmp_path, monkeypatch):
    client, csrf, _ = _signed_in_client(tmp_path, monkeypatch)
    monkeypatch.setattr(factorio_versions, "_fetch_dockerhub_tags", lambda: ["2.0.1"])
    factorio_versions.clear_cache()
    assert factorio_versions.available_versions() == ["2.0.1"]

    response = client.post(
        "/settings/clear-versions-cache",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert factorio_versions.cache_info() == (0, None)


@pytest.mark.parametrize("form", [{}, {"_csrf": "expired-test-token"}])
def test_settings_reject_invalid_csrf_before_saving(tmp_path, monkeypatch, form):
    client, _, _ = _signed_in_client(tmp_path, monkeypatch)
    file = tmp_path / ".env"
    file.write_text("PANEL_TZ=+3\n", encoding="utf-8")
    monkeypatch.setattr(settings, "ENV_PATH", file)
    response = client.post("/settings/env", data={**form, "section": "timezone", "panel_tz": "+2"},
                           headers={"Accept": "application/json"}, follow_redirects=False)
    assert response.status_code == 403
    assert response.json()["error"] == "csrf_invalid"
    assert file.read_text(encoding="utf-8") == "PANEL_TZ=+3\n"


def test_read_only_api_key_authentication_works_without_csrf(tmp_path, monkeypatch):
    client, _, user = _signed_in_client(tmp_path, monkeypatch)
    with db.connect() as conn:
        _, plain = auth.create_api_key(
            conn, owner_user_id=user["id"], name="smoke-read", scopes={"read"},
        )
        conn.commit()

    no_key = client.get("/api/v1/servers")
    response = client.get("/api/v1/servers", headers={"X-API-Key": plain})

    assert no_key.status_code == 401
    assert response.status_code == 200
    assert response.json() == {"servers": [], "count": 0}


def test_authenticated_log_socket_keeps_its_own_handshake(tmp_path, monkeypatch):
    client, _, _ = _signed_in_client(tmp_path, monkeypatch)
    # An empty test DB exercises the socket without starting a game or attaching logs.
    with client.websocket_connect("/ws/servers/missing/logs", headers={"Origin": "http://testserver"}) as socket:
        assert socket.receive_text() == "[panel] Сервер не найден"
