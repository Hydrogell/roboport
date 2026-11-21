"""Аутентификация панели: хэш паролей, сессии, CSRF, helpers.

stdlib-only:
  - hashlib.scrypt    — хэш паролей (memory-hard, GPU-стойкий)
  - secrets           — криптостойкие случайные значения (session id, csrf, salt)
  - hmac.compare_digest — constant-time сравнение (защита от timing-атак)

Формат хэша пароля (PHC-style, наш собственный):
    scrypt$N$r$p$<base64_salt>$<base64_hash>
Параметры по умолчанию (RFC 7914 / OWASP 2023): N=2**15, r=8, p=1.
На современном железе ~50ms на хэш — приемлемо для интерактивного логина,
дорого для брутфорса.

Сессии хранятся в БД (таблица sessions). ID — token_urlsafe(32) = 256 бит энтропии.
CSRF-токен per-session: один на всю сессию, не per-form. Достаточно для CSRF
(злоумышленник не может узнать токен из-за SameSite=Strict и отсутствия CORS).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from .i18n import tr

# ---------- Параметры scrypt ----------
SCRYPT_N = 2 ** 15      # 32768 — cost factor (CPU/memory)
SCRYPT_R = 8            # block size
SCRYPT_P = 1            # parallelism
SCRYPT_KEYLEN = 32      # output 256 бит
SCRYPT_SALT_LEN = 16    # 128 бит соли
# Память для scrypt: 128 * N * r * p байт ≈ 32 МБ при дефолтных параметрах.
# OpenSSL default maxmem = 32 МБ, что ровно на грани и иногда падает
# 'memory limit exceeded'. Даём с запасом.
SCRYPT_MAXMEM = 64 * 1024 * 1024

# ---------- Параметры сессии ----------
SESSION_COOKIE = "roboport_session"
SESSION_TTL_SEC = 24 * 3600            # абсолют: умирает через 24ч
SESSION_SLIDING_RENEW_SEC = 60 * 30    # last_seen обновляем не чаще раз в 30 мин
SESSION_ID_BYTES = 32                  # 32 байта = 43 символа token_urlsafe = 256 бит
CSRF_TOKEN_BYTES = 32

# ---------- Лимиты пароля ----------
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 256
USERNAME_MIN_LEN = 3
USERNAME_MAX_LEN = 32


class AuthError(Exception):
    """Базовая ошибка аутентификации."""


# =============================================================================
# Пароли: хэш и проверка
# =============================================================================

def hash_password(plain: str) -> str:
    """Сгенерировать новый хэш пароля. Случайная соль, scrypt, encode в строку."""
    if not isinstance(plain, str):
        raise AuthError(tr("auth.password_type_invalid"))
    if not (PASSWORD_MIN_LEN <= len(plain) <= PASSWORD_MAX_LEN):
        raise AuthError(tr("auth.password_length", minimum=PASSWORD_MIN_LEN, maximum=PASSWORD_MAX_LEN))
    salt = secrets.token_bytes(SCRYPT_SALT_LEN)
    h = hashlib.scrypt(
        plain.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_KEYLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return (
        f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(h).decode('ascii')}"
    )


def verify_password(plain: str, stored: str) -> bool:
    """Constant-time проверка пароля против сохранённого хэша.
    Возвращает False (а не raise) при любых проблемах с форматом."""
    if not plain or not stored:
        return False
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError, base64.binascii.Error):
        return False
    try:
        actual = hashlib.scrypt(
            plain.encode("utf-8"), salt=salt,
            n=n, r=r, p=p, dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# =============================================================================
# Пользователи (CRUD)
# =============================================================================

def count_users(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def issue_setup_code(conn: sqlite3.Connection) -> str | None:
    """Issue from the host CLI only. The caller commits the transaction."""
    code = secrets.token_urlsafe(24)
    cur = conn.execute(
        "INSERT OR REPLACE INTO setup_claim (id, digest, expires_at) "
        "SELECT 1, ?, ? WHERE NOT EXISTS (SELECT 1 FROM users)",
        (hashlib.sha256(code.encode("ascii")).hexdigest(), int(time.time()) + 600),
    )
    return code if cur.rowcount == 1 else None


def consume_setup_code(conn: sqlite3.Connection, code: str) -> bool:
    """The caller creates the owner in the same transaction as this deletion."""
    if len(code) != 32 or not code.isascii():
        return False
    cur = conn.execute(
        "DELETE FROM setup_claim WHERE id = 1 AND digest = ? AND expires_at > ? "
        "AND NOT EXISTS (SELECT 1 FROM users)",
        (hashlib.sha256(code.encode("ascii")).hexdigest(), int(time.time())),
    )
    return cur.rowcount == 1


_USER_COLS = ("id, username, password_hash, created_at, last_login_at, "
              "last_login_ip, totp_secret, totp_enabled")


def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[dict]:
    row = conn.execute(
        f"SELECT {_USER_COLS} FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    return dict(row) if row else None


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> Optional[dict]:
    row = conn.execute(
        f"SELECT {_USER_COLS} FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def normalize_username(username: str) -> str:
    """Привести логин к каноническому виду и проверить его формат."""
    username = (username or "").strip().lower()
    if not (USERNAME_MIN_LEN <= len(username) <= USERNAME_MAX_LEN):
        raise AuthError(tr("auth.username_length", minimum=USERNAME_MIN_LEN, maximum=USERNAME_MAX_LEN))
    # Только латиница/цифры/_/. Простой URL-safe-friendly набор.
    import re
    if not re.match(r"^[a-z0-9_.-]+$", username):
        raise AuthError(tr("auth.username_invalid_characters"))
    return username


def create_user(conn: sqlite3.Connection, username: str, password: str) -> int:
    """Создать пользователя. Возвращает новый id. Caller обязан commit'нуть."""
    username = normalize_username(username)
    pw_hash = hash_password(password)
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, pw_hash, int(time.time())),
    )
    return int(cur.lastrowid)


