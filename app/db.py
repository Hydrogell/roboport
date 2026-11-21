"""Минимальный слой работы с SQLite на stdlib.

Открываем соединение per-request через зависимость FastAPI. Для админ-панели на
одного пользователя этого хватит — без async-обвязок и ORM. Если упрёмся в
конкуренцию, перевернём на aiosqlite/SQLModel позже.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "panel.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    uuid          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,            -- e.g. "2.0.76"
    port          INTEGER NOT NULL,
    runtime       TEXT NOT NULL DEFAULT 'docker',  -- сейчас только 'docker'
    container_id  TEXT,                     -- docker container id (если runtime=docker)
    autostart     INTEGER NOT NULL DEFAULT 0,  -- 1 = поднимать на старте панели
    network_mode  TEXT NOT NULL DEFAULT 'bridge',  -- 'bridge' (NAT) | 'host' (LAN-видим)
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Образцы метрик контейнера. Sampler пишет каждые ~30 сек когда контейнер running.
CREATE TABLE IF NOT EXISTS metrics_samples (
    server_uuid   TEXT NOT NULL,
    ts            INTEGER NOT NULL,         -- unix-секунды
    cpu_pct       REAL NOT NULL,            -- 0..N*100 (sum по vCPU как docker stats)
    mem_bytes     INTEGER NOT NULL,         -- использованная память (RSS-like)
    mem_pct       REAL NOT NULL             -- от хостового лимита (как docker stats)
);
CREATE INDEX IF NOT EXISTS metrics_samples_idx ON metrics_samples(server_uuid, ts);

-- Игровые сессии: открываются когда ник появился в /players, закрываются когда
-- исчез (или сервер ушёл в stopped). left_at IS NULL = сессия открыта.
CREATE TABLE IF NOT EXISTS player_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    server_uuid   TEXT NOT NULL,
    name          TEXT NOT NULL,
    name_lc       TEXT NOT NULL,             -- lower-case для CI поиска
    joined_at     INTEGER NOT NULL,
    left_at       INTEGER                    -- NULL пока онлайн
);
CREATE INDEX IF NOT EXISTS player_sessions_open
    ON player_sessions(server_uuid, name_lc) WHERE left_at IS NULL;
CREATE INDEX IF NOT EXISTS player_sessions_idx
    ON player_sessions(server_uuid, joined_at);

-- Расписания: периодические задачи на сервер. Тикает sampler_loop раз в 30 сек.
-- schedule format:
--   'every:Nm' / 'every:Nh'           — каждые N минут/часов
--   'daily:HH:MM'                     — каждый день в HH:MM (локальное время)
--   'weekly:dow1,dow2,...:HH:MM'      — по дням недели (mon..sun) в HH:MM
-- kind: 'command' (RCON-команда из колонки command) | 'restart' (stop+start)
-- warn_seconds: для kind='restart' — за сколько секунд предупредить игроков через
--               /server-message перед stop+start. 0 = без анонса.
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    server_uuid   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    schedule      TEXT NOT NULL,
    command       TEXT,                       -- RCON-команда для kind='command'
    enabled       INTEGER NOT NULL DEFAULT 1,
    warn_seconds  INTEGER NOT NULL DEFAULT 0,
    last_run      INTEGER,
    last_status   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS scheduled_tasks_server_idx
    ON scheduled_tasks(server_uuid);

-- Аудит-лог панельных действий: что юзер нажал и что мы автоматом сделали.
-- kind — короткий тег ('server.start', 'server.delete', 'config.update', ...).
-- server_uuid может быть NULL для не-серверных событий.
-- summary — человекочитаемая строка для UI.
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    server_uuid   TEXT,
    summary       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_server_idx ON audit_log(server_uuid, ts DESC);

-- ===== Аутентификация =====
-- Один админ для соло-панели (роли не нужны). Хэш — scrypt со случайной солью,
-- формат строки: 'scrypt$N$r$p$<base64_salt>$<base64_hash>'.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    last_login_at   INTEGER,
    last_login_ip   TEXT,
    totp_secret     TEXT,                          -- base32 без padding, NULL пока 2FA не включена
    totp_enabled    INTEGER NOT NULL DEFAULT 0     -- 1 после успешного подтверждения первым кодом
);

-- One local installation code exists only until the first administrator is created.
CREATE TABLE IF NOT EXISTS setup_claim (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    digest TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);

-- Pending 2FA: после правильного password юзер получает временную «pending»
-- сессию, и до ввода TOTP-кода полноценная сессия не создаётся. TTL 5 мин.
CREATE TABLE IF NOT EXISTS pending_2fa (
    id              TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    ip              TEXT,
    user_agent      TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS pending_2fa_expires_idx ON pending_2fa(expires_at);

-- Backup-коды для 2FA — одноразовые, на случай потери authenticator-устройства.
-- Хранится только sha256, plain показывается ОДИН раз сразу после генерации
-- (на странице /settings/2fa/codes). Юзер копирует/скачивает.
-- При consume помечается used_at и больше не работает.
CREATE TABLE IF NOT EXISTS backup_codes (
    user_id         INTEGER NOT NULL,
    code_sha256     TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    used_at         INTEGER,
    used_from_ip    TEXT,
    PRIMARY KEY (user_id, code_sha256),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS backup_codes_user_idx ON backup_codes(user_id);

-- Сессии — серверные. id = secrets.token_urlsafe(32) (256 бит энтропии).
-- csrf_token живёт вместе с сессией (per-session, не per-form), валидируется
-- middleware на всех state-changing запросах (POST/PUT/DELETE).
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    ip              TEXT,
    user_agent      TEXT,
    csrf_token      TEXT NOT NULL,
    last_stepup_at  INTEGER NOT NULL DEFAULT 0,  -- unix-ts последнего step-up
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);

-- Неудачные попытки логина — для rate-limit и UI «5 неудач с IP X за час».
-- Чистится pruning'ом старше 24ч.
CREATE TABLE IF NOT EXISTS auth_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    ip              TEXT NOT NULL,
    username        TEXT,
    success         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS auth_attempts_ip_ts_idx ON auth_attempts(ip, ts DESC);
CREATE INDEX IF NOT EXISTS auth_attempts_ts_idx ON auth_attempts(ts DESC);

-- Anti-lockout (break-glass) recovery tokens. Хранится sha256 от plain-токена,
-- сам plain показывается ТОЛЬКО один раз — в выводе CLI. Single-use, 15 мин TTL.
-- Дает доступ ТОЛЬКО к /recovery/<token> (страница смены пароля), больше никуда.
CREATE TABLE IF NOT EXISTS recovery_tokens (
    token_sha256    TEXT PRIMARY KEY,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    used_at         INTEGER,
    used_for_user   TEXT,
    used_from_ip    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0  -- инкремент при wrong-submit, >=N = сгорел
);
CREATE INDEX IF NOT EXISTS recovery_tokens_expires_idx ON recovery_tokens(expires_at);

-- IP-баны. Срабатывает middleware на ВХОДЕ (до auth и rate-limit) — забаненный
-- IP не дойдёт даже до /login. banned_until=0 → permanent. auto=1 — авто-бан
-- от системы (превышение rate-limit), 0 — ручной (через CLI/UI).
CREATE TABLE IF NOT EXISTS ip_bans (
    ip              TEXT PRIMARY KEY,
    banned_at       INTEGER NOT NULL,
    banned_until    INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,
    auto            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ip_bans_until_idx ON ip_bans(banned_until);

-- ===== API-ключи (Phase 1) =====
-- Ключи для публичного API (/api/v1/*). Plain-key показывается ОДИН РАЗ при
-- создании, в БД хранится только sha256. Scopes — comma-separated ('read,command').
-- 4 уровня: read | command (RCON) | control (start/stop/save/schedule) | admin (CRUD/files).
-- Скопы независимы (нет иерархии «admin includes control»), ключ имеет конкретный список.
CREATE TABLE IF NOT EXISTS api_keys (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,                 -- человекочитаемая метка
    key_sha256          TEXT NOT NULL UNIQUE,          -- sha256(plain) hex
    scopes              TEXT NOT NULL,                 -- 'read' | 'read,command' | 'admin' | etc.
    created_at          INTEGER NOT NULL,
    expires_at          INTEGER,                       -- NULL = бессрочно
    last_used_at        INTEGER,
    last_used_ip        TEXT,
    revoked_at          INTEGER,                       -- NULL = активен
    owner_user_id       INTEGER NOT NULL,
    rate_limit_per_min  INTEGER NOT NULL DEFAULT 60,
    FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS api_keys_owner_idx ON api_keys(owner_user_id);

-- ===== Webhooks (Phase 4) =====
-- Push-события panel→bot. Когда что-то происходит (player.joined, server.crashed,
-- schedule.fired, ...), панель шлёт POST на webhooks.url с HMAC-SHA256 подписью.
-- events: '*' = все события, иначе comma-list ('player.joined,server.crashed').
-- secret: 32-байтная случайная строка для HMAC-подписи payload'а.
-- broken_at != NULL → доставка приостановлена после 3 подряд failure'ов;
-- юзер вручную «возобновляет» через UI/API после фикса URL.
CREATE TABLE IF NOT EXISTS webhooks (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT NOT NULL,
    url                     TEXT NOT NULL,
    events                  TEXT NOT NULL DEFAULT '*',
    secret                  TEXT NOT NULL,
    enabled                 INTEGER NOT NULL DEFAULT 1,
    created_at              INTEGER NOT NULL,
    last_delivery_at        INTEGER,
    last_delivery_status    INTEGER,                  -- HTTP статус последней доставки
    last_error              TEXT,                     -- текст ошибки если был
    broken_at               INTEGER,                  -- помечается после N подряд failure'ов
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    owner_user_id           INTEGER NOT NULL,
    FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS webhooks_owner_idx ON webhooks(owner_user_id);
CREATE INDEX IF NOT EXISTS webhooks_enabled_idx ON webhooks(enabled, broken_at);

-- Лента последних доставок (для UI «последние 10 deliveries»).
-- Чистится в _prune (старше 7 дней удаляются).
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id      INTEGER NOT NULL,
    delivery_id     TEXT NOT NULL,                    -- uuid; X-Roboport-Delivery-Id
    event           TEXT NOT NULL,
    ts              INTEGER NOT NULL,                 -- когда мы попытались
    status          INTEGER,                          -- HTTP code (или NULL при network-error)
    duration_ms     INTEGER,
    error           TEXT,                             -- текст ошибки если не 2xx
    attempt         INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (webhook_id) REFERENCES webhooks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS webhook_deliveries_wid_idx
    ON webhook_deliveries(webhook_id, ts DESC);

-- Полный access-log API: пишется middleware'ом по каждому /api/v1/* запросу
-- (включая anonymous-fail'ы — key_id NULL). Цель: видеть "кто что читал",
-- т.к. GET-запросы не пишутся в audit_log. Чистится >30 дней в _prune.
CREATE TABLE IF NOT EXISTS api_request_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,                 -- unix-секунды (start)
    key_id          INTEGER,                          -- NULL = анонимный (или auth провалился)
    method          TEXT NOT NULL,
    path            TEXT NOT NULL,                    -- request.url.path (без query)
    status          INTEGER NOT NULL,                 -- HTTP response status
    duration_ms     INTEGER NOT NULL,                 -- сколько обрабатывали
    ip              TEXT NOT NULL,
    user_agent      TEXT,                             -- может быть NULL
    FOREIGN KEY (key_id) REFERENCES api_keys(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS api_request_log_key_idx
    ON api_request_log(key_id, ts DESC);
CREATE INDEX IF NOT EXISTS api_request_log_ts_idx
    ON api_request_log(ts);

-- История self-update'ов панели. Каждая попытка апдейта (и rollback'а)
-- пишет строку. status='running' пока watchdog не отметит финал.
-- backup_dir указывает на директорию с прежними файлами для возможного
-- ручного rollback'а. Старые записи >90 дней чистятся в _prune.
CREATE TABLE IF NOT EXISTS panel_updates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER,                          -- NULL пока running
    from_version    TEXT,
    to_version      TEXT,
    source          TEXT NOT NULL,                    -- 'github:vX.Y.Z' | 'upload' | 'rollback'
    status          TEXT NOT NULL,                    -- 'running' | 'success' | 'failed' | 'rolled-back'
    error           TEXT,
    backup_dir      TEXT,                             -- путь к бэкапу (для rollback'а)
    initiated_by    TEXT,                             -- username | 'api:<key_name>'
    archive_sha256  TEXT                              -- sha256 архива (различает сборки с одинаковой VERSION)
);
CREATE INDEX IF NOT EXISTS panel_updates_ts_idx ON panel_updates(started_at DESC);
"""


