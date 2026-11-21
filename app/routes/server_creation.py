"""Create servers and import existing Factorio directories."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import urllib.parse
import uuid as uuid_lib
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from .. import factorio_mods, server_data
from ..db import audit, get_db
from ..factorio_versions import VERSION_RE, available_versions
from ..i18n import tr
from ..panel_config import has_factorio_credentials
from ..server_data import NAME_RE, default_server_settings, unique_conflict_message
from ..server_files import humanize_size
from ..server_saves import LOADED_SAVE_NAME


router = APIRouter()


@router.get("/new")
def new_server_form(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "server_new.html",
        {"page": "server_new",
         "versions": available_versions(),
         "has_creds": has_factorio_credentials()},
    )


@router.get("/new/check")
def new_server_check(name: str = "", port: str = "",
                     db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Реалтайм-валидация для формы создания. Проверяет занятость name/port
    БЕЗ создания записи. Используется JS'ом по debounced input. Возвращает:
        {name: {ok: bool, conflict: 'имя занято'/...},
         port: {ok: bool, conflict: '...'}}
    Не находит коллизий с orphan-папками — это сознательно: orphan'ы
    импортируются через /servers/import, а не создаются заново."""
    out: dict = {"name": {"ok": True}, "port": {"ok": True}}
    name = (name or "").strip()
    port_s = (port or "").strip()
    if name:
        # Те же ограничения что в форме: до 40 символов
        if len(name) > 40:
            out["name"] = {"ok": False, "conflict": tr("servers.name_too_long")}
        else:
            row = db.execute(
                "SELECT name FROM servers WHERE LOWER(name) = LOWER(?) LIMIT 1",
                (name,),
            ).fetchone()
            if row:
                out["name"] = {"ok": False, "conflict": tr("servers.name_taken_by_server", name=row['name'])}
    if port_s:
        try:
            p = int(port_s)
        except ValueError:
            out["port"] = {"ok": False, "conflict": tr("servers.port_not_number")}
        else:
            if p < 1024 or p > 65535:
                out["port"] = {"ok": False, "conflict": tr("servers.port_out_of_range_short")}
            else:
                row = db.execute(
                    "SELECT name FROM servers WHERE port = ? LIMIT 1", (p,),
                ).fetchone()
                if row:
                    out["port"] = {"ok": False, "conflict": tr("servers.port_taken_by_server", name=row['name'])}
    return out


def _scan_orphan_dirs(known_uuids: set[str]) -> list[dict]:
    """Поддиректории servers/ без записи в БД. UUID-ом считаем имя папки.
    Подсасываем meta.json если есть — оттуда подсказки name/version/port."""
    if not server_data.SERVERS_DIR.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(server_data.SERVERS_DIR.iterdir()):
        if d.name in known_uuids:
            continue
        try:
            d = server_data.direct_server_dir(server_data.SERVERS_DIR, d.name)
            server_data.validate_server_tree(d)
        except ValueError:
            continue
        guess: dict = {"dir_name": d.name, "name": d.name, "version": "", "port": 34197}
        meta_p = d / "meta.json"
        if meta_p.is_file():
            try:
                m = json.loads(meta_p.read_text(encoding="utf-8"))
                if isinstance(m, dict):
                    if isinstance(m.get("name"), str):
                        guess["name"] = m["name"]
                    if isinstance(m.get("version"), str):
                        guess["version"] = m["version"]
                    if isinstance(m.get("port"), int):
                        guess["port"] = m["port"]
            except (json.JSONDecodeError, OSError):
                pass
        # Если в meta нет — попробуем server-settings.json для имени
        if guess["name"] == d.name:
            ss = d / "config" / "server-settings.json"
            if ss.is_file():
                try:
                    s = json.loads(ss.read_text(encoding="utf-8"))
                    if isinstance(s, dict) and isinstance(s.get("name"), str):
                        guess["name"] = s["name"]
                except (json.JSONDecodeError, OSError):
                    pass
        # Размер папки — для подсказки «реальный» это сервер или мусор
        size = 0
        for p in d.rglob("*"):
            try:
                if p.is_file():
                    size += p.stat().st_size
            except OSError:
                continue
        guess["size_h"] = humanize_size(size)
        out.append(guess)
    return out