def update_password(conn: sqlite3.Connection, user_id: int, new_password: str) -> None:
    pw_hash = hash_password(new_password)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))
    conn.execute("DELETE FROM pending_2fa WHERE user_id = ?", (user_id,))


def mark_user_logged_in(conn: sqlite3.Connection, user_id: int, ip: str) -> None:
    conn.execute(
        "UPDATE users SET last_login_at = ?, last_login_ip = ? WHERE id = ?",
        (int(time.time()), ip[:64], user_id),
    )


# =============================================================================
# Сессии
# =============================================================================

@dataclass
class SessionInfo:
    id: str
    user_id: int
    username: str
    expires_at: int
    csrf_token: str
    created_at: int
    last_seen_at: int
    last_stepup_at: int = 0


def create_session(conn: sqlite3.Connection, user_id: int,
                   ip: str = "", user_agent: str = "") -> SessionInfo:
    """Создать новую сессию. last_stepup_at = now: только что прошедший логин
    считаем свежим step-up'ом (юзер только что ввёл пароль/TOTP), 10-минутное
    окно дёргать заново не имеет смысла."""
    sid = secrets.token_urlsafe(SESSION_ID_BYTES)
    csrf = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
    now = int(time.time())
    expires = now + SESSION_TTL_SEC
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at, expires_at, last_seen_at, "
        "ip, user_agent, csrf_token, last_stepup_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, user_id, now, expires, now, ip[:64], (user_agent or "")[:256],
         csrf, now),
    )
    user = get_user_by_id(conn, user_id)
    return SessionInfo(
        id=sid, user_id=user_id, username=user["username"] if user else "?",
        expires_at=expires, csrf_token=csrf,
        created_at=now, last_seen_at=now, last_stepup_at=now,
    )


def load_session(conn: sqlite3.Connection, session_id: str) -> Optional[SessionInfo]:
    """Найти сессию по id, проверить что не истекла. Заодно сдвинуть last_seen
    (sliding expiration), но не чаще раз в SESSION_SLIDING_RENEW_SEC чтобы
    не тыркать БД на каждом запросе."""
    if not session_id or len(session_id) > 200:
        return None
    row = conn.execute(
        "SELECT s.id, s.user_id, s.created_at, s.expires_at, s.last_seen_at, "
        "       s.csrf_token, s.last_stepup_at, u.username "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if not row:
        return None
    now = int(time.time())
    if row["expires_at"] < now:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return None
    if (now - row["last_seen_at"]) > SESSION_SLIDING_RENEW_SEC:
        conn.execute(
            "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
            (now, now + SESSION_TTL_SEC, session_id),
        )
        new_expires = now + SESSION_TTL_SEC
    else:
        new_expires = row["expires_at"]
    return SessionInfo(
        id=row["id"], user_id=row["user_id"], username=row["username"],
        expires_at=new_expires, csrf_token=row["csrf_token"],
        created_at=row["created_at"], last_seen_at=now,
        last_stepup_at=row["last_stepup_at"] or 0,
    )


# =============================================================================
# Step-up authentication: повторное подтверждение для критичных операций
# =============================================================================
# Удалить сервер / сменить пароль / выключить 2FA / перегенерить backup-коды —
# для таких операций session-cookie мало (она могла быть угнана). Просим юзера
# подтвердить TOTP-кодом (если 2FA on) либо паролем (если off). Подтверждение
# даёт окно freshness (10 мин), в которое можно делать другие критичные дела.

SESSION_STEPUP_FRESHNESS_SEC = 10 * 60


def is_stepup_fresh(session: SessionInfo) -> bool:
    if not session or not session.last_stepup_at:
        return False
    return (int(time.time()) - session.last_stepup_at) <= SESSION_STEPUP_FRESHNESS_SEC


def mark_stepup(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET last_stepup_at = ? WHERE id = ?",
        (int(time.time()), session_id),
    )


def revoke_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def revoke_user_sessions(conn: sqlite3.Connection, user_id: int,
                         except_session_id: str | None = None) -> int:
    """Снести все сессии пользователя. Если except_session_id указан — оставить её
    (полезно для «выйти со всех других устройств»). Возвращает количество удалённых."""
    if except_session_id:
        cur = conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND id != ?",
            (user_id, except_session_id),
        )
    else:
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return cur.rowcount or 0


