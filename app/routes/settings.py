"""Настройки панели: /settings.

Что внутри:
  1) Factorio.com creds (.env: FACTORIO_USERNAME / FACTORIO_TOKEN).
  2) Информация: версия, БД, число серверов, занятое место под servers/,
     кэш версий, последний tick sampler'а.
  3) Действия: очистить кэш версий (Docker Hub), форс prune старых сэмплов.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from .. import auth as auth_lib
from ..db import DB_PATH, audit, get_db
from ..panel_config import (
    COMMON_TZ_IANA, COMMON_TZ_OFFSETS, iana_supported, is_valid_local_domain,
    is_valid_tz, panel_local_domain, panel_tz_name,
)
from ..i18n import tr
from ._settings_redirect import settings_redirect
from .auth_routes import stepup_check_or_redirect
from .. import factorio_versions, server_data
from ..server_files import directory_size, humanize_size

router = APIRouter()

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"

ALLOWED_KEYS = {"FACTORIO_USERNAME", "FACTORIO_TOKEN", "PANEL_TZ",
                "PANEL_LOCAL_DOMAIN", "PANEL_UPDATE_REPO"}
_ENV_WRITE_LOCK = threading.Lock()


# ---------- .env helpers ----------

def _read_env(path: Path) -> dict[str, str]:
    """Простой парсер: KEY=VALUE построчно. Комментарии (#...) и пустые строки
    сохраняются при перезаписи (см. _write_env). Здесь же только наши ключи."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """Атомарно перезаписать .env, обновив значения для updates-ключей.
    Существующие комментарии и порядок строк сохраняем; неизвестные ключи
    остаются на месте; известные, которых не было — добавляются в конец."""
    existing_lines: list[str] = []
    seen_keys: set[str] = set()
    if path.is_file():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    for line in existing_lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            new_lines.append(line)
            continue
        k, _ = s.split("=", 1)
        k = k.strip()
        if k in updates:
            new_lines.append(f"{k}={updates[k]}")
            seen_keys.add(k)
        else:
            new_lines.append(line)
    for k, v in updates.items():
        if k not in seen_keys:
            new_lines.append(f"{k}={v}")
    text = "\n".join(new_lines)
    if not text.endswith("\n"):
        text += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    # Права 600: токен в plain-text, но хотя бы недоступен из других UID-ов
    # на этом хосте. До .replace() ставим — чтобы новый inode уже был защищён.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _apply_env(updates: dict[str, str]) -> None:
    """Keep this process and the file in sync when separate forms save concurrently."""
    with _ENV_WRITE_LOCK:
        _write_env(ENV_PATH, updates)
        os.environ.update(updates)


def _mask_token(token: str) -> str:
    """Маска для отображения: a1b2…c3d4 (первые 4 + последние 4)."""
    if not token:
        return ""
    if len(token) <= 8:
        return "•" * len(token)
    return f"{token[:4]}…{token[-4:]}"


# ---------- Info-блок ----------

def _last_sample_age(db: sqlite3.Connection) -> tuple[int | None, int]:
    """Вернуть (timestamp_последнего_сэмпла, всего_сэмплов_в_БД)."""
    row = db.execute(
        "SELECT MAX(ts) AS ts, COUNT(*) AS n FROM metrics_samples"
    ).fetchone()
    return (row["ts"] if row and row["ts"] else None,
            int(row["n"]) if row else 0)


def _gather_info(db: sqlite3.Connection) -> dict:
    last_ts, samples_total = _last_sample_age(db)
    servers_count = db.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
    sessions_open = db.execute(
        "SELECT COUNT(*) FROM player_sessions WHERE left_at IS NULL"
    ).fetchone()[0]
    version_count, cache_age = factorio_versions.cache_info()
    return {
        "root": str(ROOT),
        "db_path": str(DB_PATH),
        "db_size": humanize_size(DB_PATH.stat().st_size) if DB_PATH.is_file() else "—",
        "servers_dir": str(server_data.SERVERS_DIR),
        "servers_dir_size": humanize_size(directory_size(server_data.SERVERS_DIR)),
        "servers_count": servers_count,
        "sessions_open": sessions_open,
        "samples_total": samples_total,
        "last_sample_ts": last_ts,
        "versions_cache_n": version_count,
        "versions_cache_age": cache_age,
        "versions_cache_ttl": factorio_versions.CACHE_TTL,
    }


# ---------- Routes ----------

def _redirect(*, msg: str | None = None, err: str | None = None,
              anchor: str = "") -> RedirectResponse:
    return settings_redirect(msg=msg, err=err, tab=anchor)


def _env_updates_for_section(section: str, *, username: str, token: str,
                             clear_token: str, panel_tz: str) -> dict[str, str]:
    """Собрать изменения .env для одной формы, не читая чужие поля формы.

    Пустой ``section`` оставляет старый контракт единой формы для клиентов,
    которые ещё не передают режим.
    """
    if section not in ("", "timezone", "factorio"):
        raise ValueError(tr("settings.unknown_section"))
    updates: dict[str, str] = {}
    if section != "factorio":
        tz = panel_tz.strip()
        if not is_valid_tz(tz):
            raise ValueError(tr("settings.timezone_unrecognized", timezone=tz))
        updates["PANEL_TZ"] = tz
    if section != "timezone":
        updates["FACTORIO_USERNAME"] = username.strip()
        if token.strip():
            updates["FACTORIO_TOKEN"] = token.strip()
        elif clear_token == "1":
            updates["FACTORIO_TOKEN"] = ""
    return updates


def _collect_audit(qp, db: sqlite3.Connection) -> tuple[list[dict], dict]:
    """Собрать audit_rows + audit_filter по query-params. Используется
    /settings (full-page) и /settings/audit-fragment (live-poll)."""
    audit_kind = (qp.get("audit_kind") or "").strip()
    audit_server = (qp.get("audit_server") or "").strip()
    try:
        audit_per = int(qp.get("audit_per") or 50)
    except ValueError:
        audit_per = 50
    if audit_per not in (10, 25, 50, 100):
        audit_per = 50
    try:
        audit_page = max(1, int(qp.get("audit_page") or 1))
    except ValueError:
        audit_page = 1

    where: list[str] = []
    args: list = []
    if audit_kind:
        if "." in audit_kind:
            where.append("a.kind = ?")
            args.append(audit_kind)
        else:
            where.append("(a.kind = ? OR a.kind LIKE ?)")
            args.extend([audit_kind, audit_kind + ".%"])
    if audit_server:
        if audit_server == "__none__":
            where.append("a.server_uuid IS NULL")
        else:
            where.append("a.server_uuid = ?")
            args.append(audit_server)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    audit_total = db.execute(
        "SELECT COUNT(*) FROM audit_log a" + where_sql, args
    ).fetchone()[0]
    audit_pages = max(1, (audit_total + audit_per - 1) // audit_per)
    if audit_page > audit_pages:
        audit_page = audit_pages
    offset = (audit_page - 1) * audit_per
    rows = [dict(r) for r in db.execute(
        "SELECT a.ts, a.kind, a.server_uuid, a.summary, s.name AS server_name "
        "FROM audit_log a LEFT JOIN servers s ON s.uuid = a.server_uuid "
        + where_sql +
        " ORDER BY a.ts DESC LIMIT ? OFFSET ?",
        args + [audit_per, offset],
    ).fetchall()]
    audit_kinds = [r[0] for r in db.execute(
        "SELECT DISTINCT kind FROM audit_log ORDER BY kind"
    ).fetchall()]
    audit_kind_groups = sorted({k.split(".", 1)[0] for k in audit_kinds})
    audit_servers_list = [dict(r) for r in db.execute(
        "SELECT DISTINCT a.server_uuid AS uuid, s.name "
        "FROM audit_log a LEFT JOIN servers s ON s.uuid = a.server_uuid "
        "WHERE a.server_uuid IS NOT NULL ORDER BY s.name"
    ).fetchall()]
    return rows, {
        "kind": audit_kind, "server": audit_server,
        "page": audit_page, "per": audit_per,
        "total": audit_total, "pages": audit_pages,
        "kinds": audit_kinds, "kind_groups": audit_kind_groups,
        "servers": audit_servers_list,
    }


@router.get("/settings/audit-fragment")
def settings_audit_fragment(request: Request,
                            db: sqlite3.Connection = Depends(get_db)):
    """Live-fragment журнала: только tbody + пагинация. JS на /settings
    свопает innerHTML контейнера id='audit-table-region' каждые 30с когда
    вкладка #audit активна."""
    rows, filt = _collect_audit(request.query_params, db)
    return request.app.state.templates.TemplateResponse(
        request, "_partials/audit_table.html",
        {"audit_rows": rows, "audit_filter": filt},
    )


@router.get("/settings")
def settings_page(request: Request,
                  db: sqlite3.Connection = Depends(get_db)):
    env = _read_env(ENV_PATH)

    # ---- Журнал: фильтр + пагинация (server-side) ----
    # audit_kind: префикс типа события ('server' / 'server.start' / 'auth' и т.д.).
    # audit_server: точный UUID сервера; '__none__' = только без привязки к серверу.
    # audit_page (1-based) и audit_per (10/25/50/100) — пагинация.
    audit_rows, audit_filter = _collect_audit(request.query_params, db)

    # ---- Security-секция: сессии, неудачные попытки, IP-баны, 2FA ----
    user = getattr(request.state, "user", None)
    sec: dict = {"sessions": [], "failed_attempts": [], "bans": [],
                 "current_session_id": None, "totp_enabled": False}
    if user:
        sec["current_session_id"] = user.id
        sec["sessions"] = auth_lib.list_user_sessions(db, user.user_id)
        sec["failed_attempts"] = [dict(r) for r in db.execute(
            "SELECT ts, ip, username FROM auth_attempts "
            "WHERE success = 0 AND ts >= ? ORDER BY ts DESC LIMIT 10",
            (int(time.time()) - 24 * 3600,),
        ).fetchall()]
        sec["successful_logins"] = [dict(r) for r in db.execute(
            "SELECT ts, ip, username FROM auth_attempts "
            "WHERE success = 1 AND ts >= ? ORDER BY ts DESC LIMIT 20",
            (int(time.time()) - 7 * 86400,),
        ).fetchall()]
        sec["bans"] = auth_lib.list_active_bans(db)
        sec["now"] = int(time.time())
        db_user = auth_lib.get_user_by_id(db, user.user_id)
        sec["totp_enabled"] = bool(db_user and db_user.get("totp_enabled"))
        sec["backup_codes_left"] = (auth_lib.count_unused_backup_codes(db, user.user_id)
                                    if sec["totp_enabled"] else 0)

    # ---- API-ключи секция ----
    api_keys: list[dict] = []
    if user:
        api_keys = auth_lib.list_api_keys(db, user.user_id)

    # ---- Webhooks секция (Phase 4) ----
    webhooks: list[dict] = []
    if user:
        from .. import webhooks as _wh
        webhooks = _wh.list_webhooks(db, user.user_id)

    # ---- Network/Access info ----
    from .. import network_info as _ni
    network = _ni.collect()
    local_domain_current = panel_local_domain()

    # ---- Panel self-update info ----
    from .. import panel_update as _upd
    # Что лежит в staging — чтобы юзеру не приходилось вручную набирать
    # имя файла в форме «установить из загруженного». Берём только архивные
    # расширения, отсортированные по mtime DESC (свежий сверху).
    staging_files: list[dict] = []
    # Загруженные архивы с миграции лежат в UPLOADS_DIR, до миграции — могли
    # лежать в STAGING_DIR. Сканим оба, дедуп по имени (свежий побеждает).
    seen_names: set[str] = set()
    for src_dir in (_upd.UPLOADS_DIR, _upd.STAGING_DIR):
        if not src_dir.is_dir():
            continue
        for p in src_dir.iterdir():
            if not p.is_file() or p.name in seen_names:
                continue
            if not (p.name.endswith(".tar.gz") or p.name.endswith(".tgz")
                    or p.name.endswith(".zip")):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            seen_names.add(p.name)
            staging_files.append({
                "name": p.name,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
    staging_files.sort(key=lambda f: f["mtime"], reverse=True)
    panel_update_ctx = {
        "current_version": _upd.read_version(),
        "repo": _upd.get_repo(),
        "history": _upd.list_history(db, limit=10),
        "is_windows": __import__("sys").platform == "win32",
        "staging_files": staging_files,
    }

    return request.app.state.templates.TemplateResponse(
        request, "settings.html",
        {
            "page": "settings",
            "env": {
                "FACTORIO_USERNAME": env.get("FACTORIO_USERNAME", ""),
                "FACTORIO_TOKEN_MASK": _mask_token(env.get("FACTORIO_TOKEN", "")),
                "FACTORIO_TOKEN_SET": bool(env.get("FACTORIO_TOKEN")),
                "PANEL_TZ": panel_tz_name(),
            },
            "env_path": str(ENV_PATH),
            "common_tzs": COMMON_TZ_OFFSETS,
            "common_tzs_iana": COMMON_TZ_IANA,
            "iana_supported": iana_supported(),
            "info": _gather_info(db),
            "audit_rows": audit_rows,
            "audit_filter": audit_filter,
            "autostart": _autostart_status(),
            "security": sec,
            "api_keys": api_keys,
            "webhooks": webhooks,
            "webhook_events_all": __import__("app.webhooks", fromlist=["EVENTS_ALL"]).EVENTS_ALL,
            "network": network,
            "local_domain_current": local_domain_current,
            "api_scopes_all": auth_lib.API_SCOPES_ALL,
            "panel_update": panel_update_ctx,
            "msg": (request.query_params.get("msg") or "").strip() or None,
            "err": (request.query_params.get("err") or "").strip() or None,
        },
    )


@router.post("/settings/env")
def settings_env(request: Request,
                 username: str = Form(""),
                 token: str = Form(""),
                 clear_token: str = Form(""),
                 panel_tz: str = Form(""),
                 section: str = Form("")):
    """Сохранить FACTORIO_USERNAME / FACTORIO_TOKEN / PANEL_TZ в .env. Пустой
    token игнорим (чтобы случайным submit'ом не затереть существующий).
    Очистка — отдельным чекбоксом clear_token=1. PANEL_TZ — IANA-имя
    (Europe/Moscow); пустое = «системная локальная»."""
    if (r := stepup_check_or_redirect(request)) is not None: return r
    section = section.strip().lower()
    try:
        updates = _env_updates_for_section(
            section, username=username, token=token,
            clear_token=clear_token, panel_tz=panel_tz,
        )
    except ValueError as exc:
        return _redirect(err=str(exc))
    try:
        _apply_env(updates)
    except OSError as e:
        return _redirect(err=tr("settings.env_write_failed", detail=e))
    if section == "timezone":
        return _redirect(msg=tr("settings.timezone_saved"))
    if section == "factorio":
        return _redirect(msg=tr("settings.factorio_credentials_saved"))
    return _redirect(msg=tr("settings.env_updated_no_restart"))


# ---------- Автозагрузка самой панели ----------
#
# Кросс-OS детект и toggle:
#   Linux  — системный systemd-unit roboport.service. Состояние читаем
#            через `systemctl is-enabled roboport` (без sudo). Включить/выключить —
#            spawn'аем `systemctl enable/disable roboport`; без прав на это
#            (что нормально для not-root юзера) возвращаем подсказку с командой.
#   Windows — `.bat` в Startup-папке текущего юзера (autorun без админских прав).
#            Состояние = факт наличия файла; toggle = создать/удалить.

import platform
import sys


def _windows_startup_bat() -> Path:
    """Путь к Roboport.bat в shell:startup текущего юзера."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        appdata = str(Path.home() / "AppData" / "Roaming")
    return (Path(appdata)
            / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Startup" / "Roboport.bat")


def _autostart_status() -> dict:
    """Снимок состояния автозагрузки панели для UI."""
    if platform.system() == "Windows":
        bat = _windows_startup_bat()
        return {
            "platform": "windows",
            "available": True,
            "enabled": bat.is_file(),
            "path": str(bat),
            "note": tr("settings.autostart_windows"),
        }
    if platform.system() == "Linux":
        # Проверяем именно systemd — другие init'ы не поддерживаем
        import shutil as _shutil
        if not _shutil.which("systemctl"):
            return {
                "platform": "linux", "available": False, "enabled": False,
                "note": tr("settings.autostart_systemctl_missing"),
            }
        import subprocess as _sp
        try:
            r = _sp.run(["systemctl", "is-enabled", "roboport"],
                        capture_output=True, text=True, timeout=5)
        except Exception as e:
            return {
                "platform": "linux", "available": False, "enabled": False,
                "note": tr("settings.autostart_systemctl_failed", detail=e),
            }
        out = (r.stdout or "").strip()
        # is-enabled выдаёт: enabled / disabled / static / masked / not-found / ...
        if out == "not-found" or "could not be found" in (r.stderr or "").lower():
            return {
                "platform": "linux", "available": False, "enabled": False,
                "note": tr("settings.autostart_service_missing"),
            }
        return {
            "platform": "linux", "available": True,
            "enabled": (out == "enabled"),
            "raw": out,
            "note": tr("settings.autostart_systemd"),
        }
    return {
        "platform": platform.system().lower(),
        "available": False, "enabled": False,
        "note": tr("settings.autostart_os_unsupported"),
    }


def _set_autostart(enable: bool) -> tuple[bool, str]:
    """Включить/выключить автозагрузку. Возвращает (ok, message)."""
    st = _autostart_status()
    if not st["available"]:
        return False, st.get("note") or tr("settings.autostart_unavailable")
    if st["platform"] == "windows":
        bat = _windows_startup_bat()
        bat.parent.mkdir(parents=True, exist_ok=True)
        if enable:
            project = Path(__file__).resolve().parents[2]
            pyw = project / ".venv" / "Scripts" / "pythonw.exe"
            if not pyw.exists():
                # фоллбек на python.exe — будет с консолью
                pyw = project / ".venv" / "Scripts" / "python.exe"
            if not pyw.exists():
                return False, tr("settings.autostart_python_missing", project=project)
            content = (
                "@echo off\r\n"
                f'cd /d "{project}"\r\n'
                f'start "" "{pyw}" -m uvicorn app.main:app --host 127.0.0.1 --port 8000\r\n'
            )
            bat.write_text(content, encoding="ascii")
            return True, tr("settings.autostart_enabled_at", path=bat)
        else:
            if bat.exists():
                bat.unlink()
            return True, tr("settings.autostart_disabled")
    if st["platform"] == "linux":
        import subprocess as _sp
        action = "enable" if enable else "disable"
        try:
            r = _sp.run(["systemctl", action, "roboport"],
                        capture_output=True, text=True, timeout=10)
        except Exception as e:
            return False, tr("settings.autostart_systemctl_action_failed", action=action, detail=e)
        if r.returncode == 0:
            return True, tr("settings.autostart_enabled" if enable else "settings.autostart_disabled")
        # Чаще всего — нет прав. Не ругаемся, отдаём команду чтобы юзер сам выполнил.
        err = (r.stderr or r.stdout or "").strip()[:300]
        cmd = f"sudo systemctl {action} roboport"
        return False, tr("settings.autostart_systemctl_permission", detail=err, command=cmd)
    return False, tr("settings.autostart_platform_unknown", platform=st['platform'])


@router.post("/settings/panel-autostart")
def settings_panel_autostart(enable: str = Form("0"),
                             db: sqlite3.Connection = Depends(get_db)):
    want = (enable or "").strip() == "1"
    ok, msg = _set_autostart(want)
    audit(db, "panel.autostart",
          summary=f"{'enable' if want else 'disable'} → {'ok' if ok else 'fail'}: {msg[:120]}")
    db.commit()
    return _redirect(msg=msg) if ok else _redirect(err=msg)


@router.post("/settings/unban")
def settings_unban(request: Request,
                   ip: str = Form(...),
                   db: sqlite3.Connection = Depends(get_db)):
    """Снять IP-бан из UI. Требует залогиненного админа (auth-middleware
    уже проверил). Бан-middleware пропускает свой же IP к этому роуту,
    потому что мы НЕ баним сами себя."""
    if (r := stepup_check_or_redirect(request)) is not None: return r
    ip = (ip or "").strip()
    if not ip:
        return _redirect(err=tr("settings.ip_required"), anchor="security")
    ok = auth_lib.unban_ip(db, ip)
    audit(db, "auth.ip.unbanned", None, summary=f"manual unban: {ip}")
    db.commit()
    return _redirect(
        msg=tr("settings.ip_unbanned" if ok else "settings.ip_not_banned", ip=ip),
        anchor="security",
    )


@router.post("/settings/clear-versions-cache")
def settings_clear_versions_cache():
    factorio_versions.clear_cache()
    return _redirect(msg=tr("settings.version_cache_cleared"))


@router.post("/settings/prune-metrics")
def settings_prune_metrics(db: sqlite3.Connection = Depends(get_db)):
    """Прибить сэмплы и закрытые сессии старше RETAIN_DAYS прямо сейчас
    (вместо ожидания планового prune раз в час)."""
    from ..sampler import RETAIN_DAYS
    cutoff = int(time.time()) - RETAIN_DAYS * 86400
    n_metrics = db.execute(
        "DELETE FROM metrics_samples WHERE ts < ?", (cutoff,)
    ).rowcount
    n_sess = db.execute(
        "DELETE FROM player_sessions WHERE left_at IS NOT NULL AND left_at < ?",
        (cutoff,),
    ).rowcount
    db.commit()
    return _redirect(msg=tr("settings.prune_completed", metrics=n_metrics, sessions=n_sess, days=RETAIN_DAYS))


# =============================================================================
# TLS actions (для вкладки «🌐 Доступ»)
# =============================================================================
# Все требуют step-up (destructive — может сломать доступ если конфиг битый).
# Выполняются через sudo с whitelist'ом из /etc/sudoers.d/roboport-tls
# (создаётся install.sh при INSTALL_TLS=selfsigned).

CERT_PATH = Path("/etc/nginx/roboport-tls/cert.pem")
KEY_PATH = Path("/etc/nginx/roboport-tls/key.pem")
GEN_SCRIPT = ROOT / "deploy" / "gen-self-signed.sh"


def _access_redirect(*, msg: str | None = None,
                     err: str | None = None) -> RedirectResponse:
    return settings_redirect(msg=msg, err=err, tab="access")


def _run_sudo(cmd: list[str], env_extra: dict | None = None,
              timeout: int = 30) -> tuple[int, str, str]:
    """sudo -n (no-password) на whitelist'ные команды. Возвращает
    (rc, stdout, stderr)."""
    import subprocess
    full_cmd = ["sudo", "-n"]
    if env_extra:
        full_cmd.append("env")
        for k, v in env_extra.items():
            full_cmd.append(f"{k}={v}")
    full_cmd.extend(cmd)
    try:
        r = subprocess.run(full_cmd, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


def _nginx_test_reload() -> str | None:
    """nginx -t + reload. Возвращает None при успехе, текст ошибки иначе."""
    rc, _, err = _run_sudo(["/usr/sbin/nginx", "-t"], timeout=5)
    if rc != 0:
        return tr("settings.nginx_test_failed", detail=err[:200])
    rc, _, err = _run_sudo(["/bin/systemctl", "reload", "nginx"], timeout=10)
    if rc != 0:
        rc, _, err = _run_sudo(["/usr/sbin/systemctl", "reload", "nginx"],
                                timeout=10)
    if rc != 0:
        return tr("settings.nginx_reload_failed", detail=err[:200])
    return None


@router.post("/settings/tls/regenerate")
def tls_regenerate(request: Request,
                   db: sqlite3.Connection = Depends(get_db)):
    """Перевыпустить self-signed cert. SAN автоматически собирается заново
    (hostname + .local + текущие IP)."""
    if (r := stepup_check_or_redirect(request)) is not None: return r
    user = getattr(request.state, "user", None)
    if not user:
        return _access_redirect(err=tr("auth_routes.not_authenticated"))

    rc, out, err = _run_sudo(
        [str(GEN_SCRIPT), "--force"],
        timeout=20,
    )
    if rc != 0:
        return _access_redirect(err=tr(
            "settings.tls_generation_failed", code=rc, detail=(err or out)[:200],
        ))

    rl_err = _nginx_test_reload()
    if rl_err:
        return _access_redirect(err=rl_err)

    audit(db, "tls.regenerate", None,
          summary=f"{user.username} regenerated self-signed cert")
    db.commit()
    return _access_redirect(
        msg=tr("settings.tls_regenerated"))


@router.post("/settings/tls/upload")
async def tls_upload(request: Request,
                     db: sqlite3.Connection = Depends(get_db)):
    """Загрузить свой cert + key (multipart). Валидируется PEM-формат + cert
    и key должны быть парой (modulus check). Записываются в /etc/nginx/
    roboport-tls/ через `install -m ... -o root -g root` (sudo whitelist)."""
    if (r := stepup_check_or_redirect(request)) is not None: return r
    user = getattr(request.state, "user", None)
    if not user:
        return _access_redirect(err=tr("auth_routes.not_authenticated"))

    form = await request.form()
    cert_file = form.get("cert_file")
    key_file = form.get("key_file")
    if not cert_file or not key_file:
        return _access_redirect(err=tr("settings.tls_files_required"))
    cert_bytes = await cert_file.read()
    key_bytes = await key_file.read()

    if b"-----BEGIN CERTIFICATE-----" not in cert_bytes:
        return _access_redirect(err=tr("settings.tls_invalid_cert"))
    if (b"-----BEGIN PRIVATE KEY-----" not in key_bytes and
        b"-----BEGIN RSA PRIVATE KEY-----" not in key_bytes and
        b"-----BEGIN EC PRIVATE KEY-----" not in key_bytes):
        return _access_redirect(err=tr("settings.tls_invalid_key"))

    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(prefix="roboport-cert-upload-",
                                      suffix=".pem", delete=False,
                                      dir="/tmp") as tcf:
        tcf.write(cert_bytes); cert_tmp = tcf.name
    with tempfile.NamedTemporaryFile(prefix="roboport-cert-upload-",
                                      suffix="-key.pem", delete=False,
                                      dir="/tmp") as tkf:
        tkf.write(key_bytes); key_tmp = tkf.name

    cleanup_tmp = lambda: [Path(p).unlink(missing_ok=True)
                           for p in (cert_tmp, key_tmp)]
    try:
        # Modulus cross-check для RSA. EC пропускаем (другой формат).
        if b"EC PRIVATE KEY" not in key_bytes:
            try:
                m1 = subprocess.run(
                    ["openssl", "x509", "-noout", "-modulus", "-in", cert_tmp],
                    capture_output=True, text=True, errors="replace", timeout=5,
                )
                m2 = subprocess.run(
                    ["openssl", "rsa", "-noout", "-modulus", "-in", key_tmp],
                    capture_output=True, text=True, errors="replace", timeout=5,
                )
                if m1.returncode != 0:
                    cleanup_tmp()
                    return _access_redirect(err=tr("settings.tls_cert_parse_failed", detail=m1.stderr[:150]))
                if m2.returncode == 0 and m1.stdout.strip() != m2.stdout.strip():
                    cleanup_tmp()
                    return _access_redirect(err=tr("settings.tls_cert_key_mismatch"))
            except subprocess.TimeoutExpired:
                cleanup_tmp()
                return _access_redirect(err=tr("settings.tls_validation_timeout"))

        # install через sudo whitelist
        rc, _, err = _run_sudo(
            ["/usr/bin/install", "-m", "644", "-o", "root", "-g", "root",
             cert_tmp, str(CERT_PATH)],
            timeout=5,
        )
        if rc != 0:
            return _access_redirect(err=tr("settings.tls_file_install_failed", file="cert.pem", detail=err[:200]))
        rc, _, err = _run_sudo(
            ["/usr/bin/install", "-m", "600", "-o", "root", "-g", "root",
             key_tmp, str(KEY_PATH)],
            timeout=5,
        )
        if rc != 0:
            return _access_redirect(err=tr("settings.tls_file_install_failed", file="key.pem", detail=err[:200]))

        rl_err = _nginx_test_reload()
        if rl_err:
            return _access_redirect(err=rl_err)

        audit(db, "tls.upload", None,
              summary=f"{user.username} uploaded custom cert + key")
        db.commit()
        return _access_redirect(msg=tr("settings.tls_uploaded"))
    finally:
        cleanup_tmp()


@router.post("/settings/panel/restart")
def panel_restart(request: Request,
                  db: sqlite3.Connection = Depends(get_db)):
    """Рестарт самой панели через systemd. На Linux спавним detached
    процесс, который через 1с делает `sudo -n systemctl restart roboport`
    — за эту секунду мы успеваем вернуть JSON клиенту, потом systemd
    убивает uvicorn и поднимает заново. Клиент после ответа поллит
    /api/v1/health пока не вернётся 200, тогда reload.

    Платформа != linux → 501 (Windows-dev: дёргать вручную)."""
    if (r := stepup_check_or_redirect(request)) is not None: return r
    user = getattr(request.state, "user", None)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    import sys
    if not sys.platform.startswith("linux"):
        return JSONResponse(
            {"error": "not_supported",
             "message": tr("settings.restart_linux_only")},
            status_code=501)

    # Сначала фиксируем audit (commit пройдёт до рестарта).
    audit(db, "panel.restart", None,
          summary=f"{user.username} restart panel via UI")
    db.commit()

    # Detached процесс: bash -c "sleep 1 && sudo -n systemctl restart ..."
    # `sleep 1` — чтобы успеть вернуть HTTP-ответ клиенту до того как
    # systemd убьёт нас. start_new_session=True — детач от родительского
    # process group, иначе systemd прибьёт и его при stop unit'а.
    import subprocess
    cmd = ("sleep 1 && "
           "( sudo -n /bin/systemctl restart roboport "
           "|| sudo -n /usr/sbin/systemctl restart roboport )")
    try:
        subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return JSONResponse(
            {"error": "spawn_failed", "message": str(e)[:200]},
            status_code=500)

    return JSONResponse(
        {"ok": True,
         "message": tr("settings.restart_waiting_for_health")},
        status_code=202)


@router.post("/settings/tls/local-domain")
def tls_local_domain(request: Request,
                     name: str = Form(...),
                     db: sqlite3.Connection = Depends(get_db)):
    """Сменить локальное mDNS-имя в LAN (например `panel.local`). Цепочка:
    1. Запись в .env (PANEL_LOCAL_DOMAIN)
    2. Обновление /etc/avahi/avahi-daemon.conf (host-name=<base>) →
       avahi-daemon перезапускается → начинает рекламировать новое имя
    3. Перевыпуск cert с новым именем в primary CN/SAN (gen-self-signed
       автоматом добавит остальные имена: localhost, IP, и т.п.)
    4. nginx -t + reload (cert path тот же, просто содержимое новое)

    nginx server_name НЕ трогаем — он остаётся `_` (catch-all) в template'е,
    отвечает на любой Host header. Это правильно: cert SAN — единственная
    проверка имён, она же действует на global-домены через Let's Encrypt."""
    if (r := stepup_check_or_redirect(request)) is not None: return r
    user = getattr(request.state, "user", None)
    if not user:
        return _access_redirect(err=tr("auth_routes.not_authenticated"))

    name = (name or "").strip().lower()
    if not is_valid_local_domain(name):
        return _access_redirect(
            err=tr(
                "settings.local_domain_invalid",
                name=name,
            ))

    base = name[:-len(".local")]  # 'panel.local' -> 'panel'

    # 1) .env
    _apply_env({"PANEL_LOCAL_DOMAIN": name})

    # 2) /etc/avahi/avahi-daemon.conf — установить host-name=<base>.
    # Читаем текущий конфиг, заменяем (или вставляем) host-name=, пишем
    # tempfile, sudo install, restart avahi.
    avahi_conf_path = "/etc/avahi/avahi-daemon.conf"
    try:
        avahi_text = Path(avahi_conf_path).read_text(encoding="utf-8")
    except OSError as e:
        return _access_redirect(
            err=tr(
                "settings.local_domain_avahi_missing",
                path=avahi_conf_path,
                detail=e,
            ))

    import re as _re
    # Ищем `host-name=...` в [server]-секции, или раскомментированный `#host-name=...`
    if _re.search(r"^[#\s]*host-name=", avahi_text, _re.MULTILINE):
        new_avahi = _re.sub(
            r"^[#\s]*host-name=.*$",
            f"host-name={base}",
            avahi_text, count=1, flags=_re.MULTILINE,
        )
    else:
        # Вставляем после [server]-заголовка
        new_avahi = _re.sub(
            r"(^\[server\]\s*\n)",
            r"\1host-name=" + base + "\n",
            avahi_text, count=1, flags=_re.MULTILINE,
        )

    import tempfile
    with tempfile.NamedTemporaryFile(prefix="roboport-avahi-",
                                      suffix=".conf", delete=False,
                                      dir="/tmp") as tf:
        tf.write(new_avahi.encode("utf-8")); tmp_avahi = tf.name
    try:
        rc, _, err = _run_sudo(
            ["/usr/bin/install", "-m", "644", "-o", "root", "-g", "root",
             tmp_avahi, avahi_conf_path],
            timeout=5,
        )
        if rc != 0:
            return _access_redirect(err=f"install avahi-config: {err[:200]}")
    finally:
        Path(tmp_avahi).unlink(missing_ok=True)

    rc, _, err = _run_sudo(
        ["/bin/systemctl", "restart", "avahi-daemon"], timeout=10,
    )
    if rc != 0:
        rc, _, err = _run_sudo(
            ["/usr/sbin/systemctl", "restart", "avahi-daemon"], timeout=10,
        )
    if rc != 0:
        return _access_redirect(err=f"restart avahi: {err[:200]}")

    # 3) Перевыпуск cert — новое имя как primary, остальные SAN auto.
    rc, out, err = _run_sudo(
        [str(GEN_SCRIPT), name, "--force"],
        timeout=20,
    )
    if rc != 0:
        return _access_redirect(
            err=f"gen-self-signed: {(err or out)[:200]}")

    # 4) nginx -t + reload
    rl_err = _nginx_test_reload()
    if rl_err:
        return _access_redirect(err=rl_err)

    audit(db, "tls.local_domain", None,
          summary=f"{user.username} local domain = {name!r}")
    db.commit()
    return _access_redirect(
        msg=tr(
            "settings.local_domain_applied",
            name=name,
        ))


# ============================================================================
# Self-update панели — UI endpoints (вкладка «🔄 Обновления»)
# ============================================================================
# Бэкенд логика — в app/panel_update.py. REST-аналоги (для API-ключей) —
# в app/routes/api_v1.py под /api/v1/panel/update/*. UI-роуты делают то же
# самое, но используют cookie-сессию + CSRF, и делают redirect на /settings#updates.

def _update_redirect(*, msg: str | None = None,
                     err: str | None = None) -> RedirectResponse:
    """Redirect обратно на вкладку «Обновления» с сохранением hash."""
    return settings_redirect(msg=msg, err=err, tab="updates")


@router.post("/settings/panel-update/repo")
def panel_update_set_repo(
    repo: str = Form(""),
    request: Request = None,  # type: ignore
    db: sqlite3.Connection = Depends(get_db),
):
    """Сохранить GitHub-репо для проверки апдейтов в .env (PANEL_UPDATE_REPO)
    и в process env (чтобы текущий процесс сразу видел новое значение)."""
    from .. import panel_update as _upd
    repo = (repo or "").strip()
    if repo and not _upd.is_valid_repo(repo):
        return _update_redirect(err=tr(
            "settings.update_repo_invalid",
            repo=repo,
        ))
    _apply_env({"PANEL_UPDATE_REPO": repo})
    user = getattr(request.state, "user", None)
    if user:
        audit(db, "panel.update.repo", None,
              summary=f"{user.username} set update_repo = {repo or '(default)'!r}")
        db.commit()
    return _update_redirect(
        msg=tr("settings.update_repo_saved", repo=repo or _upd.DEFAULT_REPO))


@router.get("/settings/panel-update/check")
def panel_update_check(repo: str = "") -> JSONResponse:
    """Дёрнуть GitHub Releases API. Возвращает JSON для AJAX из UI."""
    from .. import panel_update as _upd
    use_repo = (repo or _upd.get_repo()).strip()
    try:
        rel = _upd.check_github(use_repo)
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    current = _upd.read_version()
    return JSONResponse({
        "ok": True,
        "current": current,
        "repo": use_repo,
        "latest": {
            "tag": rel.tag,
            "name": rel.name,
            "body": rel.body,
            "published_at": rel.published_at,
            "html_url": rel.html_url,
        },
        "update_available": _upd.is_newer(rel.tag, current),
    })


@router.post("/settings/panel-update/install")
def panel_update_install(
    request: Request,
    source: str = Form("github"),
    tag: str = Form(""),
    upload_filename: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
):
    """Запустить апдейт. Source = 'github' (нужен tag) или 'upload'
    (нужен upload_filename из предыдущего /upload)."""
    from .. import panel_update as _upd
    user = getattr(request.state, "user", None)
    if (sr := stepup_check_or_redirect(request)) is not None:
        return sr

    if _upd.STAGING_DIR.exists() and (_upd.STAGING_DIR / "running").exists():
        return _update_redirect(err=tr("settings.update_already_running"))

    try:
        if source == "github":
            if not tag.strip():
                return _update_redirect(err=tr("settings.update_tag_required"))
            plan = _upd.prepare_from_github(_upd.get_repo(), tag.strip())
        elif source == "upload":
            if not upload_filename.strip():
                return _update_redirect(err=tr("settings.update_upload_filename_required"))
            fname = Path(upload_filename.strip()).name
            # Сначала ищем в новой папке UPLOADS_DIR, потом fallback в STAGING_DIR
            # (чтобы старые загрузки до миграции не сломались).
            archive = _upd.UPLOADS_DIR / fname
            if not archive.exists():
                legacy = _upd.STAGING_DIR / fname
                if legacy.exists():
                    archive = legacy
                else:
                    return _update_redirect(err=tr(
                        "settings.update_upload_missing",
                        filename=upload_filename,
                    ))
            plan = _upd.prepare_from_upload(archive)
        else:
            return _update_redirect(err=tr("settings.update_source_unknown", source=source))
    except RuntimeError as e:
        return _update_redirect(err=tr("settings.update_preparation_failed", detail=e))

    initiator = user.username if user else "ui:unknown"
    try:
        update_id = _upd.execute_install(
            db, plan,
            initiated_by=initiator,
        )
    except RuntimeError as e:
        return _update_redirect(err=tr("settings.update_install_failed", detail=e))

    audit(db, "panel.update.started", None,
          summary=f"{initiator} → {plan.source} (id={update_id})")
    db.commit()
    return _update_redirect(
        msg=tr(
            "settings.update_install_started",
            id=update_id,
        ))


@router.post("/settings/panel-update/upload")
def panel_update_upload(
    request: Request,
    file: UploadFile = File(...),
    db: sqlite3.Connection = Depends(get_db),
):
    """Принять multipart-архив (.tar.gz/.zip) и положить в .update-staging/.
    После — отдельная форма с source=upload и upload_filename."""
    from .. import panel_update as _upd
    if not file.filename:
        return _update_redirect(err=tr("settings.update_file_not_selected"))
    fname = Path(file.filename).name
    if not (fname.endswith(".tar.gz") or fname.endswith(".tgz") or fname.endswith(".zip")):
        return _update_redirect(err=tr("settings.update_file_type_invalid"))
    # ВАЖНО: пишем в UPLOADS_DIR, НЕ в STAGING_DIR. execute_install rmtree'ит
    # staging до распаковки, поэтому архив нельзя там держать — иначе он
    # удалится раньше чем tarfile успеет его прочитать.
    _upd.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dst = _upd.UPLOADS_DIR / fname
    with open(dst, "wb") as f:
        while chunk := file.file.read(64 * 1024):
            f.write(chunk)
    user = getattr(request.state, "user", None)
    if user:
        audit(db, "panel.update.upload", None,
              summary=f"{user.username} uploaded {fname} ({dst.stat().st_size} bytes)")
        db.commit()
    return _update_redirect(
        msg=tr(
            "settings.update_file_uploaded",
            filename=fname,
            size=dst.stat().st_size // 1024,
        ))


@router.post("/settings/panel-update/rollback/{update_id}")
def panel_update_rollback(
    update_id: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
):
    """Откатиться к бэкапу указанного апдейта. Шаг-ап обязателен."""
    from .. import panel_update as _upd
    user = getattr(request.state, "user", None)
    if (sr := stepup_check_or_redirect(request)) is not None:
        return sr

    initiator = user.username if user else "ui:unknown"
    try:
        rb_id = _upd.execute_rollback(
            db, src_update_id=update_id,
            initiated_by=initiator,
        )
    except RuntimeError as e:
        return _update_redirect(err=f"Rollback failed: {e}")

    audit(db, "panel.update.rollback", None,
          summary=f"{initiator} rollback to backup of update #{update_id} "
                  f"(new id={rb_id})")
    db.commit()
    return _update_redirect(
        msg=tr("settings.update_rollback_started", id=rb_id))


@router.get("/settings/panel-update/status/{update_id}")
def panel_update_status(
    update_id: int,
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """JSON со статусом апдейта (для AJAX-polling из UI)."""
    from .. import panel_update as _upd
    rec = _upd.get_history(db, update_id)
    if not rec:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return JSONResponse({"ok": True, "update": rec})
