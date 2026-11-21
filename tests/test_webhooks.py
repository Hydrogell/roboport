"""Тесты для app/webhooks.py: HMAC-подпись, парсер событий, валидатор URL."""
import asyncio
import hmac
import hashlib

import pytest

from app import db, webhooks


# ---------------------------------------------------------------------------
# sign_payload — HMAC-SHA256, hex
# ---------------------------------------------------------------------------

class TestSignPayload:
    def test_matches_stdlib_hmac(self):
        # Эталонная реализация: hmac(secret, body, sha256).hexdigest().
        # Если sign_payload расходится — webhook-консьюмеры будут отвергать.
        secret = "shared-secret-xyz"
        body = b'{"event":"server.started"}'
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert webhooks.sign_payload(secret, body) == expected

    def test_returns_hex_64_chars(self):
        sig = webhooks.sign_payload("k", b"x")
        assert len(sig) == 64
        assert all(c in "0123456789abcdef" for c in sig)

    def test_different_secret_different_sig(self):
        body = b'{"event":"x"}'
        s1 = webhooks.sign_payload("secret-a", body)
        s2 = webhooks.sign_payload("secret-b", body)
        assert s1 != s2

    def test_different_body_different_sig(self):
        secret = "k"
        s1 = webhooks.sign_payload(secret, b"a")
        s2 = webhooks.sign_payload(secret, b"b")
        assert s1 != s2

    def test_empty_body_signs_clean(self):
        # Один из ретраев может прийти с body=b"" (пинг). Не должно падать.
        sig = webhooks.sign_payload("k", b"")
        assert len(sig) == 64

    def test_unicode_body_handled(self):
        # Кириллица в payload — не должно ломать utf-8 encode
        sig = webhooks.sign_payload("ключ", "{\"name\":\"тест\"}".encode("utf-8"))
        assert len(sig) == 64


# ---------------------------------------------------------------------------
# parse_events / format_events — '*' и CSV
# ---------------------------------------------------------------------------

class TestEventsParser:
    def test_star_returns_all(self):
        # Wildcard — подписан на всё
        result = webhooks.parse_events("*")
        assert result == set(webhooks.EVENTS_ALL)

    def test_empty_returns_all(self):
        # Пустая строка трактуется как «всё» (исторический default)
        assert webhooks.parse_events("") == set(webhooks.EVENTS_ALL)
        assert webhooks.parse_events(None) == set(webhooks.EVENTS_ALL)

    def test_csv_filters_to_known(self):
        # Один валидный + один битый → только валидный, без exception
        first_valid = webhooks.EVENTS_ALL[0]
        result = webhooks.parse_events(f"{first_valid},garbage.event")
        assert result == {first_valid}

    def test_format_all_returns_star(self):
        # Если подписан на всё — компактно представляем как '*'
        assert webhooks.format_events(set(webhooks.EVENTS_ALL)) == "*"

    def test_format_subset_returns_sorted_csv(self):
        # Несколько событий — сортируем для детерминизма
        ev = set(webhooks.EVENTS_ALL[:2])
        formatted = webhooks.format_events(ev)
        assert formatted == ",".join(sorted(ev))

    def test_format_empty(self):
        assert webhooks.format_events(set()) == ""


# ---------------------------------------------------------------------------
# validate_url — http(s)://, лимит длины
# ---------------------------------------------------------------------------

class TestValidateUrl:
    def test_https_accepted(self):
        url = "https://example.com/hook"
        assert webhooks.validate_url(url) == url

    def test_http_accepted(self):
        # http тоже допустим — для приватных ботов в LAN
        url = "http://192.168.1.10:5000/webhook"
        assert webhooks.validate_url(url) == url

    def test_strips_whitespace(self):
        assert webhooks.validate_url("  https://e.com/h  ") == "https://e.com/h"

    @pytest.mark.parametrize("bad", [
        "",
        "ftp://example.com/",
        "javascript:alert(1)",
        "//example.com/no-scheme",
        "example.com/no-scheme",
    ])
    def test_invalid_schemes_rejected(self, bad):
        with pytest.raises(webhooks.WebhookError):
            webhooks.validate_url(bad)

    def test_too_long_rejected(self):
        with pytest.raises(webhooks.WebhookError):
            webhooks.validate_url("https://example.com/" + "x" * 10000)


# ---------------------------------------------------------------------------
# generate_secret — длина и уникальность
# ---------------------------------------------------------------------------

class TestGenerateSecret:
    def test_unique_each_call(self):
        secrets_set = {webhooks.generate_secret() for _ in range(50)}
        assert len(secrets_set) == 50

    def test_non_empty(self):
        s = webhooks.generate_secret()
        assert s
        # token_urlsafe(N) даёт примерно ceil(N*4/3) символов
        assert len(s) >= 20


def test_worker_notifications_and_inflight_deliveries_follow_dispatcher_lifetime(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel.sqlite")
    db.init_db()
    with db.connect() as conn:
        owner = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES ('owner', 'fixture', 0)"
        ).lastrowid
        webhooks.create_webhook(conn, owner, "test subscriber", "https://example.com/hook", {"webhook.ping"})

    async def exercise():
        observed = asyncio.Queue()
        finished = []

        async def capture(hook, event):
            tag = event.data["tag"]
            observed.put_nowait((hook["name"], tag))
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(tag)

        monkeypatch.setattr(webhooks, "_deliver_with_retry", capture)
        worker = asyncio.create_task(webhooks.dispatcher_loop())
        try:
            await asyncio.sleep(0)
            for tag, emit in (("compat", webhooks.emit_threadsafe), ("worker", webhooks.emit)):
                await asyncio.to_thread(emit, "webhook.ping", tag=tag)
                assert await asyncio.wait_for(observed.get(), timeout=3) == ("test subscriber", tag)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert sorted(finished) == ["compat", "worker"]

    asyncio.run(exercise(), debug=True)