def list_user_sessions(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, created_at, last_seen_at, expires_at, ip, user_agent "
        "FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC",
        (user_id,),
    ).fetchall()]


def cleanup_expired_sessions(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
    return cur.rowcount or 0


# =============================================================================
# Auth attempts (для rate limit и UI)
# =============================================================================

def record_attempt(conn: sqlite3.Connection, ip: str, username: str | None,
                   success: bool) -> None:
    conn.execute(
        "INSERT INTO auth_attempts (ts, ip, username, success) VALUES (?, ?, ?, ?)",
        (int(time.time()), ip[:64], (username or "")[:64] or None, 1 if success else 0),
    )


def count_recent_failures(conn: sqlite3.Connection, ip: str,
                          window_sec: int = 900) -> int:
    """Сколько неудачных попыток с этого IP за последние window_sec секунд."""
    since = int(time.time()) - window_sec
    return int(conn.execute(
        "SELECT COUNT(*) FROM auth_attempts "
        "WHERE ip = ? AND success = 0 AND ts >= ?",
        (ip[:64], since),
    ).fetchone()[0])


def cleanup_old_attempts(conn: sqlite3.Connection, retain_sec: int = 86400) -> int:
    cur = conn.execute(
        "DELETE FROM auth_attempts WHERE ts < ?",
        (int(time.time()) - retain_sec,),
    )
    return cur.rowcount or 0


# =============================================================================
# Anti-lockout: emergency recovery tokens
# =============================================================================

RECOVERY_TTL_SEC = 10 * 60        # 10 минут окна (короче чем для URL — токен короткий)
RECOVERY_TOKEN_LEN = 8            # 8 символов
RECOVERY_MAX_ATTEMPTS = 5         # после стольких ошибок токен сгорает
# Безопасный алфавит без сбивающих символов (нет 0/O, 1/I/L). 31 символ.
# 31^8 ≈ 8.5*10^11 комбинаций — для брутфорса нужны года при rate-limit.
RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _hash_recovery_token(plain: str) -> str:
    """Single-pass SHA-256 над нормализованным токеном (uppercase, без дашей)."""
    return hashlib.sha256(_normalize_recovery_token(plain).encode("utf-8")).hexdigest()


def _normalize_recovery_token(plain: str) -> str:
    """Юзер мог ввести 'k7m3-x9p2' или 'k7m3 x9p2' или 'K7M3X9P2' — приводим
    к каноничному виду 'K7M3X9P2'. Несовместимые символы игнорируем."""
    if not plain:
        return ""
    cleaned = "".join(c for c in plain.upper() if c in RECOVERY_ALPHABET)
    return cleaned[:RECOVERY_TOKEN_LEN]


def format_recovery_token_for_display(plain: str) -> str:
    """K7M3X9P2 → K7M3-X9P2 (для печати в журнал, чтобы юзер легче считал)."""
    p = plain.upper()
    if len(p) == 8:
        return f"{p[:4]}-{p[4:]}"
    return p


def create_recovery_token(conn: sqlite3.Connection) -> str:
    """Сгенерить новый recovery-токен. В БД пишем только sha256, plain
    возвращается caller'у для одноразового вывода в консоль."""
    plain = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_TOKEN_LEN))
    now = int(time.time())
    conn.execute(
        "INSERT INTO recovery_tokens (token_sha256, created_at, expires_at) VALUES (?, ?, ?)",
        (_hash_recovery_token(plain), now, now + RECOVERY_TTL_SEC),
    )
    return plain


def find_active_recovery_token(conn: sqlite3.Connection, plain: str):
    """Найти токен в БД и проверить базовые свойства (не использован, не истёк,
    не сгорел от попыток). Возвращает row или None."""
    if not plain:
        return None
    h = _hash_recovery_token(plain)
    row = conn.execute(
        "SELECT token_sha256, expires_at, used_at, attempts "
        "FROM recovery_tokens WHERE token_sha256 = ?",
        (h,),
    ).fetchone()
    if not row:
        return None
    if row["used_at"] is not None:
        return None
    if row["expires_at"] < int(time.time()):
        return None
    if row["attempts"] >= RECOVERY_MAX_ATTEMPTS:
        return None
    return row


def bump_recovery_attempt(conn: sqlite3.Connection, plain: str) -> int:
    """Инкремент attempts на конкретном токене. Возвращает новое значение.
    Если токена нет — 0 (но даже если кто-то перебирает — sha не совпадает,
    запись не обновляется, и это не help'ит атакующему)."""
    h = _hash_recovery_token(plain)
    conn.execute(
        "UPDATE recovery_tokens SET attempts = attempts + 1 WHERE token_sha256 = ?",
        (h,),
    )
    row = conn.execute(
        "SELECT attempts FROM recovery_tokens WHERE token_sha256 = ?", (h,),
    ).fetchone()
    return int(row["attempts"]) if row else 0