def connect() -> sqlite3.Connection:
    # check_same_thread=False — у нас sync- и async-эндпоинты живут на разных
    # потоках, и FastAPI прогоняет sync-Depends через threadpool. Без этого
    # async-endpoint падает: "SQLite objects created in a thread can only be
    # used in that same thread". Пишем мы только из одного процесса, конфликтов
    # нет; для безопасности мог бы ещё isolation_level=None+lock, но не надо пока.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Миграции: добавляем колонки которые могли отсутствовать в старых БД.
        # SQLite не умеет ADD COLUMN IF NOT EXISTS, проверяем через PRAGMA.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(servers)")}
        if "autostart" not in cols:
            conn.execute("ALTER TABLE servers ADD COLUMN autostart INTEGER NOT NULL DEFAULT 0")
        if "network_mode" not in cols:
            conn.execute(
                "ALTER TABLE servers ADD COLUMN network_mode TEXT NOT NULL DEFAULT 'bridge'"
            )
        # scheduled_tasks: добавили колонку command + переименовали kind 'save' → 'command'
        sched_cols = {r["name"] for r in conn.execute("PRAGMA table_info(scheduled_tasks)")}
        if sched_cols and "command" not in sched_cols:
            conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN command TEXT")
            conn.execute(
                "UPDATE scheduled_tasks SET kind = 'command', command = '/server-save' "
                "WHERE kind = 'save'"
            )
        if sched_cols and "warn_seconds" not in sched_cols:
            conn.execute(
                "ALTER TABLE scheduled_tasks "
                "ADD COLUMN warn_seconds INTEGER NOT NULL DEFAULT 0"
            )
        # recovery_tokens: добавили attempts (счётчик failed-submit per-token)
        rt_cols = {r["name"] for r in conn.execute("PRAGMA table_info(recovery_tokens)")}
        if rt_cols and "attempts" not in rt_cols:
            conn.execute(
                "ALTER TABLE recovery_tokens "
                "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )
        # users: totp_secret/totp_enabled добавлены позже
        u_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if u_cols and "totp_secret" not in u_cols:
            conn.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
        if u_cols and "totp_enabled" not in u_cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0"
            )
        # sessions: last_stepup_at добавлено позже (для step-up auth)
        s_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        if s_cols and "last_stepup_at" not in s_cols:
            conn.execute(
                "ALTER TABLE sessions "
                "ADD COLUMN last_stepup_at INTEGER NOT NULL DEFAULT 0"
            )
        # panel_updates: archive_sha256 добавлено позже — отличаем разные
        # сборки с одинаковой VERSION (минорные хотфиксы без bump'а версии).
        pu_cols = {r["name"] for r in conn.execute("PRAGMA table_info(panel_updates)")}
        if pu_cols and "archive_sha256" not in pu_cols:
            conn.execute("ALTER TABLE panel_updates ADD COLUMN archive_sha256 TEXT")

        # UNIQUE-индексы на servers.name и servers.port. Раньше уникальность
        # держалась только в коде (4 SELECT-then-INSERT сайта), теперь и БД.
        # Если в БД уже есть дубликаты — CREATE UNIQUE INDEX упадёт; ловим это
        # заранее, не создаём индекс, печатаем actionable warning. Юзер чинит
        # руками (переименовывает / меняет порт), следующий рестарт зацепит.
        _migrate_unique_servers(conn)

        conn.commit()


