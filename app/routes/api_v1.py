"""Public API entry point. Subrouters retain the v1 prefix, tags, and order."""
from fastapi import APIRouter

from .api import files, mods, panel_updates, read, runtime, saves, schedules, servers, webhooks
from .api.auth import ApiAuthError, require_api_key

__all__ = ["ApiAuthError", "require_api_key", "router"]

router = APIRouter(prefix="/api/v1", tags=["api-v1"])
router.include_router(read.router)
router.include_router(runtime.router)
router.include_router(schedules.router)
router.include_router(servers.router)
router.include_router(files.router)
router.include_router(mods.router)
router.include_router(saves.router)
router.include_router(webhooks.router)
router.include_router(panel_updates.router)
