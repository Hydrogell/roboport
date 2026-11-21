"""Регрессии разбиения HTML-действий интеграций по независимым роутерам."""

import pytest
from fastapi.testclient import TestClient

from app import auth, db, main
from app import webhooks as webhook_service


@pytest.fixture
def signed_in_client(tmp_path, monkeypatch):
    """Реальная cookie-сессия, без дорогого setup/scrypt для route-регрессий."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()
    with db.connect() as conn:
        user_id = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("integrations-admin", "fixture", 0),
        ).lastrowid
        session = auth.create_session(conn, user_id)
        conn.commit()
    client = TestClient(main.app, raise_server_exceptions=True)
    client.cookies.set(auth.SESSION_COOKIE, session.id)
    return client, session.csrf_token


def test_api_key_forms_keep_authenticated_one_time_lifecycle(signed_in_client):
    """Вынесенный router сохраняет CSRF, одноразовый показ, audit и PRG."""
    client, csrf = signed_in_client

    missing_scope = client.post("/settings/api-keys/new", data={
        "_csrf": csrf, "name": "invalid bot",
    }, follow_redirects=False)
    assert missing_scope.status_code == 303
    assert "err=" in missing_scope.headers["location"]
    assert missing_scope.headers["location"].endswith("#integrations")

    create_key = client.post("/settings/api-keys/new", data={
        "_csrf": csrf, "name": "release bot", "scope_read": "on",
    }, follow_redirects=False)
    assert create_key.status_code == 303
    key_show_path = create_key.headers["location"]
    key_id = int(key_show_path.split("/")[-2])
    assert client.get(key_show_path).status_code == 200
    assert client.get(key_show_path, follow_redirects=False).status_code == 303
    assert client.get(f"/settings/api-keys/{key_id}/requests").status_code == 200

    revoke = client.post(f"/settings/api-keys/{key_id}/revoke", data={
        "_csrf": csrf,
    }, follow_redirects=False)
    assert revoke.status_code == 303
    assert "msg=" in revoke.headers["location"]
    assert revoke.headers["location"].endswith("#integrations")
    delete = client.post(f"/settings/api-keys/{key_id}/delete", data={
        "_csrf": csrf,
    }, follow_redirects=False)
    assert delete.status_code == 303
    assert "msg=" in delete.headers["location"]
    assert delete.headers["location"].endswith("#integrations")
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM api_keys WHERE id = ?", (key_id,)).fetchone() is None
        audit_kinds = [row["kind"] for row in conn.execute(
            "SELECT kind FROM audit_log WHERE kind LIKE 'api_key.%' ORDER BY id"
        )]
    assert audit_kinds == ["api_key.create", "api_key.revoke", "api_key.purge"]


def test_webhook_forms_keep_authenticated_lifecycle(signed_in_client, monkeypatch):
    """Вынесенный router сохраняет CSRF, resume, audit и PRG-ответы."""
    client, csrf = signed_in_client
    pinged = []

    async def fake_test_ping(hook):
        pinged.append(hook["id"])
        return {"ok": True, "status": 204, "duration_ms": 7}

    monkeypatch.setattr(webhook_service, "test_ping", fake_test_ping)

    create_hook = client.post("/settings/webhooks/new", data={
        "_csrf": csrf, "name": "release receiver", "url": "https://example.test/hook",
        "events": "webhook.ping",
    }, follow_redirects=False)
    assert create_hook.status_code == 303
    assert "msg=" in create_hook.headers["location"]
    assert create_hook.headers["location"].endswith("#integrations")
    with db.connect() as conn:
        hook_id = conn.execute("SELECT id FROM webhooks WHERE name = ?", ("release receiver",)).fetchone()["id"]

    assert client.post(f"/settings/webhooks/{hook_id}/toggle", data={
        "_csrf": csrf, "enabled": "0",
    }, follow_redirects=False).status_code == 303
    with db.connect() as conn:
        assert conn.execute("SELECT enabled FROM webhooks WHERE id = ?", (hook_id,)).fetchone()["enabled"] == 0
        conn.execute("UPDATE webhooks SET broken_at = 1 WHERE id = ?", (hook_id,))
        conn.commit()

    resume = client.post(f"/settings/webhooks/{hook_id}/resume", data={
        "_csrf": csrf,
    }, follow_redirects=False)
    assert resume.status_code == 303
    assert "msg=" in resume.headers["location"]
    assert resume.headers["location"].endswith("#integrations")
    with db.connect() as conn:
        assert conn.execute("SELECT broken_at FROM webhooks WHERE id = ?", (hook_id,)).fetchone()["broken_at"] is None
    ping = client.post(f"/settings/webhooks/{hook_id}/test", data={
        "_csrf": csrf,
    }, follow_redirects=False)
    assert ping.status_code == 303
    assert pinged == [hook_id]

    delete = client.post(f"/settings/webhooks/{hook_id}/delete", data={
        "_csrf": csrf,
    }, follow_redirects=False)
    assert delete.status_code == 303
    assert "msg=" in delete.headers["location"]
    assert delete.headers["location"].endswith("#integrations")
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM webhooks WHERE id = ?", (hook_id,)).fetchone() is None
        audit_kinds = [row["kind"] for row in conn.execute(
            "SELECT kind FROM audit_log WHERE kind LIKE 'webhook.%' ORDER BY id"
        )]
    assert audit_kinds == [
        "webhook.create", "webhook.toggle", "webhook.resume", "webhook.delete",
    ]
