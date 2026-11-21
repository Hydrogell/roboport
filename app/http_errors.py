"""HTML form, page and REST API error responses."""
from __future__ import annotations

import traceback
import urllib.parse

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import i18n
from .csrf import InvalidCsrfToken, csrf_error
from .i18n import tr
from .routes.api.auth import ApiAuthError
from .routes.auth_routes import _safe_next


def _wants_json(request: Request) -> bool:
    return (request.url.path.startswith("/api")
            or "application/json" in request.headers.get("accept", ""))


def _redirect_with_err(referer: str, err_msg: str) -> RedirectResponse:
    """Добавить ?err=... к URL (или склеить с существующей querystring)."""
    msg = err_msg[:500]
    sep = "&" if "?" in referer else "?"
    return RedirectResponse(
        url=f"{referer}{sep}err={urllib.parse.quote(msg)}",
        status_code=303,
    )


def _form_return_path(request: Request) -> str | None:
    if request.method != "POST" or _wants_json(request) or not getattr(request.state, "user", None):
        return None
    try:
        referer = urllib.parse.urlsplit(request.headers.get("referer", ""))
    except ValueError:
        return None
    if not referer.path or (referer.scheme, referer.netloc) != (request.url.scheme, request.url.netloc):
        return None
    return _safe_next(urllib.parse.urlunsplit(("", "", referer.path, referer.query, "")))


def _render_error(request: Request, status_code: int, title: str, detail: str) -> Response:
    """HTML или plain-text страница ошибки. Если шаблон сломан — текстовый fallback."""
    try:
        return request.app.state.templates.TemplateResponse(
            request, "error.html" if getattr(request.state, "user", None) else "public_error.html",
            {"page": "error", "status_code": status_code,
             "title": title, "detail": detail,
             "path": request.url.path},
            status_code=status_code, headers={"Cache-Control": "no-store"},
        )
    except Exception:
        return PlainTextResponse(
            f"{status_code} {title}\n\n{detail}",
            status_code=status_code, headers={"Cache-Control": "no-store"},
        )


async def api_v1_auth_error(request: Request, exc: ApiAuthError):
    """Ошибки API ({"error": "<code>", "detail": "..."}) — отдельный handler,
    чтобы не конвертить через generic StarletteHTTPException и сохранить
    Retry-After header для 429."""
    return JSONResponse(
        {"error": exc.error_code, "detail": str(exc.detail or "")},
        status_code=exc.status_code,
        headers=exc.headers or {},
    )


async def panel_http_exception(request: Request, exc: StarletteHTTPException):
    """raise HTTPException(...) → дружелюбная страница вместо JSON {detail}."""
    detail = str(exc.detail or "")
    default_messages = {
        "Not Found": "error.page_not_found", "Method Not Allowed": "error.method_unavailable",
        "Forbidden": "error.access_denied", "Unauthorized": "error.sign_in_required",
        "Bad Request": "error.invalid_request",
    }
    if not _wants_json(request) and detail in default_messages:
        detail = tr(default_messages[detail])
    # Для form-submit'ов с Referer — возвращаем юзера назад с err=
    referer = _form_return_path(request)
    if referer:
        return _redirect_with_err(referer, detail)
    if _wants_json(request):
        return JSONResponse(
            {"error": "HTTPException", "status": exc.status_code, "detail": detail},
            status_code=exc.status_code,
        )
    return _render_error(request, exc.status_code,
                         tr("main.error_title", status=exc.status_code), detail)


async def panel_validation_error(request: Request, exc: RequestValidationError):
    if _wants_json(request) or "text/html" not in request.headers.get("accept", ""):
        return await request_validation_exception_handler(request, exc)
    return _render_error(request, 422, tr("error.form_title"), tr("error.form_invalid"))


async def panel_unhandled(request: Request, exc: Exception):
    """Любое необработанное исключение в коде панели. Логируем traceback,
    юзеру показываем понятную плашку. Никаких 'Internal Server Error'."""
    tb = traceback.format_exc()
    print(f"[panel] UNHANDLED {type(exc).__name__} on {request.method} "
          f"{request.url.path}: {exc}\n{tb}", flush=True)

    # Starlette invokes the 500 handler outside the user middleware.
    code = getattr(request.state, "locale", i18n.current_language())
    with i18n.use_language(code):
        response = _unhandled_response(request)
    response.headers["Content-Language"] = code
    response.headers.add_vary_header("Accept-Language, Cookie")
    return response


def _unhandled_response(request: Request):
    title = tr("main.internal_error_title")
    referer = _form_return_path(request)
    if referer:
        return _redirect_with_err(referer, title)
    if _wants_json(request):
        return JSONResponse({"error": "internal_error", "detail": title}, status_code=500,
                            headers={"Cache-Control": "no-store"})
    return _render_error(request, 500, title, "")


EXCEPTION_HANDLERS = {
    InvalidCsrfToken: csrf_error,
    ApiAuthError: api_v1_auth_error,
    StarletteHTTPException: panel_http_exception,
    RequestValidationError: panel_validation_error,
    Exception: panel_unhandled,
}
