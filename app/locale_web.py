"""Language negotiation for HTML, browser actions and WebSocket messages."""
import json
from functools import lru_cache
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection

from . import i18n
from .routes.auth_routes import _is_https, _safe_next

router = APIRouter(include_in_schema=False)


class LocaleMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"} or scope["path"].startswith(("/static/", "/locales/")):
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        lang = i18n.choose_language(connection.cookies.get(i18n.COOKIE), connection.headers.get("accept-language", ""))
        connection.state.locale = lang

        async def localized_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if headers.get("content-type", "").startswith(("text/html", "text/plain", "application/json")):
                    headers["content-language"] = lang
                    headers.add_vary_header("Accept-Language, Cookie")
            await send(message)

        with i18n.use_language(lang):
            await self.app(scope, receive, localized_headers)


def template_context(request: Request):
    catalog = i18n.languages()
    code = getattr(request.state, "locale", i18n.current_language())
    chosen = request.cookies.get(i18n.COOKIE)
    query = str(request.url.query)
    return {
        "locale_code": code, "locale_name": catalog[code].name, "locale_dir": catalog[code].direction,
        "locale_version": catalog["en"].version + catalog[code].version,
        "locale_options": [(k, v.name) for k, v in catalog.items()],
        "locale_choice": chosen if chosen in catalog else "auto",
        "locale_next": request.url.path + ("?" + query if query else ""),
        # A POST can be a one-time secret. Switching must never replay that POST.
        "locale_can_switch": request.method == "GET" or request.url.path in
                             {"/login", "/setup", "/login/2fa", "/recovery", "/stepup"},
    }


@lru_cache(maxsize=128)
def _browser_payload(code):
    messages = {**i18n.languages()["en"].messages, **i18n.languages()[code].messages}
    return "window.roboportMessages=" + json.dumps(messages, ensure_ascii=False, separators=(",", ":")) + ";"


@router.get("/locales/{code}/client.js")
def browser_catalog(code: str):
    if code not in i18n.languages():
        raise HTTPException(status_code=404)
    return Response(_browser_payload(code), media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=86400", "Content-Language": code})


@router.post("/language")
def select_language(request: Request, code: str = Form(...), next: str = Form("/")):
    if code != "auto" and code not in i18n.languages():
        raise HTTPException(status_code=400)
    # This public preference is also available before the first administrator exists.
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and urlsplit(origin).netloc != request.headers.get("host")):
        raise HTTPException(status_code=403)
    target = _safe_next(next) if "\\" not in next else "/"
    response = RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})
    options = dict(path="/", httponly=True, secure=_is_https(request), samesite="lax")
    if code == "auto":
        response.delete_cookie(i18n.COOKIE, **options)
    else:
        response.set_cookie(i18n.COOKIE, code, max_age=365 * 86400, **options)
    return response