def _migrate_unique_servers(conn: sqlite3.Connection) -> None:
    """Создать UNIQUE INDEX на servers.name и servers.port если их ещё нет
    и если данные позволяют (нет дубликатов). Идемпотентно."""
    existing_idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='servers'"
    )}
    for col, idx_name in (("name", "servers_name_uniq_idx"),
                          ("port", "servers_port_uniq_idx")):
        if idx_name in existing_idx:
            continue
        dups = conn.execute(
            f"SELECT {col}, COUNT(*) AS n FROM servers "
            f"GROUP BY {col} HAVING n > 1"
        ).fetchall()
        if dups:
            details = ", ".join(f"{r[col]!r} x{r['n']}" for r in dups)
            print(f"[migrate] cannot add UNIQUE index on servers.{col}: "
                  f"duplicates exist ({details}). Resolve them in UI/CLI, "
                  f"index will be added on next start.", flush=True)
            continue
        conn.execute(
            f"CREATE UNIQUE INDEX {idx_name} ON servers({col})"
        )
        print(f"[migrate] added UNIQUE INDEX {idx_name} on servers.{col}",
              flush=True)


def get_db():
    """FastAPI dependency: открывает соединение на запрос."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def audit(conn: sqlite3.Connection, kind: str,
          server_uuid: str | None = None, summary: str = "") -> None:
    """Записать событие в audit_log. Не падаем — лог не должен мешать работе.
    Вызывается из тех же роутов что меняют состояние; делать commit обязан caller
    (мы используем тот же connection что и для основной модификации).

    Side-effect: для определённых kind также эмитим webhook-событие
    (Phase 4 — push events panel→bot). Маппинг audit-kind → webhook-event
    в _AUDIT_TO_WEBHOOK ниже."""
    import time as _time
    try:
        conn.execute(
            "INSERT INTO audit_log (ts, kind, server_uuid, summary) VALUES (?, ?, ?, ?)",
            (int(_time.time()), kind, server_uuid, summary[:500]),
        )
    except sqlite3.Error as e:
        print(f"[audit] failed to log {kind!r}: {e}")

    # Webhook fan-out. Если kind есть в маппинге — кладём событие в очередь.
    # Lazy import чтобы избежать цикла db ↔ webhooks.
    wh_event = _AUDIT_TO_WEBHOOK.get(kind)
    if wh_event:
        try:
            from . import webhooks as _wh
            # Пытаемся достать имя сервера из servers (может пригодиться в payload).
            server_name = None
            if server_uuid:
                row = conn.execute(
                    "SELECT name FROM servers WHERE uuid = ?", (server_uuid,),
                ).fetchone()
                if row: server_name = row["name"]
            _wh.emit(wh_event, server_uuid=server_uuid, server_name=server_name,
                     audit_kind=kind, summary=summary)
        except Exception as e:
            # Не валим audit-write если webhooks упал.
            print(f"[audit] webhook emit failed for {kind!r}: {e}")


# Маппинг audit-kind → webhook-event. Не каждый audit-kind становится webhook-event;
# только бизнес-значимые (а не каждый login.ok / api_key.create).
# Для технических событий (auth.recovery.*, api_key.*, schedule.add) бот может
# опросить /api/v1/audit если нужно.
_AUDIT_TO_WEBHOOK = {
    "server.create":   "server.created",
    "server.start":    "server.started",
    "server.stop":     "server.stopped",
    "server.delete":   "server.deleted",
    "server.config":   "server.config.changed",
    "server.clone":    "server.cloned",
    "auth.login.fail": "auth.login.fail",
    "auth.ip.autobanned": "auth.ip.autobanned",
}
