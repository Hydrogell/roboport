"""Panel HTTP middleware, in the order an inbound request encounters it."""
from __future__ import annotations

import secrets
import time
import urllib.parse

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from . import auth as auth_lib
from .db import connect
from .i18n import tr
from .locale_web import LocaleMiddleware
from .routes.auth_routes import client_ip, login_rl_get_wait


PUBLIC_EXACT = {"/login", "/login/2fa", "/setup", "/health", "/favicon.ico", "/language",
                "/recovery", "/recovery/request"}
# /stepup НЕ в whitelist — туда заходит только залогиненный юзер,
# auth_middleware пропускает по обычному правилу.
# /recovery/request — POST для генерации кода (печатается в журнал, в HTTP не возвращается).
# /recovery — форма ввода кода + нового пароля. Защита: per-IP rate-limit
# + per-token attempts<5 + 8-символьный random из безопасного алфавита.
# /api/v1/ — публичный API. Cookie-сессия туда НЕ применяется; авторизация
# через API-key (Bearer/X-API-Key), валидируется в dependency require_api_key.
# CSRF тоже не нужен (нет cookie-auth, классическая CSRF-уязвимость отсутствует).
# IP-ban (ban_middleware) продолжает работать — забаненный IP не достучится
# до /api/v1 точно так же как до /login.
PUBLIC_PREFIXES = ("/static/", "/locales/", "/api/v1/")


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


async def auth_middleware(request: Request, call_next):
    """Default-deny: незалогиненный юзер видит только /login (и/или /setup
    при first-run) и статику. Авторизованный — всё. WebSocket-роуты
    проходят отдельно (в auth_ws_check ниже)."""
    path = request.url.path

    # Whitelist
    if _is_public_path(path):
        return await call_next(request)

    # Загружаем сессию из cookie
    sid = request.cookies.get(auth_lib.SESSION_COOKIE)
    sess = None
    if sid:
        try:
            with connect() as conn:
                sess = auth_lib.load_session(conn, sid)
                conn.commit()  # для sliding-renew UPDATE
        except Exception as e:
            print(f"[auth] session load failed: {e}", flush=True)

    if sess is not None:
        # Залогинен — прокидываем в request.state и пропускаем
        request.state.user = sess
        request.state.csrf_token = sess.csrf_token
        return await call_next(request)

    # First-run: нет ни одного юзера → редирект на /setup
    try:
        with connect() as conn:
            n_users = auth_lib.count_users(conn)
    except Exception:
        n_users = 1  # fallback — хотя бы не падаем
    if n_users == 0:
        return RedirectResponse(url="/setup", status_code=303)

    # Не залогинен → /login с возвратом на запрошенную страницу.
    # API/JSON-запросы получают 401 JSON.
    accept = request.headers.get("accept", "")
    if path.startswith("/api") or "application/json" in accept:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    next_param = urllib.parse.quote(path + ("?" + request.url.query if request.url.query else ""))
    return RedirectResponse(url=f"/login?next={next_param}", status_code=303)


# ============================================================================
# Rate limiting на /login (защита от brute-force).
# In-memory bucket по IP: лимиты в auth_routes (LOGIN_RL_*). Состояние и учёт
# fails/successes — в login_submit (явные login_rl_record). Здесь — только
# entry-guard: если IP уже в rate-limit ban'е, не пускаем POST /login и
# редиректим на форму с таймером.
# ============================================================================

def _login_redirect_wait(wait_sec: int, *, permanent: bool = False) -> RedirectResponse:
    """Редирект на GET /login. Сообщение про бан и `until` форма прочитает
    сама из своего же rate-limit/ban state — query-param `until` тут носим
    только для полноты, чтобы при смене IP/прокси не потерять контекст."""
    if permanent:
        msg = tr("main.ban_administrator")
        return RedirectResponse(
            url="/login?err=" + urllib.parse.quote(msg), status_code=303,
        )
    minutes = wait_sec // 60
    seconds = wait_sec % 60
    msg = tr("main.ban_rate_limit", minutes=minutes, seconds=seconds)
    return RedirectResponse(
        url="/login?err=" + urllib.parse.quote(msg), status_code=303,
    )


