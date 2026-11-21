"""API key authentication, authorization, and rate limiting."""
from __future__ import annotations

import time
from typing import Annotated

from fastapi import Header, HTTPException, Request

from ... import auth as auth_lib
from ...db import connect
from ...i18n import tr
from ..auth_routes import client_ip


# Каждый ключ имеет лимит rate_limit_per_min (default 60). Bucket хранит
# таймштампы запросов за последние 60 секунд; если их N+ — отбиваем 429.
# Сбрасывается при рестарте процесса — для соло-панели приемлемо.

_rate_buckets: dict[int, list[float]] = {}  # key_id → list of unix-ts (recent)


def _rate_limit_check(key_id: int, limit_per_min: int) -> int:
    """Проверить лимит. Возвращает 0 если ок, иначе seconds-to-wait для Retry-After.
    Side effect: добавляет текущий ts в bucket если ок."""
    now = time.time()
    cutoff = now - 60
    bucket = [ts for ts in _rate_buckets.get(key_id, []) if ts > cutoff]
    if len(bucket) >= limit_per_min:
        # Самый старый ts в окне — когда он истечёт, лимит сбросится на одну
        # позицию. Возвращаем ceil(остаток).
        oldest = bucket[0]
        return max(1, int(oldest + 60 - now) + 1)
    bucket.append(now)
    _rate_buckets[key_id] = bucket
    return 0


# FastAPI не умеет напрямую вернуть 401/403/429 при отказе из dependency без
# raise. Мы и raise'им — глобальный exception_handler в main.py видит scope-
# специфичные exception'ы и форматирует JSON.

class ApiAuthError(HTTPException):
    """Специальный HTTPException для API. Глобальные обработчики из main.py
    конвертируют его в JSON формата {error, detail}."""
    def __init__(self, status: int, error: str, detail: str = "",
                 headers: dict | None = None):
        super().__init__(status_code=status, detail=detail, headers=headers)
        self.error_code = error


def require_key_scope(key: auth_lib.ApiKeyInfo, scope: str) -> None:
    """Reject use of an authenticated key that lacks one additional scope."""
    if scope not in auth_lib.API_SCOPES_ALL:
        raise ValueError(f"unknown scope: {scope!r}")
    if scope not in key.scopes:
        raise ApiAuthError(403, "scope_denied",
                           tr(
                               "api_v1.api_key_missing_scope",
                               scope=scope,
                               scopes=sorted(key.scopes),
                           ))


def _extract_token(authorization: str | None, x_api_key: str | None) -> str | None:
    """Достать plain-key из заголовков. Bearer приоритетнее (стандарт),
    X-API-Key — для simplicity-кейсов с curl."""
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    if x_api_key:
        return x_api_key.strip()
    return None


def require_api_key(scope: str):
    """Factory для dependency. Использование:

        @router.get("/foo")
        def foo(key: ApiKeyInfo = Depends(require_api_key("read"))):
            ...

    Dependency читает заголовки, валидирует ключ, проверяет scope, проверяет
    rate-limit. На промах raise'ит ApiAuthError с понятным error/detail."""
    if scope not in auth_lib.API_SCOPES_ALL:
        raise ValueError(f"unknown scope: {scope!r}")

    def _dep(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> auth_lib.ApiKeyInfo:
        plain = _extract_token(authorization, x_api_key)
        if not plain:
            raise ApiAuthError(401, "unauthorized",
                               tr("api_v1.api_key_required"))
        ip = client_ip(request)
        with connect() as conn:
            info = auth_lib.verify_api_key(conn, plain, ip=ip)
            conn.commit()  # для UPDATE last_used
        if info is None:
            raise ApiAuthError(401, "unauthorized",
                               tr("api_v1.api_key_invalid"))
        require_key_scope(info, scope)
        wait = _rate_limit_check(info.id, info.rate_limit_per_min)
        if wait > 0:
            raise ApiAuthError(429, "rate_limited",
                               tr(
                                   "api_v1.api_key_rate_limit",
                                   limit=info.rate_limit_per_min,
                                   wait=wait,
                               ),
                               headers={"Retry-After": str(wait)})
        # Прокинем в request.state для возможного использования (audit и т.п.)
        request.state.api_key = info
        return info

    return _dep
