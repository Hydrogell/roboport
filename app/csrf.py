"""CSRF checks at the route boundary, after cookie authentication."""
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, PlainTextResponse

from .auth import csrf_token_valid
from .i18n import tr


class InvalidCsrfToken(Exception):
    pass


def _check(expected: str, supplied: object) -> None:
    if not isinstance(supplied, str) or not supplied.isascii() or not csrf_token_valid(expected, supplied):
        raise InvalidCsrfToken


async def require_session_csrf(connection: HTTPConnection):
    # This dependency also runs for WebSocket routes. Their upgrade/auth is separate.
    session = getattr(connection.state, "user", None)
    if (not isinstance(connection, Request) or session is None
            or connection.method not in {"POST", "PUT", "PATCH", "DELETE"}):
        yield
        return

    header = connection.headers.get("x-csrf-token")
    media_type = connection.headers.get("content-type", "").partition(";")[0].strip().lower()
    if header is not None or media_type not in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        _check(session.csrf_token, header)
        yield
        return

    # Share Starlette's parsed form with FastAPI and manual upload handlers.
    # Files can spool to disk and stay open until the endpoint finishes.
    async with connection.form() as form:
        _check(session.csrf_token, form.get("_csrf"))
        yield


async def csrf_error(request: Request, exc: InvalidCsrfToken):
    if request.url.path.startswith("/api") or "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {"error": "csrf_invalid", "detail": "Missing or invalid _csrf token. Reload the page to get a fresh one."},
            status_code=403,
        )
    return PlainTextResponse(tr("csrf.request_verification_failed"), status_code=403)