def consume_recovery_token(conn: sqlite3.Connection, plain: str,
                           username: str, ip: str) -> bool:
    """Атомарно: проверить условия + пометить used. Race-safe через
    WHERE used_at IS NULL AND expires_at >= now AND attempts < MAX."""
    h = _hash_recovery_token(plain)
    now = int(time.time())
    cur = conn.execute(
        "UPDATE recovery_tokens SET used_at = ?, used_for_user = ?, used_from_ip = ? "
        "WHERE token_sha256 = ? AND used_at IS NULL "
        "AND expires_at >= ? AND attempts < ?",
        (now, username[:64], ip[:64], h, now, RECOVERY_MAX_ATTEMPTS),
    )
    return cur.rowcount > 0


def cleanup_old_recovery_tokens(conn: sqlite3.Connection, retain_sec: int = 86400) -> int:
    """Чистим старые expired+used записи (старше суток)."""
    cur = conn.execute(
        "DELETE FROM recovery_tokens WHERE expires_at < ?",
        (int(time.time()) - retain_sec,),
    )
    return cur.rowcount or 0


# =============================================================================
# IP-баны (persistent fail2ban-style)
# =============================================================================

# Длительности по умолчанию для авто-банов
AUTOBAN_LOGIN_FAILS = 10            # после стольких неудач логина за окно — бан
AUTOBAN_LOGIN_WINDOW_SEC = 30 * 60  # окно подсчёта неудач
AUTOBAN_DURATION_SEC = 30 * 60      # длительность авто-бана


def is_ip_banned(conn: sqlite3.Connection, ip: str) -> dict | None:
    """Если IP в активном бане — вернуть dict с deталями. Истёкшие баны
    автоматически чистятся (lazy)."""
    if not ip:
        return None
    now = int(time.time())
    row = conn.execute(
        "SELECT ip, banned_at, banned_until, reason, auto "
        "FROM ip_bans WHERE ip = ?",
        (ip[:64],),
    ).fetchone()
    if not row:
        return None
    if row["banned_until"] != 0 and row["banned_until"] < now:
        # Истёк — чистим
        conn.execute("DELETE FROM ip_bans WHERE ip = ?", (ip[:64],))
        return None
    return dict(row)


def ban_ip(conn: sqlite3.Connection, ip: str, duration_sec: int = 0,
           reason: str = "", auto: bool = True) -> None:
    """Забанить IP. duration_sec=0 → permanent. Если IP уже забанен — продлить
    до max(текущий, новый)."""
    if not ip:
        return
    now = int(time.time())
    until = 0 if duration_sec == 0 else (now + duration_sec)
    conn.execute(
        "INSERT INTO ip_bans (ip, banned_at, banned_until, reason, auto) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(ip) DO UPDATE SET "
        "  banned_until = CASE "
        "    WHEN banned_until = 0 OR excluded.banned_until = 0 THEN 0 "
        "    WHEN banned_until > excluded.banned_until THEN banned_until "
        "    ELSE excluded.banned_until END, "
        "  reason = CASE WHEN excluded.reason != '' THEN excluded.reason ELSE reason END, "
        "  auto = excluded.auto",
        (ip[:64], now, until, (reason or "")[:200], 1 if auto else 0),
    )


def unban_ip(conn: sqlite3.Connection, ip: str) -> bool:
    """Снять бан. Возвращает True если что-то реально удалилось."""
    cur = conn.execute("DELETE FROM ip_bans WHERE ip = ?", (ip[:64],))
    return cur.rowcount > 0


def list_active_bans(conn: sqlite3.Connection) -> list[dict]:
    """Все активные баны (отсортированы по banned_at DESC)."""
    now = int(time.time())
    return [dict(r) for r in conn.execute(
        "SELECT ip, banned_at, banned_until, reason, auto FROM ip_bans "
        "WHERE banned_until = 0 OR banned_until > ? "
        "ORDER BY banned_at DESC",
        (now,),
    ).fetchall()]


def cleanup_expired_bans(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM ip_bans WHERE banned_until != 0 AND banned_until < ?",
        (int(time.time()),),
    )
    return cur.rowcount or 0


def maybe_autoban_for_login_fails(conn: sqlite3.Connection, ip: str) -> bool:
    """Если с этого IP было N+ неудачных логинов за последнее окно —
    авто-бан на AUTOBAN_DURATION_SEC. Вызывается после record_attempt(...)
    в обработчике /login. Возвращает True если бан произошёл."""
    fails = count_recent_failures(conn, ip, window_sec=AUTOBAN_LOGIN_WINDOW_SEC)
    if fails >= AUTOBAN_LOGIN_FAILS:
        ban_ip(conn, ip,
               duration_sec=AUTOBAN_DURATION_SEC,
               reason=f"auto: {fails} failed logins in "
                      f"{AUTOBAN_LOGIN_WINDOW_SEC // 60} min",
               auto=True)
        return True
    return False


# =============================================================================
# TOTP (RFC 6238) — 2FA через Google Authenticator / Authy / Aegis / 1Password.
# Чистый stdlib: hmac + sha1 + base32. Период 30с, 6 цифр, ±1 шаг для drift.
# =============================================================================

import struct as _struct

TOTP_PERIOD = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1     # ±1 шаг = ±30с — компенсация рассинхрона часов
TOTP_ISSUER = "Roboport"

