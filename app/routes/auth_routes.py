"""Auth-routes: /login, /logout, /setup и настройки учётной записи.

Особенности:
* /setup активен ТОЛЬКО когда в БД нет ни одного пользователя (first-run).
  После создания первого админа — отдаёт 404 (защита от создания вторых
  админов через эту дверь).
* /login принимает next-параметр для возврата на исходную страницу.
  next валидируется: только относительные пути на нашем домене.
* /logout требует POST (anti-CSRF — даже без middleware GET-logout был бы
  уязвим к <img src="/logout">).
* Rate-limit и CSRF валидация — в middleware (см. main.py), не здесь.
"""
from __future__ import annotations

import os
import sqlite3
import time
import urllib.parse
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse

from .. import auth as auth_lib
from ..db import audit, connect, get_db
from ..i18n import tr
from ._settings_redirect import settings_redirect as _settings_redirect

router = APIRouter()


def client_ip(req) -> str:
    """Uvicorn resolves the client address for explicitly trusted proxies."""
    return (req.client.host if req.client else "")[:64]


# Назад-совместимый алиас. Внутри модуля старый код продолжает звать _client_ip.
_client_ip = client_ip


# =============================================================================
# WebSocket-аутентификация
# =============================================================================
# HTTP-middleware не запускаются на WS (scope.type='websocket'), поэтому каждый
# @websocket-роут обязан звать ws_auth_or_close() в самом начале. Он делает три
# проверки в одной транзакции:
#   1) IP-ban (синхронизировано с ban_middleware для HTTP)
#   2) cookie сессии есть и валидна (load_session с sliding-renew)
#   3) Origin совпадает с Host (defense-in-depth поверх SameSite=Strict cookie)
#
# Возвращает SessionInfo при успехе, None при отказе (handshake уже закрыт
# с code=1008 policy violation — фронт получит чистый код и редиректнет
# на /login). Caller просто:
#
#     sess = await ws_auth_or_close(ws)
#     if sess is None: return
#     await ws.accept()
#
# accept() остаётся за caller'ом — close без accept = HTTP 403 на handshake,
# accept + close(1008) = чистый WS close-frame (мы выбираем второе ради UX).

async def ws_auth_or_close(ws: WebSocket) -> auth_lib.SessionInfo | None:
    """Авторизация WebSocket. При отказе делает accept()+close(1008) и
    возвращает None. При успехе — возвращает SessionInfo, не accept'ит
    (это делает caller). Все БД-операции в одной короткой транзакции."""

    async def _reject() -> None:
        # accept перед close чтобы фронт получил code=1008, а не 1006 abnormal.
        try:
            await ws.accept()
        except Exception:
            return  # клиент уже отвалился — нечего закрывать
        try:
            await ws.close(code=1008)
        except Exception:
            pass

    sid = ws.cookies.get(auth_lib.SESSION_COOKIE)
    if not sid:
        await _reject()
        return None

    ip = client_ip(ws)
    sess: auth_lib.SessionInfo | None = None
    try:
        with connect() as conn:
            if auth_lib.is_ip_banned(conn, ip):
                conn.commit()  # для lazy-cleanup истёкших банов
                await _reject()
                return None
            sess = auth_lib.load_session(conn, sid)
            conn.commit()  # для sliding-renew UPDATE last_seen_at
    except Exception as e:
        # БД упала — лучше не пускать, чем впустить неавторизованного
        print(f"[ws] auth DB error: {e}", flush=True)
        await _reject()
        return None

    if sess is None:
        await _reject()
        return None

    # Origin-check: SameSite=Strict уже отрезает чужие сайты, но не-браузерные
    # клиенты её игнорят. Если Origin прислан — он должен совпадать с Host.
    # Пустой Origin (curl/wscat) пропускаем — у них cookie всё равно нет,
    # они бы сюда не дошли.
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    if origin and host:
        try:
            if urlparse(origin).netloc != host:
                await _reject()
                return None
        except Exception:
            await _reject()
            return None

    return sess


def _safe_next(next_url: str | None) -> str:
    """Защита от open-redirect: разрешаем только относительные пути на нашем домене.
    Запрещаем '//evil.com', 'http://...', 'javascript:', etc."""
    if not next_url:
        return "/"
    next_url = next_url.strip()
    # Должно начинаться с '/' и НЕ с '//' (protocol-relative URL)
    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    # Без переноса строк / control-символов
    if "\\" in next_url or any(ord(c) < 32 or ord(c) == 127 for c in next_url):
        return "/"
    return next_url[:500]


