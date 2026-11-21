"""Webhooks — push-события panel→bot.

Когда что-то значимое происходит (player.joined, server.crashed, и т.п.),
панель шлёт POST на URL подписчика с HMAC-SHA256 подписью.

## Архитектура
- `emit(event, server_uuid=None, **data)` — публичная функция, кладёт событие
  в ограниченную очередь в памяти. Не блокирует caller'а — синхронна, дёшева.
- `dispatcher_loop()` — фоновая asyncio-задача (стартует вместе с панелью).
  Берёт событие из очереди, фильтрует подписчиков (по events-list), шлёт
  POST'ы с retry-backoff. Все доставки логирует в `webhook_deliveries`.
- HTTP делается через stdlib `urllib.request` в `run_in_executor` (не таскаем
  `httpx`/`aiohttp` как dep — соответствует stdlib-стилю проекта).
- HMAC-подпись: `X-Roboport-Signature: sha256=<hex>` от raw body + secret.
- Idempotency: каждый event получает уникальный `delivery_id` (uuid). Бот
  может дедуплицировать ретраи по этому id.
- Retry: 3 попытки с задержками 1s/5s/30s. После 3-го фейла — `broken_at = now`
  для веб-хука, дальнейшая доставка НЕ происходит до явного «resume» от юзера.

## Что мы НЕ делаем
- Persistent queue. Очередь в памяти; на перезапуске процесса in-flight события
  теряются. Для соло-панели приемлемо.
- Гарантированный порядок при retry. Если 1-я попытка провалилась и retry
  планируется на 5s, новые события могут прийти раньше. Бот должен быть
  готов к этому (idempotency-id в помощь).
- Backpressure. Если бот висит и копится много событий — очередь растёт. На
  практике ограничена `MAX_QUEUE` (1000); при переполнении новые события
  дропаются с warning'ом. Соло-панель не должна генерить столько.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import queue
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
import uuid as uuid_lib
from dataclasses import dataclass, field
from typing import Any

from .db import connect
from .i18n import tr

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

WEBHOOK_SECRET_LEN = 32              # 32 байта = 43 base64url char ≈ 256 бит
WEBHOOK_NAME_MIN = 1
WEBHOOK_NAME_MAX = 64
WEBHOOK_URL_MAX = 500

# Все валидные события — для UI-чекбоксов и валидации `events`-поля при создании.
# Звёздочка `*` означает «подписан на все»; pa rsing handles это отдельно.
EVENTS_ALL = (
    # Lifecycle
    "server.created", "server.started", "server.stopped", "server.crashed",
    "server.deleted", "server.config.changed", "server.cloned",
    # Player presence
    "player.joined", "player.left",
    # Schedules
    "schedule.fired",
    # Auth events (security alerts)
    "auth.login.fail", "auth.ip.autobanned",
    # Webhook self-test (через UI кнопку «Тест»)
    "webhook.ping",
)

# Retry-backoff: задержки между попытками (после 1-й попытки, перед 2-й и 3-й).
RETRY_DELAYS_SEC = (1, 5, 30)
HTTP_TIMEOUT_SEC = 10
MAX_QUEUE_SIZE = 1000
# После стольких подряд failure'ов webhook помечается broken — рассылка
# прекращается до ручного resume через UI.
MAX_CONSECUTIVE_FAILURES = 3

# Очистка лога доставок старше N дней (вызывается из sampler._prune).
DELIVERIES_RETAIN_DAYS = 7


class WebhookError(Exception):
    pass


# ---------------------------------------------------------------------------
# Event и in-memory очередь
# ---------------------------------------------------------------------------

@dataclass
class WebhookEvent:
    """Одно событие. Конструируется emit()-функцией, кладётся в очередь."""
    event: str
    ts: int
    delivery_id: str
    server_uuid: str | None = None
    server_name: str | None = None
    data: dict = field(default_factory=dict)


# Producers include synchronous FastAPI handlers and the sampler's worker threads.
_events: queue.Queue[WebhookEvent] = queue.Queue(maxsize=MAX_QUEUE_SIZE)
_dispatcher_wakeup: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None


def emit(event: str, *, server_uuid: str | None = None,
         server_name: str | None = None, **data: Any) -> None:
    """Queue an event from any thread, including before the dispatcher has started."""
    if event not in EVENTS_ALL:
        # Не ломаем caller'а; просто игнорим неизвестное событие.
        print(f"[webhooks] unknown event {event!r}, ignored", flush=True)
        return
    ev = WebhookEvent(
        event=event,
        ts=int(time.time()),
        delivery_id=str(uuid_lib.uuid4()),
        server_uuid=server_uuid,
        server_name=server_name,
        data=dict(data),
    )
    try:
        _events.put_nowait(ev)
    except queue.Full:
        print(f"[webhooks] QUEUE FULL ({MAX_QUEUE_SIZE}), dropping {event!r}", flush=True)
        return
    wakeup = _dispatcher_wakeup
    if wakeup is not None:
        loop, ready = wakeup
        try:
            loop.call_soon_threadsafe(ready.set)
        except RuntimeError:
            # Shutdown may close the old loop just after this producer enqueued the event.
            if not loop.is_closed():
                raise


emit_threadsafe = emit


# ---------------------------------------------------------------------------
# HMAC sign
# ---------------------------------------------------------------------------

def sign_payload(secret: str, body: bytes) -> str:
    """sha256-HMAC, hex. Бот проверяет: hmac(secret, raw_body).hexdigest() == sig."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# CRUD helpers (используются API + UI)
