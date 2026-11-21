"""FastAPI-приложение: панель управления Factorio-серверами.

  uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from . import __author__, __url__, locale_web
from .csrf import require_session_csrf
from .db import connect
from .http_errors import EXCEPTION_HANDLERS
from .lifespan import lifespan
from .middleware import MIDDLEWARE
from .routes import api_keys, api_v1, auth_routes, dashboard, metrics, modconfig, mods, server_console, servers, settings, updates, webhooks
from .templating import create_templates, humantime as humantime, unixtime as unixtime

ROOT = Path(__file__).parent


app = FastAPI(
    lifespan=lifespan,
    dependencies=[Depends(require_session_csrf)],
    middleware=MIDDLEWARE,
    exception_handlers=EXCEPTION_HANDLERS,
    title="Roboport Controller API",
    description=(
        "Manage Factorio servers, saves, mods and webhooks. Use a panel API key in "
        "the `Authorization: Bearer` or `X-API-Key` header. Keys can have independent "
        "`read`, `command`, `control` and `admin` permissions. "
        f"[API guide]({__url__}/blob/main/docs/api.md)"
    ),
    version="1.0.0",
    contact={
        "name": __author__,
        "url": __url__,
    },
    license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
)


# OpenAPI-схема: фильтруем только до /api/v1/*. Без этого FastAPI генерит
# спеку со ВСЕМИ ~140 роутами панели (включая HTML-формы /servers/<uuid>,
# /settings, etc), которые требуют cookie-сессии и не доступны через ключ.
# Раскрытие этого списка в публичной OpenAPI — bloat + минор-leak структуры
# UI. Кеш `app.openapi_schema` исп. FastAPI'ем сам, мы только override-им
# generation.
def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title, version=app.version,
        description=app.description,
        routes=[route for route in app.routes if route.path.startswith("/api/v1/")],
        contact=app.contact, license_info=app.license_info,
    )
    # Заодно подсветим что для большинства эндпоинтов нужен Bearer-токен.
    schema.setdefault("components", {}).setdefault("securitySchemes", {})[
        "BearerAuth"
    ] = {"type": "http", "scheme": "bearer", "bearerFormat": "rpk_<...>"}
    schema.setdefault("components", {}).setdefault("securitySchemes", {})[
        "ApiKeyAuth"
    ] = {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    # Public health and version operations override the default with an empty list.
    schema["security"] = [{"BearerAuth": []}, {"ApiKeyAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]

app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.state.templates = create_templates(ROOT)

app.include_router(auth_routes.router)
app.include_router(api_keys.router)
app.include_router(webhooks.router)
app.include_router(api_v1.router)
app.include_router(dashboard.router)
app.include_router(servers.router)
app.include_router(server_console.router)
app.include_router(mods.router)
app.include_router(metrics.router)
app.include_router(modconfig.router)
app.include_router(updates.router)
app.include_router(settings.router)
app.include_router(locale_web.router)


@app.get("/health")
def health():
    """Public liveness check for uptime monitors."""
    return {"status": "ok", "ts": int(time.time())}


@app.get("/health/sampler")
def sampler_health():
    """Collector status shown in the signed-in panel footer."""
    last_ts: int | None = None
    try:
        with connect() as conn:
            row = conn.execute("SELECT MAX(ts) AS ts FROM metrics_samples").fetchone()
            if row and row["ts"]:
                last_ts = int(row["ts"])
    except Exception:
        pass
    return {
        "status": "ok",
        "ts": int(time.time()),
        "sampler_alive": bool(getattr(app.state, "sampler_task", None)
                              and not app.state.sampler_task.done()),
        "last_sample_ts": last_ts,
    }