def _set_session_cookie(response, session_id: str, secure: bool) -> None:
    response.set_cookie(
        key=auth_lib.SESSION_COOKIE,
        value=session_id,
        max_age=auth_lib.SESSION_TTL_SEC,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https"


# =============================================================================
# In-memory rate-limit на /login (короткий «первый эшелон»).
# =============================================================================
# Persistent autoban (DB-уровень, AUTOBAN_LOGIN_FAILS) — это «второй эшелон»,
# срабатывает на больший масштаб (10 fails / 30 мин). А этот rl-bucket бьёт
# первым: 10 fails / 5 мин → 15 мин. Сбрасывается на рестарте процесса —
# это намеренно, для соло-панели приемлемо.
#
# State хранится здесь (не в main.py) чтобы и login_form GET мог посмотреть,
# когда юзер сможет повторить попытку, и нарисовать таймер на странице.

LOGIN_RL_FAIL_LIMIT = 10
LOGIN_RL_WINDOW_SEC = 5 * 60
LOGIN_RL_BAN_SEC = 15 * 60
_login_rl_state: dict[str, dict] = {}


def login_rl_get_wait(ip: str) -> int:
    """Сколько секунд осталось до конца rate-limit ban'а для этого IP.
    0 = не забанен. >0 = в бане, столько-то секунд ждать."""
    if not ip:
        return 0
    now = int(time.time())
    st = _login_rl_state.get(ip)
    if st and st.get("banned_until", 0) > now:
        return st["banned_until"] - now
    return 0


def login_rl_record(ip: str, success: bool) -> None:
    """Записать исход POST /login. Успешный логин чистит счётчик (юзер ввёл
    правильно — мы ему доверяем). Неудачный увеличивает; при достижении лимита
    в окне — выставляется banned_until."""
    if not ip:
        return
    now = int(time.time())
    st = _login_rl_state.get(ip)
    if not st or (now - st.get("first_ts", 0)) > LOGIN_RL_WINDOW_SEC:
        st = {"fails": 0, "first_ts": now, "banned_until": 0}
    if success:
        _login_rl_state.pop(ip, None)
        return
    st["fails"] += 1
    if st["fails"] >= LOGIN_RL_FAIL_LIMIT:
        st["banned_until"] = now + LOGIN_RL_BAN_SEC
    _login_rl_state[ip] = st


def login_wait_for_template(db, ip: str) -> tuple[int, bool]:
    """Объединяет in-memory rate-limit и persistent IP-ban в одно значение
    `wait_until` для рендера login-формы. Возвращает (until_unix_ts, is_permanent).

    until=0 — не забанен.
    is_permanent=True — DB-ban с banned_until=0 (никогда не истечёт сам).
    Иначе until — unix-ts когда снова можно пробовать."""
    rl_wait = login_rl_get_wait(ip)
    rl_until = (int(time.time()) + rl_wait) if rl_wait > 0 else 0

    db_ban = auth_lib.is_ip_banned(db, ip) if ip else None
    if db_ban is None:
        return rl_until, False
    db_until = int(db_ban.get("banned_until") or 0)
    if db_until == 0:
        # permanent — сразу возвращаем, таймер не нужен
        return 0, True
    # Берём максимум: какой бы из двух не истёк раньше, второй продолжит
    # давить. Юзеру удобно видеть итоговое время до полной разблокировки.
    return max(rl_until, db_until), False


# =============================================================================
# /setup — first-run, создание админа
# =============================================================================

@router.get("/setup")
def setup_form(request: Request, db: sqlite3.Connection = Depends(get_db)):
    """Форма создания первого админа. 404 если уже есть пользователи."""
    if auth_lib.count_users(db) > 0:
        raise HTTPException(404, tr("auth_routes.setup_already_completed_user_exists"))
    return request.app.state.templates.TemplateResponse(
        request, "login.html",
        {
            "page": "setup",
            "is_setup": True,
            "setup_command": (r".\.venv\Scripts\python.exe" if os.name == "nt" else ".venv/bin/python") + " -m app.cli setup-code",
            "msg": (request.query_params.get("msg") or "").strip() or None,
            "err": (request.query_params.get("err") or "").strip() or None,
        },
    )


@router.post("/setup")
def setup_submit(request: Request,
                 username: str = Form(...),
                 password: str = Form(...),
                 password2: str = Form(...),
                 setup_code: str = Form(""),
                 db: sqlite3.Connection = Depends(get_db)):
    if auth_lib.count_users(db) > 0:
        raise HTTPException(404, tr("auth_routes.setup_already_completed"))
    if password != password2:
        return RedirectResponse(
            url="/setup?err=" + urllib.parse.quote(tr("auth_routes.passwords_do_not_match")),
            status_code=303,
        )
    try:
        with db:
            if not auth_lib.consume_setup_code(db, setup_code.strip()):
                return RedirectResponse("/setup?err=" + urllib.parse.quote(tr("auth_routes.setup_code_invalid")), status_code=303)
            user_id = auth_lib.create_user(db, username, password)
            ip = _client_ip(request)
            auth_lib.mark_user_logged_in(db, user_id, ip)
            sess = auth_lib.create_session(db, user_id, ip=ip, user_agent=request.headers.get("user-agent", ""))
            db.execute("DELETE FROM recovery_tokens WHERE used_at IS NULL")
            audit(db, "auth.setup", None, summary=f"created admin {username!r} from {ip}")
    except auth_lib.AuthError as e:
        return RedirectResponse(
            url="/setup?err=" + urllib.parse.quote(str(e)),
            status_code=303,
        )
    resp = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(resp, sess.id, secure=_is_https(request))
    return resp


# =============================================================================
# /login
# =============================================================================

@router.get("/login")
def login_form(request: Request, db: sqlite3.Connection = Depends(get_db)):
    # Если ни одного юзера — редирект на /setup
    if auth_lib.count_users(db) == 0:
        return RedirectResponse(url="/setup", status_code=303)
    ip = client_ip(request)
    wait_until, is_permanent = login_wait_for_template(db, ip)
    return request.app.state.templates.TemplateResponse(
        request, "login.html",
        {
            "page": "login",
            "is_setup": False,
            "next": _safe_next(request.query_params.get("next")),
            "msg": (request.query_params.get("msg") or "").strip() or None,
            "err": (request.query_params.get("err") or "").strip() or None,
            # Для UI rate-limit/ban-таймера. Шаблон сам решит показывать или нет.
            "wait_until": wait_until,
            "wait_permanent": is_permanent,
        },
    )


@router.post("/login")
def login_submit(request: Request,
                 username: str = Form(...),
                 password: str = Form(...),
                 next: str = Form("/"),
                 db: sqlite3.Connection = Depends(get_db)):
    # Rate-limit обработан в middleware. Здесь — только проверка кредов.
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    next_url = _safe_next(next)

    username_norm = (username or "").strip().lower()
    user = auth_lib.get_user_by_username(db, username_norm) if username_norm else None

    # Constant-time: даже если user=None, всё равно вызываем verify_password
    # на dummy-хэше, чтобы time-side-channel не выдавал «такого юзера нет».
    DUMMY = "scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    pw_ok = auth_lib.verify_password(password, user["password_hash"] if user else DUMMY)

    if not user or not pw_ok:
        auth_lib.record_attempt(db, ip, username_norm, success=False)
        login_rl_record(ip, success=False)
        audit(db, "auth.login.fail", None,
              summary=f"username={username_norm!r} ip={ip} ua={ua[:80]!r}")
        # Авто-бан если N+ неудач за окно. После бана ip-middleware будет
        # бить любые дальнейшие запросы 403'ом до истечения / до unban'а.
        banned = auth_lib.maybe_autoban_for_login_fails(db, ip)
        if banned:
            audit(db, "auth.ip.autobanned", None,
                  summary=f"IP {ip} autobanned for "
                          f"{auth_lib.AUTOBAN_DURATION_SEC // 60}m "
                          f"after {auth_lib.AUTOBAN_LOGIN_FAILS}+ login fails")
        db.commit()
        # Не раскрываем существует ли юзер — единое сообщение
        err = urllib.parse.quote(tr("auth_routes.invalid_login_or_password"))
        return RedirectResponse(
            url=f"/login?err={err}&next={urllib.parse.quote(next_url)}",
            status_code=303,
        )

    # Password OK. Если 2FA включена — НЕ создаём полную сессию, выдаём
    # pending-2FA и редиректим на /login/2fa. Запись об успехе password
    # пишем только после полного логина (то есть после ввода TOTP).
    if user.get("totp_enabled"):
        pending = auth_lib.create_pending_2fa(db, user["id"], ip=ip, user_agent=ua)
        audit(db, "auth.login.2fa_pending", None,
              summary=f"{username_norm} from {ip} — awaiting TOTP")
        db.commit()
        resp = RedirectResponse(
            url=f"/login/2fa?next={urllib.parse.quote(next_url)}",
            status_code=303,
        )
        resp.set_cookie(
            key=auth_lib.PENDING_2FA_COOKIE, value=pending.id,
            max_age=auth_lib.PENDING_2FA_TTL_SEC,
            httponly=True, samesite="strict",
            secure=_is_https(request), path="/",
        )
        return resp

    # 2FA не включена — обычный логин, full session
    auth_lib.record_attempt(db, ip, username_norm, success=True)
    login_rl_record(ip, success=True)
    auth_lib.mark_user_logged_in(db, user["id"], ip)
    sess = auth_lib.create_session(db, user["id"], ip=ip, user_agent=ua)
    audit(db, "auth.login.ok", None, summary=f"{username_norm} from {ip}")
    db.commit()

    resp = RedirectResponse(url=next_url, status_code=303)
    _set_session_cookie(resp, sess.id, secure=_is_https(request))
    return resp


# =============================================================================
# /login/2fa — второй шаг логина: ввод TOTP-кода
# =============================================================================

@router.get("/login/2fa")
def login_2fa_form(request: Request, db: sqlite3.Connection = Depends(get_db)):
    pid = request.cookies.get(auth_lib.PENDING_2FA_COOKIE)
    pending = auth_lib.load_pending_2fa(db, pid) if pid else None
    if not pending:
        # Нет pending → обычный логин
        return RedirectResponse(url="/login", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request, "login.html",
        {
            "page": "login_2fa",
            "is_setup": False,
            "is_2fa": True,
            "username": pending.username,
            "next": _safe_next(request.query_params.get("next")),
            "msg": (request.query_params.get("msg") or "").strip() or None,
            "err": (request.query_params.get("err") or "").strip() or None,
        },
    )


@router.post("/login/2fa")
def login_2fa_submit(request: Request,
                     code: str = Form(...),
                     next: str = Form("/"),
                     db: sqlite3.Connection = Depends(get_db)):
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    next_url = _safe_next(next)

    pid = request.cookies.get(auth_lib.PENDING_2FA_COOKIE)
    pending = auth_lib.load_pending_2fa(db, pid) if pid else None
    if not pending:
        return RedirectResponse(
            url="/login?err=" + urllib.parse.quote(
                tr("auth_routes.two_factor_session_expired")),
            status_code=303,
        )

    db_user = auth_lib.get_user_by_id(db, pending.user_id)
    if not db_user or not db_user.get("totp_enabled") or not db_user.get("totp_secret"):
        # Состояние сломано — снести pending и начать заново
        auth_lib.consume_pending_2fa(db, pid)
        db.commit()
        return RedirectResponse(url="/login", status_code=303)

    # Принимаем код двумя способами: TOTP (6 цифр) или backup-код (12 hex с/без -).
    # Сначала пробуем TOTP, потом fallback на backup-код.
    totp_ok = auth_lib.verify_totp(db_user["totp_secret"], code)
    backup_ok = False
    if not totp_ok:
        backup_ok = auth_lib.consume_backup_code(db, db_user["id"], code, ip=ip)

    if not (totp_ok or backup_ok):
        auth_lib.record_attempt(db, ip, pending.username, success=False)
        banned = auth_lib.maybe_autoban_for_login_fails(db, ip)
        audit(db, "auth.login.2fa_fail", None,
              summary=f"{pending.username} from {ip}")
        if banned:
            audit(db, "auth.ip.autobanned", None,
                  summary=f"IP {ip} autobanned after 2FA fails")
        db.commit()
        err = urllib.parse.quote(tr("auth_routes.two_factor_invalid_login_code"))
        return RedirectResponse(
            url=f"/login/2fa?err={err}&next={urllib.parse.quote(next_url)}",
            status_code=303,
        )

    # OK — какой именно код использовали, пишем в audit
    used_kind = "TOTP" if totp_ok else "backup"
    if backup_ok:
        # Если использовали backup — отдельная audit-запись с предупреждением
        remaining = auth_lib.count_unused_backup_codes(db, db_user["id"])
        audit(db, "auth.login.backup_used", None,
              summary=f"{pending.username} used backup code from {ip}; "
                      f"{remaining} unused codes left")

    # OK: используем pending, создаём полную сессию
    auth_lib.consume_pending_2fa(db, pid)
    auth_lib.record_attempt(db, ip, pending.username, success=True)
    auth_lib.mark_user_logged_in(db, db_user["id"], ip)
    sess = auth_lib.create_session(db, db_user["id"], ip=ip, user_agent=ua)
    audit(db, "auth.login.ok", None,
          summary=f"{pending.username} from {ip} ({used_kind})")
    db.commit()
    resp = RedirectResponse(url=next_url, status_code=303)
    resp.delete_cookie(auth_lib.PENDING_2FA_COOKIE, path="/")
    _set_session_cookie(resp, sess.id, secure=_is_https(request))
    return resp


# =============================================================================
# /settings/2fa — setup, confirm, disable
# =============================================================================

@router.get("/settings/2fa/setup")
def totp_setup_form(request: Request, db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    db_user = auth_lib.get_user_by_id(db, user.user_id)
    if db_user.get("totp_enabled"):
        return _settings_redirect(msg=tr("auth_routes.two_factor_already_enabled"), tab="security")
    # Генерим pending-secret и сохраняем в users.totp_secret (НЕ-enabled).
    # При confirm проверяем код против этого secret и переключаем enabled=1.
    if not db_user.get("totp_secret"):
        secret = auth_lib.generate_totp_secret()
        auth_lib.set_user_totp(db, user.user_id, secret, enabled=False)
        db.commit()
    else:
        secret = db_user["totp_secret"]

    otpauth = auth_lib.otpauth_url(secret, user.username)
    return request.app.state.templates.TemplateResponse(
        request, "totp_setup.html",
        {
            "page": "settings",
            "username": user.username,
            "secret": secret,
            "secret_display": auth_lib.format_secret_for_display(secret),
            "otpauth": otpauth,
            "qr_svg": auth_lib.render_qr_svg(otpauth),
            "msg": (request.query_params.get("msg") or "").strip() or None,
            "err": (request.query_params.get("err") or "").strip() or None,
        },
    )


@router.post("/settings/2fa/confirm")
def totp_confirm(request: Request,
                 code: str = Form(...),
                 db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    db_user = auth_lib.get_user_by_id(db, user.user_id)
    secret = db_user.get("totp_secret") if db_user else None
    if not secret:
        return RedirectResponse(
            url="/settings/2fa/setup?err=" + urllib.parse.quote(
                tr("auth_routes.request_setup_first")),
            status_code=303,
        )
    if not auth_lib.verify_totp(secret, code):
        return RedirectResponse(
            url="/settings/2fa/setup?err=" + urllib.parse.quote(
                tr("auth_routes.two_factor_code_invalid")),
            status_code=303,
        )
    auth_lib.set_user_totp(db, user.user_id, secret, enabled=True)
    # Сразу генерим backup-коды и редиректим на страницу показа.
    # Они сохраняются в БД как sha256, plain показывается ОДИН раз.
    codes = auth_lib.regenerate_backup_codes(db, user.user_id)
    audit(db, "auth.2fa.enabled", None,
          summary=f"{user.username} enabled 2FA; "
                  f"{len(codes)} backup codes generated")
    db.commit()
    # Передаём коды через session-state — НЕ через query string.
    # Используем in-memory dict, ключ = session_id.
    _backup_codes_to_show[user.id] = codes
    return RedirectResponse(url="/settings/2fa/codes", status_code=303)


# In-memory shadow для одноразового показа backup-кодов после генерации.
# Ключ — session_id. Plain-коды живут пока юзер не открыл страницу
# /settings/2fa/codes (там удаляются), либо до 5 минут.
_backup_codes_to_show: dict[str, list[str]] = {}


@router.get("/settings/2fa/codes")
def totp_codes_show(request: Request, db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    codes = _backup_codes_to_show.pop(user.id, None)
    if codes is None:
        # Прямой переход без только-что-сгенерированных кодов — редирект на /settings.
        return _settings_redirect(
            err=tr("auth_routes.backup_codes_one_time"), tab="security")
    return request.app.state.templates.TemplateResponse(
        request, "totp_codes.html",
        {"page": "settings", "username": user.username, "codes": codes},
    )


@router.post("/settings/2fa/regenerate-codes")
def totp_regenerate_codes(request: Request,
                          password: str = Form(...),
                          db: sqlite3.Connection = Depends(get_db)):
    """Пересоздать backup-коды. Старые становятся невалидны мгновенно.
    Требует step-up + подтверждения паролем."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    redir = stepup_check_or_redirect(request)
    if redir is not None:
        return redir
    db_user = auth_lib.get_user_by_id(db, user.user_id)
    if not db_user or not auth_lib.verify_password(password, db_user["password_hash"]):
        return _settings_redirect(err=tr("auth_routes.invalid_password"), tab="security")
    if not db_user.get("totp_enabled"):
        return _settings_redirect(err=tr("auth_routes.two_factor_not_enabled"), tab="security")
    codes = auth_lib.regenerate_backup_codes(db, user.user_id)
    audit(db, "auth.2fa.codes.regenerated", None,
          summary=f"{user.username}: regenerated {len(codes)} backup codes")
    db.commit()
    _backup_codes_to_show[user.id] = codes
    return RedirectResponse(url="/settings/2fa/codes", status_code=303)


@router.post("/settings/2fa/disable")
def totp_disable(request: Request,
                 password: str = Form(...),
                 db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    redir = stepup_check_or_redirect(request)
    if redir is not None:
        return redir
    db_user = auth_lib.get_user_by_id(db, user.user_id)
    if not db_user or not auth_lib.verify_password(password, db_user["password_hash"]):
        return _settings_redirect(err=tr("auth_routes.invalid_password"), tab="security")
    auth_lib.disable_user_totp(db, user.user_id)
    audit(db, "auth.2fa.disabled", None, summary=f"{user.username} disabled 2FA")
    db.commit()
    return _settings_redirect(msg=tr("auth_routes.two_factor_disabled"), tab="security")


# =============================================================================
# /logout — только POST (защита от GET-CSRF через <img src=>)
# =============================================================================

@router.post("/logout")
def logout(request: Request, db: sqlite3.Connection = Depends(get_db)):
    sid = request.cookies.get(auth_lib.SESSION_COOKIE)
    user = getattr(request.state, "user", None)
    if sid:
        auth_lib.revoke_session(db, sid)
        if user:
            audit(db, "auth.logout", None,
                  summary=f"{user.username} from {_client_ip(request)}")
        db.commit()
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(auth_lib.SESSION_COOKIE, path="/")
    return resp


# =============================================================================
# Настройки учётной записи
# =============================================================================

@router.post("/settings/account/username")
def change_username(request: Request, new_username: str = Form(""),
                    db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    tab = "account-name"
    redirect = stepup_check_or_redirect(request, return_to=f"/settings#{tab}")
    if redirect is not None:
        return redirect

    try:
        username = auth_lib.normalize_username(new_username)
    except auth_lib.AuthError as e:
        return _settings_redirect(err=str(e), tab=tab)

    with db:
        # Serialize the read with the update so retries and simultaneous requests
        # use the current name and log only changes that actually take effect.
        db.execute("BEGIN IMMEDIATE")
        account = auth_lib.get_user_by_id(db, user.user_id)
        if account is None:
            raise HTTPException(401, tr("auth_routes.not_authenticated"))
        previous = account["username"]
        if previous != username:
            try:
                db.execute("UPDATE users SET username = ? WHERE id = ?", (username, user.user_id))
            except sqlite3.IntegrityError:
                # Python 3.10's sqlite module does not expose extended exception codes.
                if not db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                    raise
                return _settings_redirect(err=tr("account.username_taken"), tab=tab)
            audit(
                db, "auth.username.change",
                summary=f"user_id={user.user_id}: {previous!r} -> {username!r}",
            )
    return _settings_redirect(msg=tr("account.username_updated", username=username), tab=tab)


@router.post("/change-password")
def change_password(request: Request,
                    current_password: str = Form(...),
                    new_password: str = Form(...),
                    new_password2: str = Form(...),
                    db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    redir = stepup_check_or_redirect(request)
    if redir is not None:
        return redir
    if new_password != new_password2:
        return _settings_redirect(err=tr("auth_routes.new_passwords_do_not_match"), tab="security")
    db_user = auth_lib.get_user_by_id(db, user.user_id)
    if not db_user or not auth_lib.verify_password(current_password, db_user["password_hash"]):
        audit(db, "auth.password.change.fail", None,
              summary=f"{user.username}: wrong current password")
        db.commit()
        return _settings_redirect(err=tr("auth_routes.current_password_invalid"), tab="security")
    try:
        auth_lib.update_password(db, user.user_id, new_password)
    except auth_lib.AuthError as e:
        return _settings_redirect(err=str(e), tab="security")
    # Сносим все ДРУГИЕ сессии этого юзера (текущую оставляем)
    n_revoked = auth_lib.revoke_user_sessions(db, user.user_id, except_session_id=user.id)
    audit(db, "auth.password.change.ok", None,
          summary=f"{user.username}: revoked {n_revoked} other session(s)")
    db.commit()
    return _settings_redirect(
        msg=tr("auth_routes.password_updated_other_sessions", count=n_revoked), tab="security")


# =============================================================================
# /sessions/revoke — выйти со всех других устройств
# =============================================================================

@router.post("/sessions/revoke-others")
def revoke_others(request: Request, db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, tr("auth_routes.not_authenticated"))
    n = auth_lib.revoke_user_sessions(db, user.user_id, except_session_id=user.id)
    audit(db, "auth.sessions.revoke_others", None,
          summary=f"{user.username}: revoked {n} session(s)")
    db.commit()
    return _settings_redirect(msg=tr("auth_routes.sessions_ended", count=n), tab="security")


# =============================================================================
# Step-up authentication: подтверждение для критичных операций
# =============================================================================
# Helper для embed-в-роут: вызывается в начале POST'а критичной операции.
# Если step-up несвежий → 303 redirect на /stepup?next=<original-url>.
# Если свежий → возвращает None, роут продолжается.
def stepup_check_or_redirect(request: Request, *,
                             return_to: str | None = None) -> RedirectResponse | None:
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if auth_lib.is_stepup_fresh(user):
        return None
    # При POST мы не можем сохранить тело-формы и replay'нуть; редиректим
    # на страницу формы (return_to или referer). У основных операций (delete/clone/
    # config/disable-2fa) форма «жива» на странице сервера/настроек, юзер
    # переоткроет её и нажмёт ещё раз после step-up.
    # ВАЖНО: _safe_next принимает только относительные path'ы, поэтому
    # из полного Referer-URL вытаскиваем path+query, и только если он
    # ведёт на наш же домен.
    referer = request.headers.get("referer", "")
    next_path = return_to or "/"
    if return_to is None and referer:
        try:
            ref_p = urlparse(referer)
            base_p = urlparse(str(request.base_url))
            if ref_p.netloc == base_p.netloc and ref_p.scheme in ("http", "https"):
                next_path = ref_p.path + (("?" + ref_p.query) if ref_p.query else "")
        except Exception:
            pass
    safe = _safe_next(next_path)
    return RedirectResponse(
        url=f"/stepup?next={urllib.parse.quote(safe)}", status_code=303,
    )


@router.get("/stepup")
def stepup_form(request: Request, db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if auth_lib.is_stepup_fresh(user):
        # Уже свежий — сразу возвращаем
        nxt = _safe_next(request.query_params.get("next"))
        return RedirectResponse(url=nxt, status_code=303)
    db_user = auth_lib.get_user_by_id(db, user.user_id)
    return request.app.state.templates.TemplateResponse(
        request, "stepup.html",
        {
            "page": "stepup",
            "username": user.username,
            "totp_enabled": bool(db_user and db_user.get("totp_enabled")),
            "next": _safe_next(request.query_params.get("next")),
            "err": (request.query_params.get("err") or "").strip() or None,
        },
    )


@router.post("/stepup")
def stepup_submit(request: Request,
                  password: str = Form(""),
                  code: str = Form(""),
                  next: str = Form("/"),
                  db: sqlite3.Connection = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    next_url = _safe_next(next)
    db_user = auth_lib.get_user_by_id(db, user.user_id)
    if not db_user:
        return RedirectResponse(url="/login", status_code=303)

    # Пароль обязателен всегда
    if not auth_lib.verify_password(password, db_user["password_hash"]):
        audit(db, "auth.stepup.fail", None,
              summary=f"{user.username}: bad password")
        db.commit()
        return RedirectResponse(
            url="/stepup?" + urllib.parse.urlencode({
                "err": tr("auth_routes.invalid_password"), "next": next_url,
            }),
            status_code=303,
        )
    # Если 2FA включена — TOTP/backup-код тоже обязателен
    if db_user.get("totp_enabled"):
        totp_ok = auth_lib.verify_totp(db_user.get("totp_secret"), code)
        backup_ok = (not totp_ok) and auth_lib.consume_backup_code(
            db, db_user["id"], code, ip=_client_ip(request))
        if not (totp_ok or backup_ok):
            audit(db, "auth.stepup.fail", None,
                  summary=f"{user.username}: bad TOTP/backup")
            db.commit()
            return RedirectResponse(
                url="/stepup?" + urllib.parse.urlencode({
                    "err": tr("auth_routes.two_factor_invalid_stepup_code"), "next": next_url,
                }),
                status_code=303,
            )

    auth_lib.mark_stepup(db, user.id)
    audit(db, "auth.stepup.ok", None, summary=f"{user.username}")
    db.commit()
    return RedirectResponse(url=next_url, status_code=303)


# =============================================================================
# Anti-lockout: запрос recovery-URL через клик «Забыли пароль?»
# =============================================================================
# Юзер кликает на форме логина → POST /recovery/request → панель записывает
# одноразовый URL В СВОЙ stdout (= journalctl). HTTP-ответ URL НЕ содержит:
# атакующий, нажав кнопку из браузера, ничего не получит — только админ с
# доступом к консоли сервера прочитает журнал.
#
# Anti-abuse: in-memory rate-limit per-IP (1 запрос за 5 мин), плюс CLI-вариант
# `app.cli emergency-access` остаётся как fallback.

RECOVERY_REQUEST_COOLDOWN_SEC = 5 * 60       # минимум между двумя кликами с одного IP
RECOVERY_WEB_HARD_LIMIT = 5                  # максимум web-запросов за окно
RECOVERY_WEB_HARD_WINDOW_SEC = 24 * 3600     # окно подсчёта web-запросов (сутки)
# История timestamps per-IP. После исчерпания лимита — единственный путь
# восстановления это CLI (`app.cli emergency-access`), который никаких
# счётчиков не трогает (он за консолью сервера, и так гейт-кипит сам факт доступа).
_recovery_request_state: dict[str, list[int]] = {}


@router.post("/recovery/request")
def recovery_request(request: Request, db=Depends(get_db)):
    if auth_lib.count_users(db) == 0:
        return RedirectResponse("/setup", status_code=303)
    ip = _client_ip(request)
    now = int(time.time())
    cutoff = now - RECOVERY_WEB_HARD_WINDOW_SEC
    history = [ts for ts in _recovery_request_state.get(ip, []) if ts > cutoff]

    # Hard cap: 5 запросов / 24ч с этого IP
    if len(history) >= RECOVERY_WEB_HARD_LIMIT:
        oldest = history[0]
        wait = oldest + RECOVERY_WEB_HARD_WINDOW_SEC - now
        h = max(1, wait // 3600)
        _recovery_request_state[ip] = history  # сохраняем подрезанную
        audit(db, "auth.recovery.web_limit", None,
              summary=f"web cap reached, IP {ip}, ~{h}h until reset")
        db.commit()
        return RedirectResponse(
            url="/login?err=" + urllib.parse.quote(
                tr(
                    "auth_routes.recovery_daily_limit",
                    limit=RECOVERY_WEB_HARD_LIMIT,
                    hours=h,
                )
            ),
            status_code=303,
        )

    # Cooldown: минимум 5 мин между двумя последовательными запросами
    if history and (now - history[-1]) < RECOVERY_REQUEST_COOLDOWN_SEC:
        wait = RECOVERY_REQUEST_COOLDOWN_SEC - (now - history[-1])
        return RedirectResponse(
            url="/login?err=" + urllib.parse.quote(
                tr("auth_routes.recovery_wait", minutes=wait // 60, seconds=wait % 60)
            ),
            status_code=303,
        )

    history.append(now)
    _recovery_request_state[ip] = history

    plain = auth_lib.create_recovery_token(db)
    auth_lib.cleanup_old_recovery_tokens(db)

    # Печатаем в stdout → systemd-journal. Юзер прочитает через консоль и
    # введёт код в форму /recovery. Plain в HTTP-ответе НЕ возвращается.
    bar = "═" * 70
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    pretty = auth_lib.format_recovery_token_for_display(plain)
    print(f"\n{bar}", flush=True)
    print(" ⚡ RECOVERY CODE — введи на странице /recovery", flush=True)
    print(f"   from IP : {ip}", flush=True)
    print(f"   at      : {when}", flush=True)
    print(f"   ttl     : {auth_lib.RECOVERY_TTL_SEC // 60} min", flush=True)
    print(bar, flush=True)
    print(f"   CODE    : {pretty}", flush=True)
    print(bar, flush=True)
    print(" Введи код на странице /recovery (форма откроется автоматически после", flush=True)
    print(" клика 'Записать код в журнал' на странице логина). Регистр и дефис", flush=True)
    print(" не важны.", flush=True)
    print(f"{bar}\n", flush=True)

    audit(db, "auth.recovery.requested", None, summary=f"from web by IP {ip}")
    db.commit()

    return RedirectResponse(url="/recovery", status_code=303)


# =============================================================================
# /recovery — форма ввода кода и нового пароля. Per-IP rate-limit на POST.
# =============================================================================

# Per-IP rate-limit на POST /recovery. Защита от перебора (даже коротких
# токенов на 31^8 ≈ 8.5*10^11). Вместе с per-token attempts < 5 даёт
# двойной слой: нельзя спамить ни одним IP, ни множеством IP по одному токену.
_RECOVERY_FAIL_LIMIT = 5
_RECOVERY_WINDOW_SEC = 10 * 60
_RECOVERY_BAN_SEC = 30 * 60
_recovery_submit_state: dict[str, dict] = {}


def _recovery_rl_check(ip: str) -> tuple[bool, int]:
    """Возвращает (allowed, seconds_to_wait_if_banned)."""
    now = int(time.time())
    st = _recovery_submit_state.get(ip)
    if st and st.get("banned_until", 0) > now:
        return False, st["banned_until"] - now
    return True, 0


def _recovery_rl_record(ip: str, success: bool) -> None:
    now = int(time.time())
    st = _recovery_submit_state.get(ip)
    if not st or (now - st.get("first_ts", 0)) > _RECOVERY_WINDOW_SEC:
        st = {"fails": 0, "first_ts": now, "banned_until": 0}
    if success:
        _recovery_submit_state.pop(ip, None)
        return
    st["fails"] += 1
    if st["fails"] >= _RECOVERY_FAIL_LIMIT:
        st["banned_until"] = now + _RECOVERY_BAN_SEC
    _recovery_submit_state[ip] = st


@router.get("/recovery")
def recovery_form_page(request: Request):
    """Публичная форма: ввод кода + новый пароль. Открывается после
    POST /recovery/request (редирект сюда) либо вручную по знанию URL."""
    return request.app.state.templates.TemplateResponse(
        request, "recovery.html",
        {
            "page": "recovery",
            "msg": (request.query_params.get("msg") or "").strip() or None,
            "err": (request.query_params.get("err") or "").strip() or None,
        },
    )


@router.post("/recovery")
def recovery_submit(request: Request,
                    code: str = Form(...),
                    username: str = Form(""),
                    new_password: str = Form(...),
                    new_password2: str = Form(...),
                    db=Depends(get_db)):
    ip = _client_ip(request)

    # 0. IP-rate-limit: забанен — отвал
    allowed, wait = _recovery_rl_check(ip)
    if not allowed:
        return RedirectResponse(
            url="/recovery?err=" + urllib.parse.quote(
                tr("auth_routes.recovery_attempts_wait", minutes=wait // 60, seconds=wait % 60)
            ),
            status_code=303,
        )

    # 1. Валидация полей
    code_norm = auth_lib._normalize_recovery_token(code)
    if not code_norm or len(code_norm) != auth_lib.RECOVERY_TOKEN_LEN:
        _recovery_rl_record(ip, success=False)
        return RedirectResponse(
            url="/recovery?err=" + urllib.parse.quote(
                tr("auth_routes.recovery_code_length", length=auth_lib.RECOVERY_TOKEN_LEN)
            ),
            status_code=303,
        )
    if new_password != new_password2:
        # Пароли не совпали — не считаем как неверную попытку токена
        return RedirectResponse(
            url="/recovery?err=" + urllib.parse.quote(tr("auth_routes.new_passwords_do_not_match")),
            status_code=303,
        )

    # 2. Проверка токена (без consume — иначе при ошибке в пароле сжигаем зря)
    row = auth_lib.find_active_recovery_token(db, code_norm)
    if not row:
        # Bump per-token attempt counter (если токен есть, но мы пришли сюда —
        # значит он либо использован/истёк/сгорел; если нет — bump просто no-op)
        _ = auth_lib.bump_recovery_attempt(db, code_norm)
        _recovery_rl_record(ip, success=False)
        audit(db, "auth.recovery.fail", None, summary=f"invalid recovery code from {ip}")
        db.commit()
        # Считать сколько попыток осталось у этого IP
        st = _recovery_submit_state.get(ip, {})
        left = max(0, _RECOVERY_FAIL_LIMIT - st.get("fails", 0))
        return RedirectResponse(
            url="/recovery?err=" + urllib.parse.quote(
                tr("auth_routes.recovery_invalid_code", attempts=left)
            ),
            status_code=303,
        )

    # 3. Юзер существует?
    login = username.strip().lower()
    if login:
        user = auth_lib.get_user_by_username(db, login)
    else:
        accounts = db.execute("SELECT id, username FROM users LIMIT 2").fetchall()
        user = accounts[0] if len(accounts) == 1 else None
    if not user:
        # Это НЕ ошибка кода — не bump'аем attempts. Но IP-rl всё равно
        # учитываем чтобы не злоупотребляли формой.
        _recovery_rl_record(ip, success=False)
        return RedirectResponse(
            url="/recovery?err=" + urllib.parse.quote(tr("auth_routes.recovery_user_not_found")),
            status_code=303,
        )

    # Claim the code before changing credentials. Commit both changes together.
    if not auth_lib.consume_recovery_token(db, code_norm, user["username"], ip):
        return RedirectResponse(
            url="/recovery?err=" + urllib.parse.quote(
                tr("auth_routes.recovery_code_used_or_expired")
            ),
            status_code=303,
        )

    try:
        auth_lib.update_password(db, user["id"], new_password)
    except auth_lib.AuthError as e:
        db.rollback()
        return RedirectResponse("/recovery?err=" + urllib.parse.quote(str(e)), status_code=303)
    n_revoked = auth_lib.revoke_user_sessions(db, user["id"])
    _recovery_rl_record(ip, success=True)
    audit(db, "auth.recovery.used", None,
          summary=f"password reset for {user['username']!r} from {ip}, "
                  f"revoked {n_revoked} session(s)")
    db.commit()
    return RedirectResponse(
        url="/login?msg=" + urllib.parse.quote(
            tr("auth_routes.recovery_password_reset")
        ),
        status_code=303,
    )
