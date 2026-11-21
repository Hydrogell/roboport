from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import auth, db, main


USERNAME_PATH = "/settings/account/username"
ACCOUNT_PAGE = "/settings#account-name"
PASSWORD = "correct horse battery staple"
RENAME_EVENT = "auth.username.change"


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()
    with db.connect() as conn:
        user_id = auth.create_user(conn, "panel-admin", PASSWORD)
        session = auth.create_session(conn, user_id)
    client = TestClient(main.app, headers={"Accept-Language": "en"})
    client.cookies.set(auth.SESSION_COOKIE, session.id)
    return client, session


def _submit(client, session, username):
    return client.post(
        USERNAME_PATH,
        data={"new_username": username, "_csrf": session.csrf_token},
        follow_redirects=False,
    )


def _flash(response, key, message):
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/settings"
    assert location.fragment == "account-name"
    assert parse_qs(location.query) == {key: [message]}


def _audit_summaries(conn):
    return [
        row["summary"] for row in conn.execute(
            "SELECT summary FROM audit_log WHERE kind = ? ORDER BY id", (RENAME_EVENT,),
        )
    ]


def test_rename_normalizes_username_and_preserves_identity(account):
    client, session = account
    secret = auth.generate_totp_secret()
    with db.connect() as conn:
        auth.set_user_totp(conn, session.user_id, secret, enabled=True)
        second_session = auth.create_session(conn, session.user_id)
        key_id, _ = auth.create_api_key(conn, session.user_id, "example bot", {"read"})
        db.audit(conn, "auth.login.ok", summary="panel-admin from 127.0.0.1")
        before = auth.get_user_by_id(conn, session.user_id)

    for value in ("  Renamed.Admin  ", "RENAMED.ADMIN"):
        response = _submit(client, session, value)
        _flash(response, "msg", "Use renamed.admin to sign in from now on.")

    assert client.cookies.get(auth.SESSION_COOKIE) == session.id
    with db.connect() as conn:
        assert auth.get_user_by_username(conn, "panel-admin") is None
        assert auth.get_user_by_username(conn, "renamed.admin") == {**before, "username": "renamed.admin"}
        for old_session in (session, second_session):
            now = auth.load_session(conn, old_session.id)
            assert now.user_id == old_session.user_id
            assert now.username == "renamed.admin"
            assert now.csrf_token == old_session.csrf_token
        assert auth.get_api_key_by_id(conn, key_id, session.user_id)["name"] == "example bot"
        assert conn.execute(
            "SELECT summary FROM audit_log WHERE kind = 'auth.login.ok'",
        ).fetchone()["summary"] == "panel-admin from 127.0.0.1"
        [summary] = _audit_summaries(conn)
        assert "panel-admin" in summary and "renamed.admin" in summary


def test_rename_takes_effect_for_new_sign_ins(account):
    client, session = account
    _flash(_submit(client, session, "new-name"), "msg", "Use new-name to sign in from now on.")
    signed_out = TestClient(main.app, headers={"Accept-Language": "en"})
    old_login = signed_out.post(
        "/login", data={"username": "panel-admin", "password": PASSWORD}, follow_redirects=False,
    )
    assert old_login.status_code == 303
    assert urlsplit(old_login.headers["location"]).path == "/login"
    assert signed_out.cookies.get(auth.SESSION_COOKIE) is None

    new_login = signed_out.post(
        "/login", data={"username": "new-name", "password": PASSWORD}, follow_redirects=False,
    )
    assert new_login.status_code == 303
    assert new_login.headers["location"] == "/"
    with db.connect() as conn:
        assert auth.load_session(conn, signed_out.cookies[auth.SESSION_COOKIE]).user_id == session.user_id


def test_expired_stepup_returns_to_username_form_without_replaying(account):
    client, session = account
    with db.connect() as conn:
        conn.execute("UPDATE sessions SET last_stepup_at = 0 WHERE id = ?", (session.id,))

    response = _submit(client, session, "other-name")
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/stepup"
    assert parse_qs(location.query) == {"next": [ACCOUNT_PAGE]}

    stepup = client.post(
        "/stepup",
        data={"password": PASSWORD, "_csrf": session.csrf_token, "next": ACCOUNT_PAGE},
        follow_redirects=False,
    )
    assert stepup.status_code == 303
    assert stepup.headers["location"] == ACCOUNT_PAGE
    with db.connect() as conn:
        assert auth.get_user_by_id(conn, session.user_id)["username"] == "panel-admin"
        assert _audit_summaries(conn) == []

    _flash(_submit(client, session, "other-name"), "msg", "Use other-name to sign in from now on.")


@pytest.mark.parametrize(("username", "expected"), [
    ("", "username length 3..32"),
    ("ab", "username length 3..32"),
    ("invalid/name", "Username: only a-z, 0-9, _, . and - are allowed"),
    ("  ALREADY-TAKEN ", "This username is already in use."),
])
def test_invalid_or_taken_username_leaves_account_unchanged(account, username, expected):
    client, session = account
    with db.connect() as conn:
        other_user_id = auth.create_user(conn, "already-taken", PASSWORD)
    response = _submit(client, session, username)
    _flash(response, "err", expected)
    with db.connect() as conn:
        assert auth.get_user_by_id(conn, session.user_id)["username"] == "panel-admin"
        assert auth.get_user_by_id(conn, other_user_id)["username"] == "already-taken"
        assert _audit_summaries(conn) == []


def test_rename_uses_existing_session_csrf_check(account):
    client, session = account
    response = client.post(
        USERNAME_PATH,
        data={"new_username": "other-name"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "csrf_invalid"
    with db.connect() as conn:
        assert auth.get_user_by_id(conn, session.user_id)["username"] == "panel-admin"
        assert _audit_summaries(conn) == []
