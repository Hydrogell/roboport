import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from app import db, i18n, main


def test_language_negotiation_and_regional_variants():
    assert i18n.choose_language("ru", "en-US") == "ru"
    assert i18n.choose_language("unknown", "ga, fr-CA;q=0.9, de;q=0.5") == "fr"
    assert i18n.choose_language("", "zh-Hant-HK, en;q=0.7") == "zh-TW"
    assert i18n.choose_language("", "es-MX") == "es-419"
    assert i18n.choose_language("", "pt-PT") == "pt-PT"
    assert i18n.choose_language("", "nb-NO") == "no"
    assert i18n.choose_language("", "ga-IE") == "en"
    assert i18n.choose_language("", "") == "ru"


def test_translations_are_isolated_across_concurrent_requests():
    async def read(code):
        with i18n.use_language(code):
            await asyncio.sleep(0)
            return i18n.tr("locale.language")

    async def run():
        return await asyncio.gather(read("en"), read("ru"))

    assert asyncio.run(run()) == ["Language", "Язык"]
    assert i18n.tr("locale.language") == "Язык"


def test_catalogs_are_complete():
    assert len(i18n.languages()) >= 30
    assert i18n.check_catalogs() == []


def test_application_references_have_messages():
    root = Path(main.__file__).parent
    keys = i18n.languages()["en"].messages
    translation = re.compile(r"""\b(?:t|tr|rpT|rpTHtml)\(\s*["']([a-z_][a-z.0-9_-]*)["']""")
    for path in [*root.rglob("*.py"), *(root / "templates").rglob("*.html"), *(root / "static/js").glob("*.js")]:
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".html":
            source = re.sub(r"\{#.*?#\}|<!--.*?-->", "", source, flags=re.S)
        assert not (set(translation.findall(source)) - keys.keys()), path


def test_plural_rules_and_english_fallback(monkeypatch):
    with i18n.use_language("ru"):
        assert i18n.tr("settings.audit_count", count=1) == "1 запись"
        assert i18n.tr("settings.audit_count", count=22) == "22 записи"
        assert i18n.tr("settings.audit_count", count=25) == "25 записей"
        monkeypatch.delitem(i18n.languages()["ru"].messages, "topbar.signed_in_as")
        expected = i18n.languages()["en"].messages["topbar.signed_in_as"]
        username = "Alice {username} & Bob"
        assert i18n.tr("topbar.signed_in_as", username=username) == expected.replace("{username}", username)


def test_past_and_future_session_times_in_every_language():
    now = datetime.now(timezone.utc)
    for code in i18n.languages():
        with i18n.use_language(code):
            for seconds in (-61, 3, 3600, 86400 * 30):
                stamp = (now + timedelta(seconds=seconds)).isoformat()
                assert 'class="ht-rel muted small">' in main.humantime(stamp), (code, seconds)


def test_login_language_choice_persists_and_can_be_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()
    client = TestClient(main.app, headers={"Accept-Language": "en-CA"})
    setup = client.get("/setup")
    assert setup.status_code == 200 and setup.headers["Content-Language"] == "en"
    assert '<html lang="en"' in setup.text and 'action="/language"' in setup.text
    assert i18n.languages()["en"].messages["login.create_admin"] in setup.text
    changed = client.post("/language", data={"code": "ru", "next": "/setup?example=1#account"}, follow_redirects=False)
    assert changed.status_code == 303 and changed.headers["location"] == "/setup?example=1#account"
    assert client.get("/setup").headers["Content-Language"] == "ru"
    browser = client.get("/locales/ru/client.js")
    assert browser.status_code == 200
    messages = json.loads(browser.text.removeprefix("window.roboportMessages=").removesuffix(";"))
    assert messages["locale.language"] == "Язык"
    client.post("/language", data={"code": "auto", "next": "/setup"}, follow_redirects=False)
    assert client.get("/setup").headers["Content-Language"] == "en"


def test_form_validation_localizes_html_and_preserves_json(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()
    client = TestClient(main.app, headers={"Accept-Language": "fr"})
    html = client.post("/setup", data={"username": "sample"}, headers={"Accept": "text/html"})
    assert html.status_code == 422
    assert html.headers["Content-Language"] == "fr"
    assert i18n.languages()["fr"].messages["error.form_title"] in html.text
    structured = client.post("/setup", data={"username": "sample"}, headers={"Accept": "application/json"})
    assert structured.status_code == 422 and isinstance(structured.json()["detail"], list)
