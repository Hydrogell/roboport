"""Регрессии для независимых форм настроек и возврата на нужную вкладку."""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import auth, db, main
from app.routes import settings


def test_invalid_audit_page_size_uses_default(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()
    with db.connect() as conn:
        _, filters = settings._collect_audit({"audit_per": "all"}, conn)
    assert filters["per"] == 50


@pytest.mark.parametrize(("zone", "expected"), [
    ("+3", "2026-11-21 15:00:06 UTC+03:00"),
    ("-13", "2026-11-20 23:00:06 UTC-13:00"),
])
def test_login_history_and_audit_show_epoch_in_panel_timezone(monkeypatch, zone, expected):
    monkeypatch.setenv("PANEL_TZ", zone)
    stamp = 1795262406
    attempt = {"ts": stamp, "ip": "192.0.2.1", "username": "demo"}
    templates = main.app.state.templates
    security = templates.get_template("settings/security.html").render(
        current_user={"username": "demo"},
        security={"successful_logins": [attempt], "failed_attempts": [attempt]},
    )
    audit = templates.get_template("_partials/audit_table.html").render(
        audit_rows=[{"ts": stamp, "kind": "auth.login"}],
        audit_filter={"total": 1, "page": 1, "pages": 1},
    )
    assert security.count(expected) == 2
    assert expected in audit
    assert str(stamp) not in security + audit


def test_timezone_section_only_updates_panel_timezone():
    updates = settings._env_updates_for_section(
        "timezone", username="ignored", token="secret", clear_token="1",
        panel_tz="Europe/Moscow",
    )

    assert updates == {"PANEL_TZ": "Europe/Moscow"}


def test_factorio_section_keeps_token_when_token_field_is_blank():
    updates = settings._env_updates_for_section(
        "factorio", username="new-name", token="", clear_token="",
        panel_tz="Europe/Moscow",
    )

    assert updates == {"FACTORIO_USERNAME": "new-name"}


def test_factorio_section_clears_token_only_after_explicit_request():
    updates = settings._env_updates_for_section(
        "factorio", username="", token="", clear_token="1", panel_tz="",
    )

    assert updates == {"FACTORIO_USERNAME": "", "FACTORIO_TOKEN": ""}


def test_invalid_timezone_section_raises_without_any_updates():
    with pytest.raises(ValueError, match="Часовой пояс"):
        settings._env_updates_for_section(
            "timezone", username="", token="", clear_token="", panel_tz="Mars/Olympus",
        )


def test_unknown_section_is_rejected_instead_of_using_legacy_save():
    with pytest.raises(ValueError, match="Неизвестный раздел"):
        settings._env_updates_for_section(
            "typo", username="would-clear", token="", clear_token="",
            panel_tz="",
        )


def test_settings_redirect_preserves_security_anchor_after_error():
    response = settings._redirect(err="Не задан IP", anchor="security")

    assert response.headers["location"].startswith("/settings?err=")
    assert response.headers["location"].endswith("#security")


def test_settings_env_forms_work_through_auth_and_csrf(tmp_path, monkeypatch):
    """Smoke: реальные setup/logout/login/CSRF и изолированный .env/SQLite."""
    test_db = tmp_path / "panel.sqlite"
    test_env = tmp_path / ".env"
    test_env.write_text(
        "UNRELATED=keep\nFACTORIO_USERNAME=old\nFACTORIO_TOKEN=old-token\nPANEL_TZ=UTC\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(settings, "DB_PATH", test_db)
    monkeypatch.setattr(settings, "ENV_PATH", test_env)
    monkeypatch.setenv("FACTORIO_USERNAME", "old")
    monkeypatch.setenv("FACTORIO_TOKEN", "old-token")
    monkeypatch.setenv("PANEL_TZ", "UTC")
    db.init_db()

    # Без context manager TestClient не запускает lifespan-фоновые sampler/webhook
    # задачи; тестирует только HTTP ASGI и не затрагивает Docker/игровые серверы.
    client = TestClient(main.app, raise_server_exceptions=True)
    password = "correct horse battery staple"
    with db.connect() as conn:
        setup_code = auth.issue_setup_code(conn)
    assert client.post("/setup", data={
        "username": "smoke-admin", "password": password, "password2": password, "setup_code": setup_code,
    }, follow_redirects=False).status_code == 303

    page = client.get("/settings")
    assert page.status_code == 200
    csrf = page.text.split('name="_csrf" value="', 1)[1].split('"', 1)[0]

    # Обычный logout/login подтверждает, что дальнейшие POST идут через middleware.
    assert client.post("/logout", data={"_csrf": csrf}, follow_redirects=False).status_code == 303
    assert client.post("/login", data={
        "username": "smoke-admin", "password": password, "next": "/",
    }, follow_redirects=False).status_code == 303
    page = client.get("/settings")
    csrf = page.text.split('name="_csrf" value="', 1)[1].split('"', 1)[0]

    response = client.post("/settings/env", data={
        "_csrf": csrf, "section": "timezone", "panel_tz": "Europe/Moscow",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query)["msg"] == ["Часовой пояс сохранён."]
    assert "FACTORIO_TOKEN=old-token" in test_env.read_text(encoding="utf-8")

    response = client.post("/settings/env", data={
        "_csrf": csrf, "section": "factorio", "username": "demo", "token": "new-token",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert "#" not in response.headers["location"]
    assert "FACTORIO_TOKEN=new-token" in test_env.read_text(encoding="utf-8")

    client.post("/settings/env", data={
        "_csrf": csrf, "section": "factorio", "username": "renamed", "token": "",
    }, follow_redirects=False)
    env_after_blank_token = test_env.read_text(encoding="utf-8")
    assert "FACTORIO_USERNAME=renamed" in env_after_blank_token
    assert "FACTORIO_TOKEN=new-token" in env_after_blank_token

    client.post("/settings/env", data={
        "_csrf": csrf, "section": "factorio", "username": "renamed", "clear_token": "1",
    }, follow_redirects=False)
    assert "FACTORIO_TOKEN=\n" in test_env.read_text(encoding="utf-8")

    # Старый единый payload по-прежнему сохраняет все его поля.
    client.post("/settings/env", data={
        "_csrf": csrf, "username": "legacy", "token": "legacy-token", "panel_tz": "UTC",
    }, follow_redirects=False)
    before_invalid_tz = test_env.read_text(encoding="utf-8")
    assert "FACTORIO_USERNAME=legacy\n" in before_invalid_tz
    assert "FACTORIO_TOKEN=legacy-token\n" in before_invalid_tz
    assert "PANEL_TZ=UTC\n" in before_invalid_tz
    invalid = client.post("/settings/env", data={
        "_csrf": csrf, "section": "timezone", "panel_tz": "Mars/Olympus",
    }, follow_redirects=False)
    assert invalid.status_code == 303
    assert "err=" in invalid.headers["location"]
    assert test_env.read_text(encoding="utf-8") == before_invalid_tz

    unknown = client.post("/settings/env", data={
        "_csrf": csrf, "section": "typo", "username": "would-clear", "panel_tz": "",
    }, follow_redirects=False)
    assert unknown.status_code == 303
    assert parse_qs(urlsplit(unknown.headers["location"]).query)["err"] == ["Неизвестный раздел настроек."]
    assert test_env.read_text(encoding="utf-8") == before_invalid_tz

    same_tab_error = client.post("/change-password", data={
        "_csrf": csrf, "current_password": "wrong", "new_password": password,
        "new_password2": password,
    }, follow_redirects=False)
    assert same_tab_error.status_code == 303
    assert same_tab_error.headers["location"].endswith("#security")