PENDING_2FA_TTL_SEC = 5 * 60     # 5 мин на ввод TOTP-кода после password
PENDING_2FA_COOKIE = "roboport_pending2fa"


def generate_totp_secret() -> str:
    """20 случайных байт → base32 без padding (формат для GA)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_compute(secret_b32: str, counter: int) -> str:
    """HOTP/TOTP — RFC 6238."""
    # Восстанавливаем padding для b32decode
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode((secret_b32 + pad).upper())
    msg = _struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = _struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def totp_now(secret_b32: str) -> str:
    """Текущий 6-значный TOTP-код. Только для дебага/тестов."""
    return _totp_compute(secret_b32, int(time.time()) // TOTP_PERIOD)


def verify_totp(secret_b32: str, supplied: str) -> bool:
    """Constant-time проверка TOTP-кода с window=±1 (~30с tolerance)."""
    if not secret_b32 or not supplied:
        return False
    s = (supplied or "").strip().replace(" ", "")
    if not s.isdigit() or len(s) != TOTP_DIGITS:
        return False
    try:
        now_counter = int(time.time()) // TOTP_PERIOD
        for offset in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
            actual = _totp_compute(secret_b32, now_counter + offset)
            if hmac.compare_digest(actual, s):
                return True
    except Exception:
        return False
    return False


def otpauth_url(secret_b32: str, account: str) -> str:
    """otpauth://totp/Roboport:account?secret=...&issuer=Roboport
    Любой стандартный TOTP-authenticator (Google, Authy, Aegis, 1Password)
    принимает либо вставку этого URL, либо QR-код с этого URL."""
    import urllib.parse as _up
    label = f"{TOTP_ISSUER}:{account}"
    params = _up.urlencode({
        "secret": secret_b32,
        "issuer": TOTP_ISSUER,
        "algorithm": "SHA1",
        "digits": str(TOTP_DIGITS),
        "period": str(TOTP_PERIOD),
    })
    return f"otpauth://totp/{_up.quote(label)}?{params}"


def format_secret_for_display(secret_b32: str) -> str:
    """ABCDEFGHIJKLMNOP → ABCD EFGH IJKL MNOP — легче читать/набирать вручную."""
    return " ".join(secret_b32[i:i + 4] for i in range(0, len(secret_b32), 4))


def set_user_totp(conn: sqlite3.Connection, user_id: int,
                  secret_b32: str, enabled: bool) -> None:
    conn.execute(
        "UPDATE users SET totp_secret = ?, totp_enabled = ? WHERE id = ?",
        (secret_b32, 1 if enabled else 0, user_id),
    )


def disable_user_totp(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE users SET totp_secret = NULL, totp_enabled = 0 WHERE id = ?",
        (user_id,),
    )
    conn.execute("DELETE FROM backup_codes WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM pending_2fa WHERE user_id = ?", (user_id,))


# =============================================================================
# Pending 2FA — промежуточное состояние между password OK и TOTP-кодом
# =============================================================================

@dataclass
class PendingInfo:
    id: str
    user_id: int
    username: str
    expires_at: int


def create_pending_2fa(conn: sqlite3.Connection, user_id: int,
                        ip: str = "", user_agent: str = "") -> PendingInfo:
    pid = secrets.token_urlsafe(SESSION_ID_BYTES)
    now = int(time.time())
    expires = now + PENDING_2FA_TTL_SEC
    conn.execute(
        "INSERT INTO pending_2fa (id, user_id, created_at, expires_at, ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pid, user_id, now, expires, ip[:64], (user_agent or "")[:256]),
    )
    user = get_user_by_id(conn, user_id)
    return PendingInfo(
        id=pid, user_id=user_id,
        username=user["username"] if user else "?",
        expires_at=expires,
    )


def load_pending_2fa(conn: sqlite3.Connection, pending_id: str) -> PendingInfo | None:
    if not pending_id or len(pending_id) > 200:
        return None
    row = conn.execute(
        "SELECT p.id, p.user_id, p.expires_at, u.username "
        "FROM pending_2fa p JOIN users u ON u.id = p.user_id "
        "WHERE p.id = ?",
        (pending_id,),
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] < int(time.time()):
        conn.execute("DELETE FROM pending_2fa WHERE id = ?", (pending_id,))
        return None
    return PendingInfo(
        id=row["id"], user_id=row["user_id"],
        username=row["username"], expires_at=row["expires_at"],
    )


def consume_pending_2fa(conn: sqlite3.Connection, pending_id: str) -> None:
    conn.execute("DELETE FROM pending_2fa WHERE id = ?", (pending_id,))


def cleanup_expired_pending_2fa(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM pending_2fa WHERE expires_at < ?", (int(time.time()),),
    )
    return cur.rowcount or 0


# =============================================================================
# Backup-коды для 2FA
# =============================================================================

BACKUP_CODE_COUNT = 10
BACKUP_CODE_BYTES = 6   # 12 hex chars per code → формат XXXX-XXXX-XXXX


def _hash_backup_code(plain: str) -> str:
    """Нормализуем (lower, без -/пробелов) и sha256."""
    normalized = "".join(c for c in plain.lower() if c.isalnum())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _format_backup_code(plain_hex: str) -> str:
    """abcd1234ef56 → ABCD-1234-EF56 (uppercase, дефисы по 4)."""
    s = plain_hex.upper()
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def regenerate_backup_codes(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """Снести старые backup-коды, сгенерить N новых. Возвращает PLAIN-коды
    (форматированные), сохраняем в БД только sha256. Caller обязан commit."""
    conn.execute("DELETE FROM backup_codes WHERE user_id = ?", (user_id,))
    out: list[str] = []
    now = int(time.time())
    for _ in range(BACKUP_CODE_COUNT):
        plain_hex = secrets.token_hex(BACKUP_CODE_BYTES)
        formatted = _format_backup_code(plain_hex)
        out.append(formatted)
        conn.execute(
            "INSERT INTO backup_codes (user_id, code_sha256, created_at) VALUES (?, ?, ?)",
            (user_id, _hash_backup_code(plain_hex), now),
        )
    return out


def consume_backup_code(conn: sqlite3.Connection, user_id: int,
                        plain: str, ip: str = "") -> bool:
    """Если код есть, не использован, и принадлежит user_id — пометить как
    used и вернуть True. Атомарно через UPDATE с условием."""
    h = _hash_backup_code(plain)
    cur = conn.execute(
        "UPDATE backup_codes SET used_at = ?, used_from_ip = ? "
        "WHERE user_id = ? AND code_sha256 = ? AND used_at IS NULL",
        (int(time.time()), ip[:64], user_id, h),
    )
    return cur.rowcount > 0


def count_unused_backup_codes(conn: sqlite3.Connection, user_id: int) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM backup_codes WHERE user_id = ? AND used_at IS NULL",
        (user_id,),
    ).fetchone()[0])


def has_any_backup_codes(conn: sqlite3.Connection, user_id: int) -> bool:
    """Хоть один backup-код был когда-либо сгенерирован (used или нет)."""
    return bool(conn.execute(
        "SELECT 1 FROM backup_codes WHERE user_id = ? LIMIT 1", (user_id,),
    ).fetchone())


# =============================================================================
# QR-код для otpauth-URL — SVG inline (без deps на Pillow)
# =============================================================================

def render_qr_svg(data: str, box_size: int = 6, border: int = 2) -> str:
    """Сгенерить SVG-QR из строки. Возвращает <svg>...</svg> для вставки в
    шаблон, либо пустую строку если пакет qrcode не установлен / упал.
    Шаблон при пустом QR'е должен показать otpauth-URL текстом — этого хватает
    любому современному authenticator'у (Aegis, 1Password, Authy умеют принимать
    URL вставкой; Google Authenticator — только камерой, но там просто
    переключаются на manual-entry секрета).

    Используем qrcode.image.svg.SvgPathImage — компактный single-path SVG,
    без deps на Pillow."""
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
        img = qrcode.make(
            data,
            image_factory=SvgPathImage,
            box_size=box_size,
            border=border,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
        )
        import io as _io
        buf = _io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except ImportError:
        # Пакет qrcode не установлен — fallback на текстовый otpauth-URL.
        # `pip install -r requirements.txt` чинит это.
        print("[auth] qrcode пакет не установлен — QR не показываем, "
              "юзер добавит TOTP вручную через otpauth-URL", flush=True)
        return ""
    except Exception as e:
        # Неожиданная ошибка рендера (битые данные? слишком длинный URL?) —
        # не валим страницу, отдаём fallback.
        print(f"[auth] render_qr_svg failed: {type(e).__name__}: {e}", flush=True)
        return ""


# =============================================================================
# CSRF helpers
# =============================================================================

def csrf_token_valid(session_csrf: str, supplied: str) -> bool:
    """Constant-time compare. Если токены пустые / разной длины — False."""
    if not session_csrf or not supplied:
        return False
    if len(session_csrf) != len(supplied):
        return False
    return hmac.compare_digest(session_csrf, supplied)


# =============================================================================
# API-ключи (Phase 1 публичного API)
# =============================================================================
# Plain-key формата `rpk_<32 base64url-символа>` (~190 бит энтропии).
# В БД — sha256(plain), plain показывается ОДИН РАЗ при создании.
# Scope — список из 4 уровней (read/command/control/admin), хранится как
# comma-separated TEXT в колонке api_keys.scopes. Iterable доступа: set.

API_KEY_PREFIX = "rpk_"
API_KEY_BODY_BYTES = 24            # 24 байта → 32 base64url-символа
API_KEY_NAME_MIN = 1
API_KEY_NAME_MAX = 64

# Все валидные scope. Любой scope не из этого набора → ValueError при создании.
API_SCOPES_ALL = ("read", "command", "control", "admin")


class ApiKeyError(Exception):
    """Ошибка работы с API-ключом (валидация имени/scope/etc)."""


def generate_api_key_plain() -> str:
    """Сгенерить новый plain-ключ. Возвращается ОДИН РАЗ — в БД сразу
    сохраним sha256."""
    body = secrets.token_urlsafe(API_KEY_BODY_BYTES)
    return f"{API_KEY_PREFIX}{body}"


def hash_api_key(plain: str) -> str:
    """sha256-hex от plain-key. Используется и при создании (запись в БД),
    и при verify (lookup)."""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def mask_api_key(plain: str | None) -> str:
    """Маска для логов/debug-вывода: `rpk_AbCd…wXyZ` (первые 4+4 символа
    хвоста). Если значение None/пустое/слишком короткое — возвращает
    плейсхолдер. Сейчас в коде нигде не дёргается, но превентивно — на
    случай если в будущем добавим debug-print или рассуждаем в тикете."""
    if not plain:
        return "<no-key>"
    if not plain.startswith(API_KEY_PREFIX):
        # неизвестный формат — маскируем целиком, не подсказывая структуру
        return "<bad-fmt>"
    body = plain[len(API_KEY_PREFIX):]
    if len(body) < 8:
        return f"{API_KEY_PREFIX}<short>"
    return f"{API_KEY_PREFIX}{body[:4]}…{body[-4:]}"


def parse_scopes(raw: str) -> set[str]:
    """'read,command' → {'read', 'command'}. Игнорирует пустые и неизвестные.
    Дедуплицирует. Возвращает SET для удобной проверки `'read' in scopes`."""
    if not raw:
        return set()
    return {s.strip() for s in raw.split(",")
            if s.strip() in API_SCOPES_ALL}


def format_scopes(scopes: set[str]) -> str:
    """Канонический вид для записи в БД: 'admin,command,control,read'
    (упорядочено для детерминизма, удобно diff'ить в audit-логах)."""
    return ",".join(sorted(s for s in scopes if s in API_SCOPES_ALL))


def validate_api_key_name(name: str) -> str:
    """Чистка и валидация имени ключа. Возвращает нормализованное."""
    name = (name or "").strip()
    if not (API_KEY_NAME_MIN <= len(name) <= API_KEY_NAME_MAX):
        raise ApiKeyError(tr(
            "auth.api_key_name_length", min=API_KEY_NAME_MIN, max=API_KEY_NAME_MAX))
    # Любые печатаемые символы кроме control'ов. Иначе UI/audit ломается.
    if any(ord(c) < 32 for c in name):
        raise ApiKeyError(tr("auth.api_key_name_control_characters"))
    return name


def create_api_key(conn: sqlite3.Connection, owner_user_id: int,
                   name: str, scopes: set[str],
                   expires_at: int | None = None,
                   rate_limit_per_min: int = 60) -> tuple[int, str]:
    """Сгенерить новый ключ, записать в БД, вернуть (id, plain).
    plain показывается caller'у ровно один раз. Caller обязан commit'нуть."""
    name = validate_api_key_name(name)
    valid = {s for s in scopes if s in API_SCOPES_ALL}
    if not valid:
        raise ApiKeyError(tr("auth.api_key_scopes_required", scopes=API_SCOPES_ALL))
    if rate_limit_per_min < 1 or rate_limit_per_min > 100000:
        raise ApiKeyError(tr("auth.api_key_rate_limit_range"))
    if expires_at is not None and expires_at <= int(time.time()):
        raise ApiKeyError(tr("auth.api_key_expiry_future"))

    plain = generate_api_key_plain()
    cur = conn.execute(
        "INSERT INTO api_keys (name, key_sha256, scopes, created_at, expires_at, "
        "owner_user_id, rate_limit_per_min) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, hash_api_key(plain), format_scopes(valid),
         int(time.time()), expires_at, owner_user_id, rate_limit_per_min),
    )
    return int(cur.lastrowid), plain