async def rate_limit_middleware(request: Request, call_next):
    """Rate-limit ТОЛЬКО для POST /login. На остальное не лезем."""
    if not (request.method == "POST" and request.url.path == "/login"):
        return await call_next(request)

    ip = client_ip(request)
    wait = login_rl_get_wait(ip)
    if wait > 0:
        # JSON-клиенты получают честный 429 с Retry-After. HTML-клиенты —
        # стилизованную форму с таймером (через GET /login).
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"error": "rate_limited", "retry_after": wait},
                status_code=429, headers={"Retry-After": str(wait)},
            )
        return _login_redirect_wait(wait)

    return await call_next(request)


# ============================================================================
# Security headers — на каждый ответ.
# ============================================================================

async def security_headers_middleware(request: Request, call_next):
    # Per-request CSP nonce. Все inline <script> тегов снабжены этим nonce
    # через Jinja-context, любой injected <script> без него браузер заблокирует.
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce

    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy",
                 "camera=(), microphone=(), geolocation=(), payment=()")

    # FastAPI генерит HTML для /api/v1/docs (Swagger UI) и /api/v1/redoc с
    # inline `<script>` без nonce'а — мы не контролируем тот HTML и не можем
    # его nonce'ить. Strict CSP блокирует такие скрипты → пустая страница.
    # Для doc-страниц используем loose CSP с 'unsafe-inline' (риск минимален:
    # public docs, без user data, FastAPI-сгенерёный известный HTML).
    p = request.url.path
    if not p.startswith(("/static/", "/locales/")):
        h.setdefault("Cache-Control", "private, no-store")
    is_docs = p in ("/api/v1/docs", "/api/v1/redoc",
                    "/api/v1/docs/oauth2-redirect")
    if is_docs:
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://fastapi.tiangolo.com; "
            "font-src 'self' data: https://cdn.jsdelivr.net; "
            "connect-src 'self'; "
            "worker-src 'self' blob:; "  # Swagger UI использует workers
            "object-src 'none'; "
            "base-uri 'self'"
        )
    else:
        # Strict CSP для всего остального: НИКАКОГО 'unsafe-inline'. Только
        # nonced + 'self' + cdn. Inline-скрипты без правильного nonce
        # заблокированы — целый класс XSS-эксплойтов отсекается.
        # cdn.jsdelivr.net разрешён в script/style/font/connect — нужно для
        # Monaco editor (грузит свои css+amd-modules+шрифты с CDN).
        # worker-src blob: data: — Monaco запускает language workers через
        # blob URL'ы (см. MonacoEnvironment.getWorkerUrl в files.html).
        csp = (
            f"default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
            f"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            f"img-src 'self' data: blob:; "
            f"font-src 'self' data: https://cdn.jsdelivr.net; "
            f"connect-src 'self' ws: wss: https://cdn.jsdelivr.net; "
            f"worker-src 'self' blob: data:; "
            f"child-src 'self' blob:; "  # Safari/старые браузеры читают child-src вместо worker-src
            f"frame-ancestors 'none'; "
            f"base-uri 'self'; "
            f"form-action 'self'; "
            f"object-src 'none'"
        )
    h.setdefault("Content-Security-Policy", csp)
    if request.url.scheme == "https":
        h.setdefault("Strict-Transport-Security",
                     "max-age=31536000; includeSubDomains")
    return response


# ============================================================================
# API access-log middleware (Phase 5 Mini): пишет каждый /api/v1/* запрос
# в api_request_log для observability GET'ов. key_id берётся из
# request.state.api_key (выставляется require_api_key dependency после
# успешного auth) — anonymous fail'ы пишутся с key_id=NULL.
# ============================================================================

