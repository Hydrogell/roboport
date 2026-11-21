"""Installed mods and the Mod Portal catalog."""
from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ... import auth as auth_lib
from ... import factorio_mods as mod_service
from ...db import audit, get_db
from ...i18n import tr
from ...server_data import spec_from_row
from .auth import ApiAuthError, require_api_key
from .common import load_server

router = APIRouter()


class ModInstallRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100,
                      description="Mod name из mod portal (например 'rate-calculator')")
    version: str | None = Field(None,
                                pattern=r"^\d+\.\d+\.\d+$",
                                description="Конкретная версия; null = latest для текущего factorio")


class ModToggleRequest(BaseModel):
    enabled: bool


class ModBulkUpdateRequest(BaseModel):
    names: list[str] | None = Field(
        None,
        description="Список mod name для обновления. null или пусто + all=true → "
                    "обновить все где has_update=true.",
    )
    all: bool = Field(False, description="Обновить все моды с has_update=true")
    sync_first: bool = Field(
        False,
        description="Перед обновлением пере-синкать кэш mod portal'а (~30с). "
                    "Полезно если кэш старый и в нём ещё нет последних релизов.",
    )


@router.get("/servers/{uuid}/mods", summary="Установленные моды")
def api_mods_list(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Список .zip модов в servers/<uuid>/mods/ + enabled-state из mod-list.json.
    base/quality/space-age/elevated-rails — встроенные в Factorio, помечены."""
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    installed = mod_service.list_installed(spec.data_dir)
    state = mod_service.read_modlist(spec.data_dir)
    out = []
    for m in installed:
        out.append({
            "name": m.get("name"),
            "version": m.get("version"),
            "filename": m.get("filename"),
            "size": m.get("size"),
            "enabled": state.get(m.get("name"), True),
            "builtin": m.get("name") in mod_service.BUILTIN_MODS,
        })
    return {"uuid": uuid, "mods": out, "count": len(out)}


@router.post("/servers/{uuid}/mods", summary="Установить мод с mod portal")
def api_mods_install(
    uuid: str,
    body: ModInstallRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Качает мод с factorio.com mod portal и кладёт в servers/<uuid>/mods/.
    Требует FACTORIO_USERNAME/FACTORIO_TOKEN в .env (или /settings → Factorio.com).
    `version=null` → latest для текущей factorio-версии сервера."""
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    fv = mod_service.factorio_major_minor(row["version"])
    rel = mod_service.find_release(body.name, body.version, fv)
    if not rel:
        raise ApiAuthError(404, "mod_not_found",
                           tr(
                               "api_v1.mod_not_found",
                               name=repr(body.name),
                               version=body.version,
                               factorio=fv,
                           ))
    # install_missing работает со списком — даём один.
    summary = mod_service.install_missing(
        wanted=[{"name": body.name, "version": rel.get("version")}],
        target_dir=spec.data_dir / "mods",
        factorio_version=fv,
    )
    if summary.get("failed"):
        f = summary["failed"][0]
        raise ApiAuthError(502, "install_failed",
                           f"{f.get('name')}: {f.get('reason')}")
    audit(db, "mod.install", uuid,
          summary=f"api_key={key.name}: {body.name} v{rel.get('version')}")
    db.commit()
    return {"uuid": uuid, "name": body.name, "version": rel.get("version"),
            "installed": summary.get("installed", []),
            "skipped": summary.get("skipped", [])}


@router.delete("/servers/{uuid}/mods/{mod_name}", summary="Удалить мод")
def api_mods_remove(
    uuid: str,
    mod_name: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Удаляет .zip мода + убирает из mod-list.json. Идемпотентно."""
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    mods_dir = spec.data_dir / "mods"
    # Найти zip начинающийся с mod_name + '_'
    removed: list[str] = []
    if mods_dir.is_dir():
        for f in mods_dir.iterdir():
            if f.is_file() and f.name.startswith(mod_name + "_") and f.name.endswith(".zip"):
                f.unlink()
                removed.append(f.name)
    state = mod_service.read_modlist(spec.data_dir)
    if mod_name in state:
        del state[mod_name]
        mod_service.write_modlist(spec.data_dir, state)
    audit(db, "mod.remove", uuid,
          summary=f"api_key={key.name}: {mod_name} (files={len(removed)})")
    db.commit()
    return {"uuid": uuid, "name": mod_name, "removed_files": removed}


@router.patch("/servers/{uuid}/mods/{mod_name}", summary="Включить/выключить мод")
def api_mods_toggle(
    uuid: str,
    mod_name: str,
    body: ModToggleRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Переключает enabled-флаг в mod-list.json. `base` всегда enabled
    (Factorio падает иначе — игнорируем попытку выключить)."""
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    state = mod_service.read_modlist(spec.data_dir)
    if mod_name == "base" and not body.enabled:
        raise ApiAuthError(400, "bad_request",
                           tr("api_v1.cannot_disable_base"))
    state[mod_name] = body.enabled
    mod_service.write_modlist(spec.data_dir, state)
    audit(db, "mod.toggle", uuid,
          summary=f"api_key={key.name}: {mod_name}={body.enabled}")
    db.commit()
    return {"uuid": uuid, "name": mod_name, "enabled": body.enabled}


@router.get("/servers/{uuid}/mods/updates", summary="Список модов с доступными обновлениями")
def api_mods_updates(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Сравнивает установленные моды с локальным кэшем mod portal'а
    (последняя синхронизация — см. cache.synced_at). Возвращает только
    моды у которых latest > installed для текущей factorio_version.

    Возвращает:
        {
            "cache": {"synced_at": int, "count": int, "stale": bool},
            "factorio_version": "2.0",
            "updates": [{"name", "title", "current", "latest", "size_kb"}, ...]
        }

    `cache.stale=true` если синк старше 24 часов — рекомендуется сначала
    POST /mods/sync.
    """
    row = load_server(db, uuid)
    sdir = spec_from_row(row).data_dir
    factorio_v = mod_service.factorio_major_minor(row["version"])
    return mod_service.available_updates(sdir, factorio_v)


@router.post("/servers/{uuid}/mods/sync", summary="Пере-синк кэша mod portal'а")
def api_mods_sync(
    uuid: str,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Скачать свежий каталог с mods.factorio.com (~12 МБ JSON, 20–40с).
    Синхронный запрос — клиент висит пока докачается. После: используй
    GET /mods/updates чтобы увидеть свежие версии."""
    load_server(db, uuid)
    try:
        payload = mod_service.refresh_catalog()
    except mod_service.PortalListingError as e:
        raise ApiAuthError(502, "portal_unreachable",
                           tr("api_v1.mod_portal_error", detail=e))
    audit(db, "mod.sync", None,
          summary=f"api_key={key.name}: count={payload['count']}")
    db.commit()
    return {"ok": True, "synced_at": payload["synced_at"],
            "count": payload["count"]}


@router.post("/servers/{uuid}/mods/bulk-update",
             summary="Обновить несколько модов сразу")
def api_mods_bulk_update(
    uuid: str,
    body: ModBulkUpdateRequest,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    key: Annotated[auth_lib.ApiKeyInfo, Depends(require_api_key("admin"))],
) -> dict:
    """Скачать и поставить новые версии для указанных модов (или для всех с
    `has_update=true`, если `all=true`). Каждый мод обрабатывается через
    install_missing — последовательно, не параллельно (mod portal rate-limit).

    Опционально (`sync_first=true`) перед обновлением пере-синкает кэш —
    полезно когда есть подозрение что в локальном кэше ещё нет последних
    релизов.

    Возвращает:
        {
            "uuid", "synced": bool, "results": [
                {"name", "from": "1.0", "to": "1.1", "ok": true},
                {"name", "from": "2.3", "to": null, "ok": false, "error": "..."},
            ]
        }
    """
    row = load_server(db, uuid)
    spec = spec_from_row(row)
    factorio_v = mod_service.factorio_major_minor(row["version"])

    # 1. Опциональный sync кэша.
    synced = False
    if body.sync_first:
        try:
            mod_service.refresh_catalog()
            synced = True
        except Exception as e:
            raise ApiAuthError(502, "portal_unreachable",
                               tr("api_v1.mod_sync_failed", detail=e))

    # 2. Определить какие моды обновлять.
    sdir = spec.data_dir
    installed = mod_service.annotate_with_updates(mod_service.list_installed(sdir), factorio_v)
    by_name = {m["name"]: m for m in installed}
    if body.all and not body.names:
        targets = [m for m in installed if m.get("has_update")]
    else:
        if not body.names:
            raise ApiAuthError(400, "bad_request",
                               tr("api_v1.mod_names_or_all_required"))
        targets = []
        for name in body.names:
            m = by_name.get(name)
            if not m:
                # Мод не установлен — пропускаем с error в результатах
                targets.append({"name": name, "_missing": True})
            else:
                targets.append(m)

    # 3. Sequential install.
    results = []
    for m in targets:
        name = m["name"]
        from_v = m.get("version")
        if m.get("_missing"):
            results.append({"name": name, "from": None, "to": None,
                            "ok": False, "error": "not_installed"})
            continue
        latest = m.get("latest_in_cache")
        if not latest or latest == from_v:
            results.append({"name": name, "from": from_v, "to": from_v,
                            "ok": True, "skipped": "no_update"})
            continue
        error = mod_service.update_installed_mod(m, latest, sdir / "mods", factorio_v)
        if error is not None:
            results.append({"name": name, "from": from_v, "to": None,
                            "ok": False, "error": error})
        else:
            results.append({"name": name, "from": from_v, "to": latest, "ok": True})

    audit(db, "mod.bulk_update", uuid,
          summary=f"api_key={key.name}: {len(results)} mods, "
                  f"ok={sum(1 for r in results if r.get('ok'))}, synced={synced}")
    db.commit()
    return {"uuid": uuid, "synced": synced, "results": results}