@dataclass
class ApiKeyInfo:
    """Загруженный ключ — то, что dependency возвращает в роут.
    Содержит scope-set для удобной проверки."""
    id: int
    name: str
    scopes: set[str]
    owner_user_id: int
    rate_limit_per_min: int
    expires_at: int | None


def verify_api_key(conn: sqlite3.Connection, plain: str,
                   ip: str = "") -> ApiKeyInfo | None:
    """Найти ключ по plain → проверить активность → обновить last_used → вернуть
    ApiKeyInfo. None если ключа нет / отозван / истёк / битый prefix.
    Caller обязан commit'нуть для UPDATE last_used."""
    if not plain or not plain.startswith(API_KEY_PREFIX):
        return None
    if len(plain) > 200:  # защита от чрезмерно длинных запросов
        return None
    row = conn.execute(
        "SELECT id, name, scopes, expires_at, revoked_at, owner_user_id, "
        "       rate_limit_per_min "
        "FROM api_keys WHERE key_sha256 = ?",
        (hash_api_key(plain),),
    ).fetchone()
    if not row:
        return None
    now = int(time.time())
    if row["revoked_at"] is not None:
        return None
    if row["expires_at"] is not None and row["expires_at"] < now:
        return None
    # Обновим last_used. Не чаще чем раз в 60 сек, чтобы не тыркать БД на каждом
    # запросе бота с polling'ом.
    conn.execute(
        "UPDATE api_keys SET last_used_at = ?, last_used_ip = ? "
        "WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
        (now, ip[:64], row["id"], now - 60),
    )
    return ApiKeyInfo(
        id=row["id"], name=row["name"],
        scopes=parse_scopes(row["scopes"]),
        owner_user_id=row["owner_user_id"],
        rate_limit_per_min=row["rate_limit_per_min"],
        expires_at=row["expires_at"],
    )