@router.get("/import")
def import_form(request: Request, db: sqlite3.Connection = Depends(get_db)):
    known = {r["uuid"] for r in db.execute("SELECT uuid FROM servers").fetchall()}
    orphans = _scan_orphan_dirs(known)
    return request.app.state.templates.TemplateResponse(
        request, "server_import.html",
        {
            "page": "server_new",
            "orphans": orphans,
            "versions": available_versions(),
            "page_msg": (request.query_params.get("msg") or "").strip() or None,
            "page_err": (request.query_params.get("err") or "").strip() or None,
        },
    )


@router.post("/import")
def import_adopt(request: Request,
                 dir_name: str = Form(...),
                 name: str = Form(...),
                 version: str = Form(...),
                 port: int = Form(...),
                 runtime: str = Form("docker"),
                 db: sqlite3.Connection = Depends(get_db)):
    """«Усыновить» существующую папку servers/<dir_name>/ — добавить запись в БД,
    приведя dir_name к UUID. Если dir_name не валидный UUID — переименуем папку
    в новый UUID и подсунем его в БД."""
    name = name.strip()
    if not NAME_RE.match(name):
        raise HTTPException(400, tr("servers.name_format"))
    if not VERSION_RE.match(version or ""):
        raise HTTPException(400, tr("servers.version_format"))
    if not (1024 <= port <= 65535):
        raise HTTPException(400, tr("servers.port_out_of_range"))
    if runtime != "docker":
        raise HTTPException(400, tr("servers.unsupported_runtime"))
    try:
        src_dir = server_data.direct_server_dir(server_data.SERVERS_DIR, dir_name)
        server_data.validate_server_tree(src_dir)
    except ValueError:
        raise HTTPException(400, tr("servers.forbidden_path", path=dir_name))
    dir_name = src_dir.name
    if db.execute("SELECT 1 FROM servers WHERE name = ?", (name,)).fetchone():
        raise HTTPException(409, tr("servers.name_taken", name=name))
    if db.execute("SELECT 1 FROM servers WHERE port = ?", (port,)).fetchone():
        raise HTTPException(409, tr("servers.port_taken", port=port))

    # Если имя папки — валидный UUID, используем как есть; иначе переименовываем.
    try:
        uuid_lib.UUID(dir_name)
        new_uuid = dir_name
    except ValueError:
        new_uuid = str(uuid_lib.uuid4())
    target = src_dir.parent / new_uuid
    try:
        meta = json.loads((src_dir / "meta.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update({"uuid": new_uuid, "name": name, "version": version,
                 "port": port, "runtime": runtime})

    # Reserve the UUID/name/port before modifying the operator's source directory.
    try:
        inserted = db.execute(
            "INSERT INTO servers (uuid, name, version, port, runtime) SELECT ?, ?, ?, ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM servers WHERE uuid = ? COLLATE NOCASE)",
            (new_uuid, name, version, port, runtime, dir_name),
        )
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, unique_conflict_message(e))
    if inserted.rowcount != 1:
        raise HTTPException(409, tr("servers.server_already_exists", name=dir_name))
    if target != src_dir:
        try:
            src_dir.rename(target)
        except OSError as e:
            raise HTTPException(500, tr("servers.import_rename_failed", detail=e))
    try:
        with tempfile.TemporaryDirectory(dir=target) as staging:
            metadata = Path(staging) / "meta.json"
            metadata.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            if (target / "meta.json").is_file():
                shutil.copymode(target / "meta.json", metadata)
            metadata.replace(target / "meta.json")
    except OSError:
        if target != src_dir:
            target.rename(src_dir)
        raise
    audit(db, "server.import", new_uuid,
          summary=f"adopt servers/{dir_name}/ → «{name}» v{version} :{port}")
    db.commit()
    return RedirectResponse(
        url=f"/servers/{new_uuid}?msg=" + urllib.parse.quote(
            tr("servers.import_complete", directory=dir_name, name=name)
        ),
        status_code=303,
    )


MAP_PRESETS = {
    "default", "rich-resources", "marathon",
    "death-world", "death-world-marathon",
    "rail-world", "ribbon-world",
}

# Моды которые идут в комплекте с factoriotools/factorio образом и НЕ качаются
# с mod portal'а. Если в сейве встречается только это — токены не нужны.
# (base — vanilla; остальные — DLC Space Age, кладутся в образе если он pulled.)
BUILTIN_MODS = {"base", "elevated-rails", "quality", "space-age"}


