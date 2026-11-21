from contextlib import closing
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import auth, cli, db, main
from app.routes import auth_routes


USERNAME = "operator"
PASSWORD = "correct panel password"


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    monkeypatch.setattr(auth_routes, "_recovery_request_state", {})
    monkeypatch.setattr(auth_routes, "_recovery_submit_state", {})
    db.init_db()
    with closing(db.connect()) as conn, conn:
        uid = auth.create_user(conn, USERNAME, PASSWORD)
        auth.set_user_totp(conn, uid, auth.generate_totp_secret(), enabled=True)
        codes = auth.regenerate_backup_codes(conn, uid)
    with closing(TestClient(main.app, follow_redirects=False)) as client:
        yield client, uid, codes


def destination(response):
    assert response.status_code == 303
    return urlsplit(response.headers["location"]).path


def test_forgot_password_issues_one_time_code_and_keeps_second_factor(account, capsys):
    client, uid, _ = account
    with closing(db.connect()) as conn, conn:
        previous = auth.create_session(conn, uid)
        pending = auth.create_pending_2fa(conn, uid)

    assert 'action="/recovery/request"' in client.get("/login").text
    issued = client.post("/recovery/request")
    assert destination(issued) == "/recovery"
    issued_code = re.search(r"(?m)^\s*CODE\s*:\s*([A-Z0-9]{4}-[A-Z0-9]{4})\s*$", capsys.readouterr().out)
    assert issued_code is not None
    code = issued_code[1]
    form = client.get("/recovery")
    assert 'name="new_password"' in form.text
    assert code not in issued.headers["location"] and code not in form.text
    new_password = "new panel password"
    changed = client.post("/recovery", data={
        "code": code,
        "new_password": new_password, "new_password2": new_password,
    })
    assert destination(changed) == "/login"
    with closing(db.connect()) as conn:
        assert auth.find_active_recovery_token(conn, code) is None
        assert auth.load_session(conn, previous.id) is None
        assert auth.load_pending_2fa(conn, pending.id) is None
        user = auth.get_user_by_id(conn, uid)
        assert not auth.verify_password(PASSWORD, user["password_hash"])
    assert destination(client.post("/login", data={"username": USERNAME, "password": new_password})) == "/login/2fa"
    assert destination(client.post("/login/2fa", data={"code": auth.totp_now(user["totp_secret"])})) == "/"


def test_recovery_with_two_accounts_requires_explicit_username(account):
    client, uid, _ = account
    with closing(db.connect()) as conn, conn:
        other = auth.create_user(conn, "second-admin", PASSWORD)
        code = auth.create_recovery_token(conn)
    fields = {"code": code, "new_password": "new sample password", "new_password2": "new sample password"}
    assert destination(client.post("/recovery", data=fields)) == "/recovery"
    assert destination(client.post("/recovery", data={**fields, "username": USERNAME})) == "/login"
    with closing(db.connect()) as conn:
        assert auth.verify_password(fields["new_password"], auth.get_user_by_id(conn, uid)["password_hash"])
        assert auth.verify_password(PASSWORD, auth.get_user_by_id(conn, other)["password_hash"])


class CodeField(HTMLParser):
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "code":
            self.attrs = attrs


def assert_backup_code_fits(response, code):
    assert response.status_code == 200
    field = CodeField()
    field.feed(response.text)
    attrs = field.attrs
    assert attrs["autocomplete"] == "one-time-code"
    assert attrs.get("inputmode", "text") == "text"
    assert int(attrs.get("maxlength", len(code))) >= len(code)
    if "pattern" in attrs:
        assert re.fullmatch(attrs["pattern"], code)


def test_backup_codes_can_be_entered_at_login_and_password_confirmation(account):
    client, uid, codes = account
    assert destination(client.post("/login", data={"username": USERNAME, "password": PASSWORD})) == "/login/2fa"
    assert_backup_code_fits(client.get("/login/2fa"), codes[0])
    assert destination(client.post("/login/2fa", data={"code": codes[0]})) == "/"
    with closing(db.connect()) as conn, conn:
        session = auth.load_session(conn, client.cookies[auth.SESSION_COOKIE])
        conn.execute("UPDATE sessions SET last_stepup_at = 0 WHERE id = ?", (session.id,))
    assert_backup_code_fits(client.get("/stepup?next=/settings"), codes[1])
    confirmed = client.post("/stepup", data={
        "password": PASSWORD, "code": codes[1], "next": "/settings", "_csrf": session.csrf_token,
    })
    assert destination(confirmed) == "/settings"
    with closing(db.connect()) as conn:
        assert auth.count_unused_backup_codes(conn, uid) == len(codes) - 2


def test_console_reset_with_lost_authenticator_and_no_backup_codes(account):
    client, uid, _ = account
    with closing(db.connect()) as conn, conn:
        previous = auth.create_session(conn, uid)
        other = auth.create_user(conn, "other-admin", PASSWORD)
        auth.set_user_totp(conn, other, auth.generate_totp_secret(), enabled=True)
        other_codes = auth.regenerate_backup_codes(conn, other)
    assert destination(client.post("/login", data={"username": USERNAME, "password": PASSWORD})) == "/login/2fa"
    pending = client.cookies[auth.PENDING_2FA_COOKIE]

    assert cli.main(["disable-2fa", USERNAME]) == 0
    with closing(db.connect()) as conn:
        user = auth.get_user_by_id(conn, uid)
        assert not user["totp_enabled"] and user["totp_secret"] is None
        assert auth.load_session(conn, previous.id) is None
        assert not auth.has_any_backup_codes(conn, uid)
        assert auth.load_pending_2fa(conn, pending) is None
        assert auth.get_user_by_id(conn, other)["totp_enabled"]
        assert auth.count_unused_backup_codes(conn, other) == len(other_codes)
    assert destination(client.post("/login", data={"username": USERNAME, "password": PASSWORD})) == "/"
    assert auth.SESSION_COOKIE in client.cookies


def test_console_reset_cleans_up_factors_disabled_by_older_versions(account):
    _, uid, _ = account
    with closing(db.connect()) as conn, conn:
        pending = auth.create_pending_2fa(conn, uid).id
        conn.execute("UPDATE users SET totp_secret = NULL, totp_enabled = 0 WHERE id = ?", (uid,))
        current = auth.create_session(conn, uid)

    assert cli.main(["disable-2fa", USERNAME]) == 0
    with closing(db.connect()) as conn:
        assert not auth.has_any_backup_codes(conn, uid)
        assert auth.load_pending_2fa(conn, pending) is None
        assert auth.load_session(conn, current.id) is not None
