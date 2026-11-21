"""Версии Factorio: страница /updates.

В Docker-сетапе «версия» = тег образа factoriotools/factorio.
- Список доступных тегов берём из общего часового кэша версий Factorio.
- Какие из них уже загружены локально — `docker images factoriotools/factorio --format json`.
- Pull/remove — `docker pull <tag>` / `docker image rm <tag>`.
"""
from __future__ import annotations

import json
import subprocess
import urllib.parse
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..i18n import tr
from ..factorio_versions import available_versions

router = APIRouter()

IMAGE = "factoriotools/factorio"
PULL_TIMEOUT_SEC = 600   # 10 минут на pull — больших образов почти нет, но запас


def _docker(args: list[str], *, check: bool = False, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, check=check, timeout=timeout,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _pulled_images() -> dict[str, dict[str, Any]]:
    """Что у нас уже стоит локально. Ключ — тег, значение — {id, size, created}.
    Возвращает {} если docker недоступен."""
    out: dict[str, dict[str, Any]] = {}
    try:
        p = _docker(
            ["docker", "images", IMAGE, "--format", "{{json .}}"],
            check=False, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return out
    if p.returncode != 0:
        return out
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        tag = d.get("Tag") or ""
        if not tag or tag == "<none>":
            continue
        out[tag] = {
            "id": (d.get("ID") or "")[:12],
            "size": d.get("Size") or "",
            "created": d.get("CreatedSince") or d.get("CreatedAt") or "",
        }
    return out


def _all_versions(pulled: dict[str, dict]) -> list[dict]:
    """Объединяем известные теги с Docker Hub + всё что локально (на случай если у юзера
    стоит тег, которого нет в каталоге версий, например dev/beta).
    Сортируем по убыванию semver, локальные но неизвестные — внизу."""
    hub = available_versions()  # может быть пусто если нет интернета
    seen: set[str] = set()
    out: list[dict] = []
    for v in hub:
        seen.add(v)
        info = pulled.get(v)
        out.append({
            "version": v,
            "pulled": info is not None,
            "size": info.get("size") if info else None,
            "id":   info.get("id")   if info else None,
            "from_hub": True,
        })
    # Локальные, которых нет в hub-листе — отдельной группой
    for tag, info in pulled.items():
        if tag in seen:
            continue
        out.append({
            "version": tag,
            "pulled": True,
            "size": info["size"], "id": info["id"],
            "from_hub": False,
        })
    return out


def _redirect(*, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    qs = ""
    if msg:
        qs = f"?msg={urllib.parse.quote(msg)}"
    elif err:
        qs = f"?err={urllib.parse.quote(err)}"
    return RedirectResponse(url=f"/updates{qs}", status_code=303)


def _group_by_minor(versions: list[dict]) -> list[dict]:
    """Группируем по major.minor: 2.0.76 → '2.0'. Внутри группы — patches по убыванию.
    Группы упорядочены: новейшие сверху, особняком группа 'локальные' (теги без hub-описи)."""
    groups: dict[str, list[dict]] = {}
    locals_group: list[dict] = []
    for v in versions:
        ver = v["version"]
        try:
            major, minor, _patch = ver.split(".", 2)
            key = f"{major}.{minor}"
        except ValueError:
            locals_group.append(v)
            continue
        groups.setdefault(key, []).append(v)
    out: list[dict] = []
    # Сортировка ключей по semver-кортежу minor desc
    for key in sorted(groups.keys(), key=lambda k: tuple(int(x) for x in k.split(".")), reverse=True):
        items = groups[key]
        any_pulled = any(it["pulled"] for it in items)
        out.append({
            "minor": key,
            "items": items,
            "count": len(items),
            "pulled_count": sum(1 for it in items if it["pulled"]),
            "any_pulled": any_pulled,
            "newest": items[0]["version"] if items else "",
        })
    if locals_group:
        out.append({
            "minor": tr("updates.local_versions"), "items": locals_group, "count": len(locals_group),
            "pulled_count": len(locals_group), "any_pulled": True,
            "newest": locals_group[0]["version"] if locals_group else "",
        })
    return out


@router.get("/updates")
def updates_page(request: Request):
    pulled = _pulled_images()
    versions = _all_versions(pulled)
    groups = _group_by_minor(versions)
    msg = (request.query_params.get("msg") or "").strip() or None
    err = (request.query_params.get("err") or "").strip() or None
    return request.app.state.templates.TemplateResponse(
        request, "updates.html",
        {
            "page": "updates",
            "image": IMAGE,
            "groups": groups,
            "total_versions": len(versions),
            "pulled_count": sum(1 for v in versions if v["pulled"]),
            "msg": msg, "err": err,
        },
    )


@router.post("/updates/pull")
def updates_pull(version: str = Form(...)):
    version = version.strip()
    if not version:
        raise HTTPException(400, tr("updates.version_required"))
    # Простая защита от инъекции в docker CLI: не пускаем пробелы и метасимволы.
    if not all(c.isalnum() or c in ".-_" for c in version):
        raise HTTPException(400, tr("updates.invalid_version", version=repr(version)))
    try:
        p = _docker(["docker", "pull", f"{IMAGE}:{version}"],
                    check=False, timeout=PULL_TIMEOUT_SEC)
    except FileNotFoundError:
        return _redirect(err=tr("updates.docker_cli_not_found_path"))
    except subprocess.TimeoutExpired:
        return _redirect(err=tr("updates.image_pull_timeout", version=version, timeout=PULL_TIMEOUT_SEC))
    if p.returncode != 0:
        return _redirect(err=tr(
            "updates.image_pull_failed", version=version, detail=(p.stderr or p.stdout).strip()[:300],
        ))
    return _redirect(msg=tr("updates.image_pulled", image=IMAGE, version=version))


@router.post("/updates/remove")
def updates_remove(version: str = Form(...)):
    version = version.strip()
    if not all(c.isalnum() or c in ".-_" for c in version):
        raise HTTPException(400, tr("updates.invalid_version", version=repr(version)))
    try:
        p = _docker(["docker", "image", "rm", f"{IMAGE}:{version}"], check=False, timeout=30)
    except FileNotFoundError:
        return _redirect(err=tr("updates.docker_cli_not_found"))
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip()[:300]
        return _redirect(err=tr("updates.image_remove_failed", version=version, detail=msg))
    return _redirect(msg=tr("updates.image_removed", image=IMAGE, version=version))