# ---------------------------------------------------------------------------

def generate_secret() -> str:
    return secrets.token_urlsafe(WEBHOOK_SECRET_LEN)


def parse_events(raw: str) -> set[str]:
    """'*' → set(EVENTS_ALL). 'a,b,c' → {'a','b','c'} (только валидные)."""
    raw = (raw or "").strip()
    if not raw or raw == "*":
        return set(EVENTS_ALL)
    return {e.strip() for e in raw.split(",") if e.strip() in EVENTS_ALL}


def format_events(events: set[str]) -> str:
    """set → 'a,b,c' (sorted) или '*' если все."""
    if not events:
        return ""
    if events >= set(EVENTS_ALL):
        return "*"
    return ",".join(sorted(events))


def validate_url(url: str) -> str:
    """Базовая проверка URL. Только http/https, ограничение длины."""
    url = (url or "").strip()
    if not url:
        raise WebhookError(tr("webhooks.url_empty"))
    if len(url) > WEBHOOK_URL_MAX:
        raise WebhookError(tr("webhooks.url_too_long", limit=WEBHOOK_URL_MAX))
    if not (url.startswith("http://") or url.startswith("https://")):
        raise WebhookError(tr("webhooks.url_protocol"))
    return url


def create_webhook(conn: sqlite3.Connection, owner_user_id: int,
                   name: str, url: str, events: set[str]) -> tuple[int, str]:
    """Создать веб-хук. Возвращает (id, plain-secret).
    Secret хранится в БД в открытом виде — он используется обеими сторонами
    (мы для подписи, бот для верификации). Это не пароль юзера — это shared
    secret для канала. Caller обязан commit."""
    name = (name or "").strip()
    if not (WEBHOOK_NAME_MIN <= len(name) <= WEBHOOK_NAME_MAX):
        raise WebhookError(tr("webhooks.name_length", minimum=WEBHOOK_NAME_MIN, maximum=WEBHOOK_NAME_MAX))
    url = validate_url(url)
    if not events:
        raise WebhookError(tr("webhooks.events_required"))
    secret = generate_secret()
    cur = conn.execute(
        "INSERT INTO webhooks (name, url, events, secret, created_at, owner_user_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, url, format_events(events), secret, int(time.time()), owner_user_id),
    )
    return int(cur.lastrowid), secret


def list_webhooks(conn: sqlite3.Connection, owner_user_id: int) -> list[dict]:
    """Все хуки юзера. SECRET включается — он нужен в UI чтобы юзер мог
    переконфигурить бот. Plain-кей показывается напрямую (не sha256 как у API-keys)
    потому что shared, а не proof-of-identity."""
    return [dict(r) for r in conn.execute(
        "SELECT id, name, url, events, secret, enabled, created_at, "
        "       last_delivery_at, last_delivery_status, last_error, "
        "       broken_at, consecutive_failures "
        "FROM webhooks WHERE owner_user_id = ? ORDER BY created_at DESC",
        (owner_user_id,),
    ).fetchall()]


def get_webhook(conn: sqlite3.Connection, hook_id: int,
                owner_user_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM webhooks WHERE id = ? AND owner_user_id = ?",
        (hook_id, owner_user_id),
    ).fetchone()
    return dict(row) if row else None


def update_webhook(conn: sqlite3.Connection, hook_id: int, owner_user_id: int,
                   *, name: str | None = None, url: str | None = None,
                   events: set[str] | None = None,
                   enabled: bool | None = None) -> bool:
    fields, values = [], []
    if name is not None:
        n = name.strip()
        if not (WEBHOOK_NAME_MIN <= len(n) <= WEBHOOK_NAME_MAX):
            raise WebhookError(tr("webhooks.name_length", minimum=WEBHOOK_NAME_MIN, maximum=WEBHOOK_NAME_MAX))
        fields.append("name = ?"); values.append(n)
    if url is not None:
        fields.append("url = ?"); values.append(validate_url(url))
    if events is not None:
        if not events:
            raise WebhookError(tr("webhooks.events_required"))
        fields.append("events = ?"); values.append(format_events(events))
    if enabled is not None:
        fields.append("enabled = ?"); values.append(1 if enabled else 0)
    if not fields:
        return False
    values.extend([hook_id, owner_user_id])
    cur = conn.execute(
        f"UPDATE webhooks SET {', '.join(fields)} "
        f"WHERE id = ? AND owner_user_id = ?",
        values,
    )
    return cur.rowcount > 0


