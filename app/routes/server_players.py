"""Player roles, allowlist, bans and RCON player information."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse

from ..db import get_db
from ..i18n import tr
from ..runtime import ServerSpec, get_runtime
from ..runtime.docker import DockerError, DockerRuntime
from ..server_data import spec_from_row
from .server_common import require_server, server_tab_redirect


router = APIRouter()


# Имя игрока в Factorio: a-z A-Z 0-9 . _ - . До 40 символов хватает.
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,40}$")


def safe_player_name(name: str) -> str:
    name = (name or "").strip()
    if not PLAYER_NAME_RE.match(name):
        raise HTTPException(400, tr("servers.invalid_player_name"))
    return name


def read_name_list(path: Path) -> list[str]:
    """server-adminlist.json / server-whitelist.json могут быть массивом строк
    или массивом объектов {username, ...}. Приводим к списку имён."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out: list[str] = []
    for x in data if isinstance(data, list) else []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            n = x.get("username") or x.get("name")
            if isinstance(n, str):
                out.append(n)
    return out


def _write_name_list(path: Path, names: list[str]) -> None:
    """Записать как простой массив имён — формат, который понимает Factorio.
    Дедуп case-insensitive (Factorio имена нечувствительны к регистру), сохраняем
    регистр первого вхождения. Это лечит дубли вида "Admin"/"admin", которые
    появляются когда RCON-ответ нормализует регистр, а в файле уже было оригинальное имя."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}
    for n in names:
        n = (n or "").strip()
        if not n:
            continue
        k = n.lower()
        if k not in seen:
            seen[k] = n
    out = sorted(seen.values(), key=str.lower)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")


def whitelist_paths(cfg_dir: Path) -> tuple[Path, Path]:
    """Возвращает (active, disabled). Активный файл существует ⇒ whitelist включён.
    .disabled путь хранит «выключенный» список — содержимое сохраняется при тогле."""
    active = cfg_dir / "server-whitelist.json"
    disabled = cfg_dir / "server-whitelist.json.disabled"
    return active, disabled


def _whitelist_active_or_disabled(cfg_dir: Path) -> Path:
    """Куда писать имена в whitelist чтобы НЕ менять текущее состояние тогла:
    в активный если он есть, иначе в .disabled. Если оба отсутствуют — .disabled
    (по умолчанию whitelist выключен; одиночный «+ Игрок» не должен включать его)."""
    active, disabled = whitelist_paths(cfg_dir)
    if active.exists():
        return active
    return disabled


def _read_banlist(path: Path) -> list[dict]:
    """Banlist: массив строк или массив {username, reason, address}.
    Нормализуем в [{name, reason}]."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    for x in data if isinstance(data, list) else []:
        if isinstance(x, str):
            out.append({"name": x, "reason": ""})
        elif isinstance(x, dict):
            n = x.get("username") or x.get("name")
            if isinstance(n, str):
                out.append({"name": n, "reason": x.get("reason", "") or ""})
    return out