def list_api_keys(conn: sqlite3.Connection, owner_user_id: int) -> list[dict]:
    """Все ключи юзера (включая отозванные — они помечены revoked_at).
    БЕЗ key_sha256 — он не нужен в UI и не должен светиться."""
    return [dict(r) for r in conn.execute(
        "SELECT id, name, scopes, created_at, expires_at, last_used_at, "
        "       last_used_ip, revoked_at, rate_limit_per_min "
        "FROM api_keys WHERE owner_user_id = ? ORDER BY created_at DESC",
        (owner_user_id,),
    ).fetchall()]


def revoke_api_key(conn: sqlite3.Connection, key_id: int,
                   owner_user_id: int) -> bool:
    """Пометить ключ revoked_at = now. Только если принадлежит owner'у —
    защита от подмены id в форме. Возвращает True если что-то реально отозвалось.
    Caller обязан commit."""
    now = int(time.time())
    cur = conn.execute(
        "UPDATE api_keys SET revoked_at = ? "
        "WHERE id = ? AND owner_user_id = ? AND revoked_at IS NULL",
        (now, key_id, owner_user_id),
    )
    return cur.rowcount > 0


def hard_delete_api_key(conn: sqlite3.Connection, key_id: int,
                        owner_user_id: int) -> bool:
    """Физически удалить ключ из БД. В отличие от revoke (мягкая отметка
    revoked_at для истории), это безвозвратно. Используется для очистки
    UI-истории отозванных ключей. Возвращает True если что-то удалилось.
    Caller обязан commit."""
    cur = conn.execute(
        "DELETE FROM api_keys WHERE id = ? AND owner_user_id = ?",
        (key_id, owner_user_id),
    )
    return cur.rowcount > 0