# Skip: статика Swagger/ReDoc/OpenAPI — слишком шумно, не несёт ценности.
_API_LOG_SKIP_PREFIXES = (
    "/api/v1/docs",         # Swagger UI HTML + статика
    "/api/v1/redoc",        # ReDoc HTML
    "/api/v1/openapi.json", # OpenAPI spec download
)


async def api_request_log_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/v1/"):
        return await call_next(request)
    if path.startswith(_API_LOG_SKIP_PREFIXES):
        return await call_next(request)

    started = time.monotonic()
    started_wall = int(time.time())

    response = await call_next(request)

    duration_ms = int((time.monotonic() - started) * 1000)
    info = getattr(request.state, "api_key", None)
    key_id = info.id if info is not None else None
    ip = client_ip(request) or ""
    ua = request.headers.get("user-agent")

    try:
        with connect() as conn:
            auth_lib.insert_api_request_log(
                conn, ts=started_wall, key_id=key_id,
                method=request.method, path=path,
                status=response.status_code,
                duration_ms=duration_ms, ip=ip, user_agent=ua,
            )
            conn.commit()
    except Exception as e:
        # Логирование access-log не должно ломать запрос. Пишем в stderr
        # факт ошибки чтобы не молчать совсем.
        print(f"[api-log] insert failed: {e}", flush=True)
    return response


# ============================================================================
# IP bans are checked before login and API access is recorded.
# ============================================================================

async def ban_middleware(request: Request, call_next):
    ip = client_ip(request)
    if not ip:
        return await call_next(request)
    try:
        with connect() as conn:
            ban = auth_lib.is_ip_banned(conn, ip)
            conn.commit()  # для lazy-cleanup истёкших
    except Exception:
        return await call_next(request)
    if ban is None:
        return await call_next(request)
    # Active ban.
    until = int(ban.get("banned_until") or 0)

    # /login (и POST, и GET): отправляем юзера на стилизованную форму с
    # таймером — login_form сам подтянет until из is_ip_banned. Это даёт
    # понятный UX (видно сколько ждать) + позволяет показать "permanent"
    # с человеческим сообщением.
    if request.url.path == "/login":
        if request.method == "GET":
            # GET — пускаем дальше, login_form нарисует баннер. Но проверим
            # на JSON Accept — для этого случая всё равно 403 JSON.
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse(
                    {"error": "ip_banned", "until": until,
                     "reason": ban.get("reason")},
                    status_code=403,
                )
            return await call_next(request)
        # POST → редирект на GET с таймером
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {"error": "ip_banned", "until": until,
                 "reason": ban.get("reason")},
                status_code=403,
            )
        if until == 0:
            return _login_redirect_wait(0, permanent=True)
        return _login_redirect_wait(max(1, until - int(time.time())))

    # Прочие пути — короткий 403. JSON для API, plain text для остального.
    if until == 0:
        when = "permanent"
    else:
        rem = until - int(time.time())
        when = f"{rem // 60}m {rem % 60}s"
    msg = f"Your IP {ip} is banned ({ban.get('reason') or 'no reason'}). Remaining: {when}."
    accept = request.headers.get("accept", "")
    if request.url.path.startswith("/api") or "application/json" in accept:
        return JSONResponse(
            {"error": "ip_banned", "until": until, "reason": ban.get("reason")},
            status_code=403,
        )
    return PlainTextResponse(msg, status_code=403)


MIDDLEWARE = (
    Middleware(LocaleMiddleware),
    Middleware(BaseHTTPMiddleware, dispatch=ban_middleware),
    Middleware(BaseHTTPMiddleware, dispatch=api_request_log_middleware),
    Middleware(BaseHTTPMiddleware, dispatch=security_headers_middleware),
    Middleware(BaseHTTPMiddleware, dispatch=rate_limit_middleware),
    Middleware(BaseHTTPMiddleware, dispatch=auth_middleware),
)