def _write_banlist(path: Path, items: list[dict]) -> None:
    """Сохранить как [{username, reason}]. Дедуп case-insensitive по username,
    регистр первого вхождения сохраняется."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: dict[str, dict] = {}
    for i in items:
        n = (i.get("name") or "").strip()
        if not n:
            continue
        k = n.lower()
        if k not in seen:
            seen[k] = {"username": n, "reason": i.get("reason", "") or ""}
    path.write_text(
        json.dumps(sorted(seen.values(), key=lambda x: x["username"].lower()),
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _parse_players_output(text: str) -> list[dict]:
    """RCON /players отдаёт:
        Players (3):
          alice (online)
          bob
          charlie (online)
       Возвращаем [{name, online}]."""
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("Players") or line.startswith("[rcon]"):
            continue
        online = False
        if line.endswith("(online)"):
            online = True
            line = line[:-len("(online)")].strip()
        if line:
            out.append({"name": line, "online": online})
    return out


# Текст-маркеры пустых списков. Factorio в зависимости от версии шлёт разное —
# держим набор подстрок (CI), при матче возвращаем пустой список.
_EMPTY_LIST_MARKERS = (
    "the admin list is empty",
    "the banlist is empty",
    "the ban list is empty",
    "the whitelist is empty",
    "no admins",
    "no banned",
)


def _parse_simple_name_list(text: str) -> list[str] | None:
    """RCON /admins или /whitelist get — построчно имена. Header-строки и
    пустой-список маркеры пропускаем. None если ничего распознать не смогли
    (фоллбек на чтение файла), [] если список реально пустой."""
    if text is None:
        return None
    raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not raw_lines:
        return []
    # Если это пустой-список маркер — значит список реально пустой.
    joined = " ".join(raw_lines).lower()
    if any(m in joined for m in _EMPTY_LIST_MARKERS):
        return []
    out: list[str] = []
    for line in raw_lines:
        # Пропускаем header вроде "Admin list (2):" или "Whitelist (3):"
        if line.lower().startswith(("admin list", "whitelist", "ban list", "banlist")):
            continue
        # Имя может быть с (online), (offline) — снимаем
        for suf in ("(online)", "(offline)"):
            if line.endswith(suf):
                line = line[:-len(suf)].strip()
        if line and not line.startswith("["):  # игнор [rcon] префиксов
            out.append(line)
    return out


def _parse_banlist_output(text: str) -> list[dict] | None:
    """RCON /banlist get — каждая строка вида 'name' или 'name (banned by Y for: REASON)'.
    Возвращаем [{name, reason}] или None если ничего не распознали."""
    if text is None:
        return None
    raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not raw_lines:
        return []
    joined = " ".join(raw_lines).lower()
    if any(m in joined for m in _EMPTY_LIST_MARKERS):
        return []
    out: list[dict] = []
    for line in raw_lines:
        if line.lower().startswith(("ban list", "banlist")):
            continue
        # Пытаемся вытащить причину из «(banned by X for: REASON)» или «(reason: X)»
        reason = ""
        name = line
        if "(" in line and line.endswith(")"):
            head, _, paren = line.rpartition("(")
            name = head.strip()
            inner = paren[:-1]
            if "for:" in inner:
                reason = inner.split("for:", 1)[1].strip()
            elif "reason:" in inner.lower():
                reason = inner.split(":", 1)[1].strip()
        if name and not name.startswith("["):
            out.append({"name": name, "reason": reason})
    return out


def player_view(spec: ServerSpec, rt_status) -> dict:
    cfg_dir = spec.data_dir / "config"
    # Базовый источник истины — JSON-файлы (работает и offline). Для
    # running-сервера ниже перезапишем актуальными данными из RCON,
    # потому что Factorio пишет в файл асинхронно и UI иначе видит
    # старые значения 1-2 секунды после promote/ban.
    admins = read_name_list(cfg_dir / "server-adminlist.json")
    wl_active, wl_disabled = whitelist_paths(cfg_dir)
    whitelist_enabled = wl_active.exists()
    wl_path = wl_active if whitelist_enabled else wl_disabled
    whitelist = read_name_list(wl_path) if wl_path.exists() else []
    bans = _read_banlist(cfg_dir / "server-banlist.json")
    online: set[str] = set()
    ever_known: set[str] = set()
    rcon_ok = bool(rt_status and rt_status.state.value == "running")
    if rcon_ok:
        rt = DockerRuntime()
        # /players — онлайн-список
        try:
            txt = rt.send_command(spec, "/players")
            for p in _parse_players_output(txt):
                ever_known.add(p["name"])
                if p["online"]:
                    online.add(p["name"])
        except Exception:
            rcon_ok = False
        # /admins — текущий список админов (то что Factorio держит в памяти).
        # Это убирает гонку «кликнул promote → файл ещё не записан → UI пуст».
        if rcon_ok:
            try:
                a_txt = rt.send_command(spec, "/admins")
                fresh = _parse_simple_name_list(a_txt)
                if fresh is not None:
                    admins = fresh
            except Exception:
                pass  # RCON моргнул — fallback на содержимое файла
            # /banlist get — актуальные баны с reason
            try:
                b_txt = rt.send_command(spec, "/banlist get")
                fresh_b = _parse_banlist_output(b_txt)
                if fresh_b is not None:
                    bans = fresh_b
            except Exception:
                pass
            # /whitelist get — актуальный whitelist (только если включён,
            # иначе RCON-команда вернёт «whitelist disabled»)
            if whitelist_enabled:
                try:
                    w_txt = rt.send_command(spec, "/whitelist get")
                    fresh_w = _parse_simple_name_list(w_txt)
                    if fresh_w is not None:
                        whitelist = fresh_w
                except Exception:
                    pass
    # Все compare/dedup — case-insensitive, иначе дубли вида «Admin»/«admin»
    # будут показаны как два игрока, хотя для Factorio это один.
    def _ci_index(items):
        """name.lower() → original-case display name (первое вхождение wins)."""
        out: dict[str, str] = {}
        for n in items:
            k = n.lower()
            if k not in out:
                out[k] = n
        return out
    admin_idx = _ci_index(admins)
    wl_idx = _ci_index(whitelist)
    online_idx = _ci_index(online)
    ever_idx = _ci_index(ever_known)
    ban_keys = {b["name"].lower() for b in bans}
    # display-имя: приоритет — то что прислала Factorio в /players (живой регистр),
    # потом whitelist, потом adminlist, потом ever_known.
    all_keys = (admin_idx.keys() | wl_idx.keys() | ever_idx.keys()) - ban_keys
    rows = []
    for k in sorted(all_keys):
        display = (online_idx.get(k) or ever_idx.get(k)
                   or wl_idx.get(k) or admin_idx.get(k) or k)
        rows.append({
            "name": display,
            "online": k in online_idx,
            "is_admin": k in admin_idx,
            "is_whitelist": k in wl_idx,
        })
    # Online — наверх, потом по имени
    rows.sort(key=lambda r: (not r["online"], r["name"].lower()))
    # Баны тоже CI-дедупим для отображения (первый wins).
    bans_seen: dict[str, dict] = {}
    for b in bans:
        k = b["name"].lower()
        if k not in bans_seen:
            bans_seen[k] = b

    return {
        "players_rows": rows,
        "players_bans": sorted(bans_seen.values(), key=lambda ban: ban["name"].lower()),
        "players_can_rcon": rcon_ok,
        "players_whitelist_enabled": whitelist_enabled,
    }


# ---------- Игроки: админы, whitelist, баны, кики ----------
#
# Когда сервер запущен — действуем через RCON (Factorio сам перепишет json-файлы).
# Когда стоит — правим server-{adminlist,whitelist,banlist}.json на диске; на
# следующем старте Factorio их подхватит. Кик в офлайне физически невозможен
# (некого пинать), отдаём ошибку.

_ACTION_FILE = {
    "promote":   "server-adminlist.json",
    "demote":    "server-adminlist.json",
    "ban":       "server-banlist.json",
    "unban":     "server-banlist.json",
    "wl-add":    "server-whitelist.json",
    "wl-remove": "server-whitelist.json",
}


def _normalize_list_file_for_action(cfg_dir: Path, action: str) -> None:
    """После RCON-операции пере-пишем затронутый json-файл нашим writer'ом —
    он дедупит case-insensitively. Лечит дубли вида «Admin» + «admin»,
    которые Factorio иногда добавляет при разных регистрах ввода."""
    fname = _ACTION_FILE.get(action)
    if not fname:
        return
    path = cfg_dir / fname
    if not path.exists():
        return
    # PermissionError бывает на старых серверах, созданных до того как панель
    # стала пробрасывать PUID — файлы owned by uid 845. Не валим всю операцию:
    # RCON-команда уже отработала (юзер реально promote/ban'нут), мы просто
    # не смогли дедуп сделать. Лог в stdout — для отладки.
    try:
        if fname == "server-banlist.json":
            _write_banlist(path, _read_banlist(path))
        else:
            _write_name_list(path, read_name_list(path))
    except (PermissionError, OSError) as e:
        print(f"[player-action] нормализация {path} пропущена: {e}. "
              f"Файлы owned by uid контейнера? Пересоздай контейнер "
              f"кнопкой ↺ (с новой версии панели PUID/PGID прокидываются).")


def _player_action(spec: ServerSpec, runtime_name: str, *,
                   action: str, name: str, reason: str = "") -> tuple[bool, str]:
    """action ∈ {promote, demote, kick, ban, unban, wl-add, wl-remove}. Возвращает (ok, msg).

    Файловые правки (offline-ветка и whitelist-disabled) обёрнуты в _file_op:
    PermissionError на серверах, созданных до фикса с PUID/PGID, превращается
    в (False, понятный текст), а не в 500."""
    from ..runtime.rcon import RconAuthError, RconError
    rt = get_runtime(runtime_name)

    def _file_op(fn, *args, **kw) -> tuple[bool, str]:
        """Выполнить write-функцию, перехватывая PermissionError/OSError."""
        try:
            fn(*args, **kw)
            return True, ""
        except PermissionError as e:
            return False, tr(
                "servers.player_file_permission", path=e.filename or tr("servers.player_config_file")
            )
        except OSError as e:
            return False, tr("servers.player_file_write_failed", path=e.filename or '', detail=e)

    try:
        is_running = rt.status(spec).state.value == "running"
    except DockerError as e:
        return False, tr("api_v1.docker_error", detail=e)

    cfg_dir = spec.data_dir / "config"

    # Whitelist выключен → НИКОГДА не идём через RCON: Factorio /whitelist add
    # создал бы новый активный файл и неявно включил whitelist. Пишем в .disabled.
    if action in ("wl-add", "wl-remove"):
        wl_active, _wl_disabled = whitelist_paths(cfg_dir)
        if not wl_active.exists():
            path = _whitelist_active_or_disabled(cfg_dir)
            names = read_name_list(path)
            if action == "wl-add":
                if not any(n.lower() == name.lower() for n in names):
                    names.append(name)
            else:
                names = [n for n in names if n.lower() != name.lower()]
            ok, err = _file_op(_write_name_list, path, names)
            if not ok:
                return False, err
            return True, tr("servers.player_whitelist_disabled", action=action, name=name)

    if is_running:
        # RCON — самый прямой путь. Factorio сам обновит json-файлы.
        cmd_map = {
            "promote":   f"/promote {name}",
            "demote":    f"/demote {name}",
            "kick":      f"/kick {name}" + (f" {reason}" if reason else ""),
            "ban":       f"/ban {name}" + (f" {reason}" if reason else ""),
            "unban":     f"/unban {name}",
            "wl-add":    f"/whitelist add {name}",
            "wl-remove": f"/whitelist remove {name}",
        }
        cmd = cmd_map.get(action)
        if cmd is None:
            return False, tr("servers.player_action_unknown", action=action)
        try:
            resp = rt.send_command(spec, cmd)
        except RconAuthError as e:
            return False, tr("api_v1.rcon_auth_error", detail=e)
        except (RconError, DockerError, ConnectionError, OSError) as e:
            return False, tr("api_v1.rcon_error", detail=e)
        # БЫЛ дедуп файла сразу после RCON-команды, но это race: Factorio
        # пишет json асинхронно на следующем тике, а мы читаем СТАРОЕ
        # содержимое и переписываем — откатывая её изменение. Поэтому
        # пока сервер running, доверяем Factorio: она сама держит файл
        # актуальным. Дедуп case-insensitive делаем при правках через
        # offline-ветку ниже (там мы — единственный писатель).
        msg = (resp or "").strip() or tr("servers.player_action_completed", action=action)
        return True, msg

    # ------- Офлайн: правим JSON-файлы напрямую -------
    if action == "kick":
        return False, tr("servers.player_kick_offline")

    if action in ("promote", "demote"):
        path = cfg_dir / "server-adminlist.json"
        names = read_name_list(path)
        if action == "promote":
            if not any(n.lower() == name.lower() for n in names):
                names.append(name)
        else:
            names = [n for n in names if n.lower() != name.lower()]
        ok, err = _file_op(_write_name_list, path, names)
        if not ok:
            return False, err
        return True, tr("servers.player_adminlist_updated", action=action, name=name)

    if action in ("wl-add", "wl-remove"):
        path = _whitelist_active_or_disabled(cfg_dir)
        names = read_name_list(path)
        if action == "wl-add":
            if not any(n.lower() == name.lower() for n in names):
                names.append(name)
        else:
            names = [n for n in names if n.lower() != name.lower()]
        ok, err = _file_op(_write_name_list, path, names)
        if not ok:
            return False, err
        suffix = "" if path.name == "server-whitelist.json" else tr("servers.player_allowlist_off_suffix")
        return True, tr("servers.player_allowlist_updated", action=action, name=name, suffix=suffix)

    if action == "ban":
        path = cfg_dir / "server-banlist.json"
        items = [i for i in _read_banlist(path) if i["name"].lower() != name.lower()]
        items.append({"name": name, "reason": reason})
        ok, err = _file_op(_write_banlist, path, items)
        if not ok:
            return False, err
        return True, tr("servers.player_banned", name=name)

    if action == "unban":
        path = cfg_dir / "server-banlist.json"
        items = [i for i in _read_banlist(path) if i["name"].lower() != name.lower()]
        ok, err = _file_op(_write_banlist, path, items)
        if not ok:
            return False, err
        return True, tr("servers.player_unbanned", name=name)

    return False, tr("servers.player_action_unknown", action=action)


def _do_player_action(uuid: str, db: sqlite3.Connection, action: str,
                      name: str, reason: str = "") -> RedirectResponse:
    row = require_server(db, uuid)
    spec = spec_from_row(row)
    try:
        n = safe_player_name(name)
    except HTTPException as e:
        return server_tab_redirect(uuid, "players", err=str(e.detail))
    r = (reason or "").strip()[:200]
    ok, msg = _player_action(spec, row["runtime"], action=action, name=n, reason=r)
    return server_tab_redirect(uuid, "players", **({"msg": msg} if ok else {"err": msg}))


@router.post("/{uuid}/players/promote")
def players_promote(uuid: str, name: str = Form(...),
                    db: sqlite3.Connection = Depends(get_db)):
    return _do_player_action(uuid, db, "promote", name)


@router.post("/{uuid}/players/demote")
def players_demote(uuid: str, name: str = Form(...),
                   db: sqlite3.Connection = Depends(get_db)):
    return _do_player_action(uuid, db, "demote", name)


@router.post("/{uuid}/players/kick")
def players_kick(uuid: str, name: str = Form(...), reason: str = Form(""),
                 db: sqlite3.Connection = Depends(get_db)):
    return _do_player_action(uuid, db, "kick", name, reason)


@router.post("/{uuid}/players/ban")
def players_ban(uuid: str, name: str = Form(...), reason: str = Form(""),
                db: sqlite3.Connection = Depends(get_db)):
    return _do_player_action(uuid, db, "ban", name, reason)


@router.post("/{uuid}/players/unban")
def players_unban(uuid: str, name: str = Form(...),
                  db: sqlite3.Connection = Depends(get_db)):
    return _do_player_action(uuid, db, "unban", name)


@router.post("/{uuid}/players/wl-add")
def players_wl_add(uuid: str, name: str = Form(...),
                   db: sqlite3.Connection = Depends(get_db)):
    return _do_player_action(uuid, db, "wl-add", name)


@router.post("/{uuid}/players/wl-remove")
def players_wl_remove(uuid: str, name: str = Form(...),
                      db: sqlite3.Connection = Depends(get_db)):
    return _do_player_action(uuid, db, "wl-remove", name)