def get_api_key_by_id(conn: sqlite3.Connection, key_id: int,
                      owner_user_id: int) -> dict | None:
    """Один ключ юзера по id (для подтверждения отзыва UI'ем)."""
    row = conn.execute(
        "SELECT id, name, scopes, created_at, expires_at, last_used_at, "
        "       revoked_at FROM api_keys WHERE id = ? AND owner_user_id = ?",
        (key_id, owner_user_id),
    ).fetchone()
    return dict(row) if row else None


def cleanup_expired_api_keys(conn: sqlite3.Connection) -> int:
    """Удалить ключи которые истекли (expires_at < now) или отозваны больше
    30 дней назад. Чистка из sampler._prune."""
    now = int(time.time())
    cur = conn.execute(
        "DELETE FROM api_keys "
        "WHERE (expires_at IS NOT NULL AND expires_at < ?) "
        "   OR (revoked_at IS NOT NULL AND revoked_at < ?)",
        (now, now - 30 * 86400),
    )
    return cur.rowcount or 0


# =============================================================================
# API request log — полный access-log /api/v1/* (Phase 5 Mini)
# =============================================================================
# Каждый запрос к /api/v1/* пишется в api_request_log: (ts, key_id, method,
# path, status, duration_ms, ip, user_agent). Цель — иметь trail GET'ов,
# которые не попадают в audit_log. Анонимные fail'ы (auth не прошёл) тоже
# логируются с key_id=NULL — для observability brute-force попыток.

API_REQUEST_LOG_RETAIN_DAYS = 30


def insert_api_request_log(conn: sqlite3.Connection, *, ts: int,
                           key_id: int | None, method: str, path: str,
                           status: int, duration_ms: int, ip: str,
                           user_agent: str | None) -> None:
    """Вставка одной записи в api_request_log. Вызывается из middleware
    после ответа handler'а. user_agent обрезается до 500 символов чтобы не
    раздувать БД на длинных User-Agent'ах."""
    if user_agent and len(user_agent) > 500:
        user_agent = user_agent[:500]
    conn.execute(
        "INSERT INTO api_request_log (ts, key_id, method, path, status, "
        "                              duration_ms, ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, key_id, method, path, status, duration_ms, ip, user_agent),
    )


def list_api_requests_for_key(conn: sqlite3.Connection, key_id: int,
                              limit: int = 100) -> list[dict]:
    """Последние N запросов для конкретного ключа. Использует индекс
    api_request_log_key_idx. Возвращает в порядке свежести (DESC ts)."""
    if limit <= 0:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT id, ts, method, path, status, duration_ms, ip, user_agent "
        "FROM api_request_log WHERE key_id = ? "
        "ORDER BY ts DESC LIMIT ?",
        (key_id, int(limit)),
    ).fetchall()]


def cleanup_old_api_requests(conn: sqlite3.Connection,
                             retain_days: int = API_REQUEST_LOG_RETAIN_DAYS
                             ) -> int:
    """Удалить записи старше retain_days. Чистка из sampler._prune."""
    cutoff = int(time.time()) - retain_days * 86400
    cur = conn.execute(
        "DELETE FROM api_request_log WHERE ts < ?", (cutoff,),
    )
    return cur.rowcount or 0