def _non_builtin_mods(mods_list: list[dict]) -> list[str]:
    """Имена модов из сейва, которые НЕ идут с образом и требуют скачивания."""
    return sorted({
        m["name"] for m in (mods_list or [])
        if m.get("name") and m["name"] not in BUILTIN_MODS
    })


def _map_gen_template(seed: int | None, preset: str, peaceful: bool) -> dict | None:
    """Минимальный map-gen-settings.json. Factorio дополнит остальные поля
    дефолтами при `factorio --create`. None = ничего не пишем (полный default)."""
    out: dict = {}
    if preset and preset != "default":
        out["preset"] = preset
    if seed:
        out["seed"] = int(seed)
    if peaceful:
        out["peaceful_mode"] = True
    return out or None


@router.post("/new")
async def create_server(
    request: Request,
    name: str = Form(...),
    version: str = Form(...),
    port: int = Form(34197),
    password: str = Form(""),
    description: str = Form(""),
    max_players: int = Form(0),
    visibility: str = Form("lan"),
    map_preset: str = Form("default"),
    map_seed: str = Form(""),
    peaceful_mode: str = Form(""),
    initial_save: UploadFile | None = File(None),
    db: sqlite3.Connection = Depends(get_db),
):
    name = name.strip()
    version = (version or "").strip()
    if not NAME_RE.match(name):
        raise HTTPException(400, tr("servers.name_format"))
    if visibility not in ("public", "lan", "none"):
        raise HTTPException(400, tr("servers.visibility_invalid"))
    if not (1024 <= port <= 65535):
        raise HTTPException(400, tr("servers.port_out_of_range"))
    if not VERSION_RE.match(version):
        raise HTTPException(400, tr("servers.version_format_example"))
    if map_preset not in MAP_PRESETS:
        raise HTTPException(400, tr("servers.map_preset_invalid", preset=repr(map_preset)))
    seed_int: int | None = None
    if (map_seed or "").strip():
        try:
            seed_int = int(map_seed)
            if not (0 <= seed_int <= 4_294_967_295):
                raise ValueError
        except ValueError:
            raise HTTPException(400, tr("servers.seed_invalid"))
    peaceful = (peaceful_mode or "").strip().lower() in ("1", "true", "on", "yes")

    # Уникальность имени и порта на уровне БД (не в файлах) — простая проверка.
    if db.execute("SELECT 1 FROM servers WHERE name = ?", (name,)).fetchone():
        raise HTTPException(409, tr("servers.server_already_exists", name=name))
    if db.execute("SELECT 1 FROM servers WHERE port = ?", (port,)).fetchone():
        raise HTTPException(409, tr("servers.port_taken_by_another_server", port=port))

    # ----- initial_save: валидируем и определяем версию ДО создания папок,
    # чтобы при ошибке не оставлять пустую papку servers/<uuid>/.
    save_tmp: Path | None = None
    if initial_save is not None and (initial_save.filename or "").strip():
        # Стримим в tmp, чтобы не держать многомегабайтный сейв в памяти.
        import tempfile
        fd, tmp_path = tempfile.mkstemp(prefix="roboport-save-", suffix=".zip")
        try:
            with open(fd, "wb") as out:
                while chunk := await initial_save.read(1 << 20):
                    out.write(chunk)
            save_tmp = Path(tmp_path)
            if not zipfile.is_zipfile(save_tmp):
                save_tmp.unlink(missing_ok=True)
                raise HTTPException(400, tr("servers.upload_save_invalid_zip"))
            # Версия из base-мода (=версия игры). Если не нашли — оставляем пользовательскую.
            mods_in_save = factorio_mods.parse_save_mods(save_tmp)
            base = next((m for m in mods_in_save if m["name"] == "base"), None)
            if base:
                save_v = base["version"]
                if save_v != version:
                    # Доверяем сейву: иначе Factorio просто откажется загружать.
                    version = save_v
            # Pre-flight: если сейв требует моды с portal'а, а креденшалов нет —
            # блокируем создание ДО того как захламить папку servers/<uuid>/.
            # Иначе install_missing молча отдаст failed[] и юзер узнает про
            # отсутствие модов только когда сервер не запустится.
            need = _non_builtin_mods(mods_in_save)
            if need and not has_factorio_credentials():
                save_tmp.unlink(missing_ok=True)
                shown = ", ".join(need[:8]) + (tr("servers.mod_count_more", count=len(need) - 8) if len(need) > 8 else "")
                raise HTTPException(
                    400,
                    tr(
                        "servers.save_requires_mods_credentials",
                        count=len(need),
                        mods=shown,
                    ),
                )
        except HTTPException:
            raise
        except Exception as e:
            if save_tmp:
                save_tmp.unlink(missing_ok=True)
            raise HTTPException(400, tr("servers.save_processing_failed", detail=e))

    sid = str(uuid_lib.uuid4())
    sdir = server_data.SERVERS_DIR / sid
    (sdir / "mods").mkdir(parents=True, exist_ok=True)
    (sdir / "saves").mkdir(parents=True, exist_ok=True)
    (sdir / "config").mkdir(parents=True, exist_ok=True)

    settings = default_server_settings(
        name=name, description=description, password=password, max_players=max_players,
        visibility=visibility, verify_users=visibility == "public", tags=[],
    )
    ssp = sdir / "config" / "server-settings.json"
    ssp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    # 600: содержит token (копия из .env) — не должен быть world-readable
    try:
        ssp.chmod(0o600)
    except OSError:
        pass

    # map-gen-settings.json — только если сейв не загружен и пользователь что-то
    # выбрал. Иначе образ создаст карту по полному дефолту.
    if save_tmp is None:
        mg = _map_gen_template(seed_int, map_preset, peaceful)
        if mg is not None:
            (sdir / "config" / "map-gen-settings.json").write_text(
                json.dumps(mg, indent=2, ensure_ascii=False), encoding="utf-8",
            )

    meta = {
        "uuid": sid, "name": name, "version": version, "port": port,
        "runtime": "docker", "visibility": visibility,
    }
    (sdir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Положим сейв на место. Образ при первом старте увидит default.zip и
    # загрузит его вместо генерации новой карты.
    mods_summary: dict | None = None
    if save_tmp is not None:
        shutil.move(str(save_tmp), sdir / "saves" / LOADED_SAVE_NAME)
        # Подтянем моды, на которые ссылается сейв. Без этого Factorio не
        # запустит карту: builtin'ы пропускаются, для остального нужен
        # FACTORIO_USERNAME/TOKEN — install_missing вернёт failed без них.
        wanted = factorio_mods.parse_save_mods(sdir / "saves" / LOADED_SAVE_NAME)
        if wanted:
            try:
                mods_summary = factorio_mods.install_missing(
                    wanted, sdir / "mods", factorio_mods.factorio_major_minor(version),
                )
                # Включим всё что встало в mod-list.json
                state = {m["name"]: True for m in wanted}
                state["base"] = True  # base нельзя выключить
                factorio_mods.write_modlist(sdir, state)
            except Exception as e:
                mods_summary = {"installed": [], "skipped": [], "failed": [
                    {"name": "(install_missing)", "reason": str(e)[:80]}
                ]}

    try:
        db.execute(
            "INSERT INTO servers (uuid, name, version, port, runtime) VALUES (?, ?, ?, ?, ?)",
            (sid, name, version, port, "docker"),
        )
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, unique_conflict_message(e))
    audit(db, "server.create", sid,
          summary=f"create «{name}» v{version} :{port}"
                  + (" + initial save" if save_tmp is not None else ""))
    db.commit()

    # Сводку отдаём в query — server_detail покажет её как флэш-сообщение.
    qs = ""
    if mods_summary is not None:
        ok = len(mods_summary.get("installed") or [])
        skip = len(mods_summary.get("skipped") or [])
        fail = mods_summary.get("failed") or []
        if fail:
            names = ", ".join(f["name"] for f in fail[:5])
            more = "" if len(fail) <= 5 else f" (+{len(fail) - 5})"
            qs = "?err=" + urllib.parse.quote(
                tr(
                    "servers.save_mod_install_failed",
                    installed=ok,
                    skipped=skip,
                    failed=len(fail),
                    names=names,
                    more=more,
                )
            )
        else:
            qs = "?msg=" + urllib.parse.quote(
                tr("servers.save_mod_install_complete", installed=ok, skipped=skip)
            )
    return RedirectResponse(url=f"/servers/{sid}{qs}", status_code=303)