def delete_webhook(conn: sqlite3.Connection, hook_id: int,
                   owner_user_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM webhooks WHERE id = ? AND owner_user_id = ?",
        (hook_id, owner_user_id),
    )
    return cur.rowcount > 0


def resume_webhook(conn: sqlite3.Connection, hook_id: int,
                   owner_user_id: int) -> bool:
    """Снять broken_at — рассылка возобновляется. Юзер делает это после
    того как починил URL/IP бота."""
    cur = conn.execute(
        "UPDATE webhooks SET broken_at = NULL, consecutive_failures = 0 "
        "WHERE id = ? AND owner_user_id = ?",
        (hook_id, owner_user_id),
    )
    return cur.rowcount > 0


def list_recent_deliveries(conn: sqlite3.Connection, hook_id: int,
                           limit: int = 10) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, delivery_id, event, ts, status, duration_ms, error, attempt "
        "FROM webhook_deliveries WHERE webhook_id = ? "
        "ORDER BY ts DESC LIMIT ?",
        (hook_id, limit),
    ).fetchall()]


def cleanup_old_deliveries(conn: sqlite3.Connection,
                           retain_days: int = DELIVERIES_RETAIN_DAYS) -> int:
    cutoff = int(time.time()) - retain_days * 86400
    cur = conn.execute(
        "DELETE FROM webhook_deliveries WHERE ts < ?", (cutoff,),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Доставка одного хука — POST с retry
# ---------------------------------------------------------------------------

def _deliver_sync(url: str, body: bytes, headers: dict[str, str]) -> tuple[int | None, str | None, int]:
    """Синхронная доставка через urllib. Возвращает (status, error, duration_ms).
    status=None при network error; иначе HTTP-код."""
    t0 = time.time()
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            status = resp.status
            # Прочитаем чуть-чуть чтобы соединение завершилось аккуратно;
            # тело ответа боту нам не нужно.
            try: resp.read(1024)
            except Exception: pass
            duration_ms = int((time.time() - t0) * 1000)
            return status, None, duration_ms
    except urllib.error.HTTPError as e:
        # Не-2xx HTTP — это failure для нас (даже если бот вернул 4xx).
        duration_ms = int((time.time() - t0) * 1000)
        return e.code, f"HTTP {e.code} {e.reason}", duration_ms
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        duration_ms = int((time.time() - t0) * 1000)
        return None, f"{type(e).__name__}: {e}", duration_ms


async def _deliver_with_retry(hook: dict, ev: WebhookEvent) -> None:
    """Доставить event одному веб-хуку с retry-backoff. После
    MAX_CONSECUTIVE_FAILURES — помечаем broken_at, дальше пропускаем.
    Все попытки логируются в webhook_deliveries."""
    payload = {
        "delivery_id": ev.delivery_id,
        "event": ev.event,
        "ts": ev.ts,
        "server": ({"uuid": ev.server_uuid, "name": ev.server_name}
                   if ev.server_uuid else None),
        "data": ev.data,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sig = sign_payload(hook["secret"], body)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Roboport-Webhook/1",
        "X-Roboport-Delivery-Id": ev.delivery_id,
        "X-Roboport-Event": ev.event,
        "X-Roboport-Signature": f"sha256={sig}",
    }

    last_status: int | None = None
    last_error: str | None = None

    for attempt in range(1, len(RETRY_DELAYS_SEC) + 2):  # 1, 2, 3, 4 — но 4-я не делается
        # Доставка в треде чтобы не блокировать loop.
        status, error, duration_ms = await asyncio.to_thread(
            _deliver_sync, hook["url"], body, headers,
        )
        # Лог попытки
        try:
            with connect() as conn:
                conn.execute(
                    "INSERT INTO webhook_deliveries "
                    "(webhook_id, delivery_id, event, ts, status, duration_ms, error, attempt) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (hook["id"], ev.delivery_id, ev.event, int(time.time()),
                     status, duration_ms, error, attempt),
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[webhooks] failed to log delivery: {e}", flush=True)

        success = status is not None and 200 <= status < 300
        last_status, last_error = status, error
        if success:
            # Сбрасываем failure-счётчик и broken_at
            try:
                with connect() as conn:
                    conn.execute(
                        "UPDATE webhooks SET last_delivery_at = ?, "
                        "       last_delivery_status = ?, last_error = NULL, "
                        "       consecutive_failures = 0 "
                        "WHERE id = ?",
                        (int(time.time()), status, hook["id"]),
                    )
                    conn.commit()
            except sqlite3.Error:
                pass
            return

        # Не успех — если есть ещё retry-задержка, ждём и пробуем снова.
        # `attempt` начинается с 1; задержка перед следующей попыткой —
        # RETRY_DELAYS_SEC[attempt-1] (если есть).
        if attempt - 1 < len(RETRY_DELAYS_SEC):
            await asyncio.sleep(RETRY_DELAYS_SEC[attempt - 1])
            continue
        break  # все попытки исчерпаны

    # Все попытки провалились — обновляем счётчик failure'ов и, возможно, broken
    try:
        with connect() as conn:
            new_count = (hook.get("consecutive_failures") or 0) + 1
            broken_now = (new_count >= MAX_CONSECUTIVE_FAILURES)
            conn.execute(
                "UPDATE webhooks SET last_delivery_at = ?, "
                "       last_delivery_status = ?, last_error = ?, "
                "       consecutive_failures = ?, "
                "       broken_at = CASE WHEN ? = 1 AND broken_at IS NULL "
                "                        THEN ? ELSE broken_at END "
                "WHERE id = ?",
                (int(time.time()), last_status, last_error or "all retries failed",
                 new_count, 1 if broken_now else 0, int(time.time()), hook["id"]),
            )
            conn.commit()
    except sqlite3.Error as e:
        print(f"[webhooks] failed to update fail-counter: {e}", flush=True)
    if last_error:
        print(f"[webhooks] delivery failed: hook={hook['name']!r} "
              f"event={ev.event} → {last_error}", flush=True)


# ---------------------------------------------------------------------------
# Главный dispatcher loop
# ---------------------------------------------------------------------------

def _subscribers(event: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM webhooks WHERE enabled = 1 AND broken_at IS NULL "
            "AND (events = '*' OR events LIKE ?)",
            (f"%{event}%",),
        ).fetchall()
    return [dict(row) for row in rows if event in parse_events(row["events"])]


async def dispatcher_loop() -> None:
    """Deliver queued events concurrently and own each delivery until it finishes."""
    global _dispatcher_wakeup
    ready = asyncio.Event()
    _dispatcher_wakeup = (asyncio.get_running_loop(), ready)
    deliveries: set[asyncio.Task] = set()

    def finished(task: asyncio.Task) -> None:
        deliveries.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            print(f"[webhooks] delivery error: {exc!r}", flush=True)

    print("[webhooks] dispatcher started", flush=True)
    try:
        while True:
            try:
                event = _events.get_nowait()
            except queue.Empty:
                await ready.wait()
                ready.clear()
                continue
            try:
                for hook in _subscribers(event.event):
                    task = asyncio.create_task(_deliver_with_retry(hook, event))
                    deliveries.add(task)
                    task.add_done_callback(finished)
            except Exception as exc:
                print(f"[webhooks] dispatcher error on {event.event}: {exc!r}", flush=True)
            finally:
                _events.task_done()
            # A burst of producer events must also give active deliveries time to run.
            await asyncio.sleep(0)
    finally:
        _dispatcher_wakeup = None
        for task in deliveries:
            task.cancel()
        await asyncio.gather(*deliveries, return_exceptions=True)


# ---------------------------------------------------------------------------
# Test-ping (вызывается из UI «Тест»)
# ---------------------------------------------------------------------------

async def test_ping(hook: dict) -> dict:
    """Доставить webhook.ping этому конкретному хуку. Используется UI'ем
    чтобы юзер мог проверить URL без ожидания реального события.
    Возвращает диагностический dict."""
    ev = WebhookEvent(
        event="webhook.ping",
        ts=int(time.time()),
        delivery_id=str(uuid_lib.uuid4()),
        data={"message": "Тестовое событие из Roboport."},
    )
    payload = {
        "delivery_id": ev.delivery_id,
        "event": ev.event,
        "ts": ev.ts,
        "server": None,
        "data": ev.data,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sig = sign_payload(hook["secret"], body)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Roboport-Webhook/1",
        "X-Roboport-Delivery-Id": ev.delivery_id,
        "X-Roboport-Event": ev.event,
        "X-Roboport-Signature": f"sha256={sig}",
    }
    status, error, duration_ms = await asyncio.to_thread(
        _deliver_sync, hook["url"], body, headers,
    )
    success = status is not None and 200 <= status < 300
    # Лог в БД даже для теста
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO webhook_deliveries "
                "(webhook_id, delivery_id, event, ts, status, duration_ms, error, attempt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (hook["id"], ev.delivery_id, ev.event, int(time.time()),
                 status, duration_ms, error),
            )
            conn.commit()
    except sqlite3.Error:
        pass
    return {
        "ok": success, "status": status,
        "duration_ms": duration_ms, "error": error,
        "delivery_id": ev.delivery_id,
    }
