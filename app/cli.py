"""Roboport CLI. Запускается с консоли сервера.

Без аргументов открывается интерактивное меню. С аргументом — выполняет команду:

  Информация:
    .venv/bin/python -m app.cli status                  # статус панели и общие счётчики
    .venv/bin/python -m app.cli logs [N]                # journalctl -u roboport -n N
    .venv/bin/python -m app.cli logs follow             # живые логи (journalctl -f)

  Пользователи:
    .venv/bin/python -m app.cli setup-code
    .venv/bin/python -m app.cli list-users
    .venv/bin/python -m app.cli reset-password <логин>
    .venv/bin/python -m app.cli emergency-access [host[:port]]
    .venv/bin/python -m app.cli revoke-all-sessions

  IP-баны:
    .venv/bin/python -m app.cli ban-list
    .venv/bin/python -m app.cli unban <ip>
    .venv/bin/python -m app.cli ban <ip> [<duration_min>] [<reason>]

  Сервис:
    .venv/bin/python -m app.cli restart                 # systemctl restart roboport

  2FA emergency:
    .venv/bin/python -m app.cli disable-2fa <логин>     # сбросить, если потерял authenticator
"""
from __future__ import annotations

import getpass
import subprocess
import sys
import time

from . import auth as auth_lib
from .db import connect, init_db


def cmd_reset_password(username: str) -> int:
    init_db()
    with connect() as conn:
        user = auth_lib.get_user_by_username(conn, username.strip().lower())
        if not user:
            print(f"Пользователь {username!r} не найден.", file=sys.stderr)
            return 2
        print(f"Сброс пароля для {user['username']!r}.")
        new1 = getpass.getpass("Новый пароль: ")
        new2 = getpass.getpass("Ещё раз:      ")
        if new1 != new2:
            print("Пароли не совпали.", file=sys.stderr)
            return 3
        try:
            auth_lib.update_password(conn, user["id"], new1)
        except auth_lib.AuthError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 4
        n = auth_lib.revoke_user_sessions(conn, user["id"])
        conn.commit()
        print(f"Пароль обновлён. Сессий снесено: {n}.")
        return 0


def cmd_setup_code() -> int:
    """Print a one-time first-installation code only to the operator's terminal."""
    init_db()
    with connect() as conn:
        code = auth_lib.issue_setup_code(conn)
    if code is None:
        print("Roboport already has an administrator.")
    else:
        print(f"Roboport setup code (10 min): {code}")
    return 0


def cmd_list_users() -> int:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at, last_login_at, last_login_ip "
            "FROM users ORDER BY id"
        ).fetchall()
    if not rows:
        print("Для создания первого администратора выполните setup-code и откройте /setup.")
        return 0
    print(f"{'id':>3}  {'username':16}  {'last_login':12}  last_ip")
    for r in rows:
        print(f"{r['id']:>3}  {r['username']:16}  "
              f"{(r['last_login_at'] or '—'):>12}  {r['last_login_ip'] or '—'}")
    return 0


def _detect_panel_host() -> str:
    """Угадать host:port по которому реально доступна панель — для печати URL.
    Смотрим на ExecStart в systemd-юните либо просто берём первый non-loopback
    адрес. Возвращаем 'host:port'."""
    import re
    import socket
    port = "8000"
    try:
        with open("/etc/systemd/system/roboport.service", "r", encoding="utf-8") as f:
            for line in f:
                m = re.search(r"--port\s+(\d+)", line)
                if m:
                    port = m.group(1)
                    break
    except OSError:
        pass
    # Берём IP первого non-loopback интерфейса (получим LAN-адрес VM)
    host = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))   # не отправляет, только для routing-decision
        host = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    return f"{host}:{port}"


def cmd_emergency_access(host_override: str | None = None) -> int:
    """Сгенерить one-time recovery code. Plain-код показывается ТОЛЬКО здесь
    в stdout. В БД лежит sha256.
    TTL 10 минут. Юзер вводит код на странице /recovery вместе с новым паролем."""
    init_db()
    with connect() as conn:
        plain = auth_lib.create_recovery_token(conn)
        auth_lib.cleanup_old_recovery_tokens(conn)
        conn.commit()
    host = host_override or _detect_panel_host()
    pretty = auth_lib.format_recovery_token_for_display(plain)
    bar = "═" * 70
    print()
    print(bar)
    print(" ⚡ RECOVERY CODE — введи на странице восстановления")
    print(bar)
    print()
    print(f"   CODE     :  {pretty}")
    print(f"   URL формы:  http://{host}/recovery")
    print()
    print(f"   Действует:    {auth_lib.RECOVERY_TTL_SEC // 60} минут")
    print(f"   Регистр:      не важен, дефис не важен")
    print(f"   После reset:  все сессии целевого юзера разлогинены")
    print()
    print(" Никому не светите код. Audit-log запись будет создана.")
    print(bar)
    print()
    return 0


def cmd_ban_list() -> int:
    init_db()
    with connect() as conn:
        bans = auth_lib.list_active_bans(conn)
    if not bans:
        print("Активных банов нет.")
        return 0
    import time as _t
    now = int(_t.time())
    print(f"{'IP':18}  {'until':>10}  auto?  reason")
    for b in bans:
        if b["banned_until"] == 0:
            until = "permanent"
        else:
            rem = b["banned_until"] - now
            until = f"{rem // 60}m {rem % 60}s"
        auto = "yes" if b["auto"] else "no"
        print(f"{b['ip']:18}  {until:>10}  {auto:>5}  {b['reason'] or '—'}")
    return 0


def cmd_unban(ip: str) -> int:
    init_db()
    with connect() as conn:
        ok = auth_lib.unban_ip(conn, ip)
        conn.commit()
    if ok:
        print(f"Разбанен: {ip}")
        return 0
    print(f"{ip} не найден в списке банов (или истёк сам).")
    return 1


def cmd_ban(ip: str, duration_min: int = 0, reason: str = "manual") -> int:
    init_db()
    with connect() as conn:
        auth_lib.ban_ip(conn, ip, duration_sec=duration_min * 60,
                        reason=reason, auto=False)
        conn.commit()
    when = "permanent" if duration_min == 0 else f"{duration_min} min"
    print(f"Забанен {ip} ({when}, reason: {reason!r}).")
    return 0


def cmd_disable_2fa(username: str) -> int:
    """Emergency-выключение 2FA. Полезно если юзер потерял устройство с
    authenticator'ом и не может пройти TOTP-шаг при логине."""
    init_db()
    with connect() as conn:
        user = auth_lib.get_user_by_username(conn, username.strip().lower())
        if not user:
            print(f"Пользователь {username!r} не найден.", file=sys.stderr)
            return 2
        was_configured = bool(user.get("totp_enabled") or user.get("totp_secret"))
        auth_lib.disable_user_totp(conn, user["id"])
        if not was_configured:
            print(f"У {user['username']!r} 2FA и так не настроена.")
            return 0
        # Заодно сносим все его сессии — на всякий случай
        n = auth_lib.revoke_user_sessions(conn, user["id"])
        conn.commit()
    print(f"2FA отключена для {user['username']!r}. Сессий снесено: {n}.")
    return 0


def cmd_revoke_all_sessions() -> int:
    init_db()
    with connect() as conn:
        n = conn.execute("DELETE FROM sessions").rowcount
        conn.commit()
    print(f"Снесено сессий: {n}.")
    return 0


# =============================================================================
# Service-control + логи (systemd)
# =============================================================================

SERVICE_NAME = "roboport"


def _systemctl(action: str, *, sudo: bool = False) -> int:
    """Запустить systemctl <action> roboport. Возвращает exit code.
    sudo=True — пробует через sudo, иначе напрямую (для root-сценария)."""
    cmd = ["systemctl", action, SERVICE_NAME]
    if sudo:
        cmd = ["sudo", "-n"] + cmd  # -n чтобы не висеть на пароле
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print("systemctl не найден (это не Linux/systemd?)", file=sys.stderr)
        return 1


def cmd_status() -> int:
    """Краткий статус панели + счётчики из БД."""
    init_db()
    bar = "═" * 60
    print(bar)
    print(" Roboport — статус")
    print(bar)
    # systemctl status (без --no-pager — может пейджернуться, но это интерактив)
    rc = subprocess.call(["systemctl", "status", SERVICE_NAME, "--no-pager", "-n", "0"])
    print(bar)
    with connect() as conn:
        users = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        sess = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        bans = len(auth_lib.list_active_bans(conn))
        servers = int(conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0])
        last_login = conn.execute(
            "SELECT username, last_login_at, last_login_ip FROM users "
            "WHERE last_login_at IS NOT NULL ORDER BY last_login_at DESC LIMIT 1"
        ).fetchone()
    print(f"  Пользователей панели : {users}")
    print(f"  Активных сессий      : {sess}")
    print(f"  Активных IP-банов    : {bans}")
    print(f"  Factorio-серверов    : {servers}")
    if last_login:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_login["last_login_at"]))
        print(f"  Последний логин      : {last_login['username']} from "
              f"{last_login['last_login_ip']} at {when}")
    print(bar)
    return 0 if rc == 0 else rc


def cmd_logs(n_or_follow: str = "50") -> int:
    """journalctl -u roboport -n N (или -f при follow)."""
    if n_or_follow in ("follow", "-f", "f", "live"):
        cmd = ["journalctl", "-u", SERVICE_NAME, "-f"]
    else:
        try:
            n = int(n_or_follow)
        except ValueError:
            n = 50
        cmd = ["journalctl", "-u", SERVICE_NAME, "-n", str(n), "--no-pager"]
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print("journalctl не найден.", file=sys.stderr)
        return 1


def cmd_restart_service() -> int:
    print("Перезапускаю roboport.service…")
    rc = _systemctl("restart", sudo=False)
    if rc != 0:
        # Если не от root — попробуем sudo
        rc = _systemctl("restart", sudo=True)
    if rc == 0:
        time.sleep(2)
        subprocess.call(["systemctl", "is-active", SERVICE_NAME])
    return rc


# =============================================================================
# Интерактивное меню
# =============================================================================

def _menu_header() -> None:
    bar = "═" * 60
    print()
    print(bar)
    print(" Roboport — управление с консоли")
    print(bar)
    init_db()
    # Service status
    try:
        active = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        active = "unknown"
    if active == "active":
        status_str = "● active (running)"
    else:
        status_str = f"○ {active}"
    print(f"  Service     : {status_str}")
    with connect() as conn:
        users = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        sess = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        bans = len(auth_lib.list_active_bans(conn))
        servers = int(conn.execute("SELECT COUNT(*) FROM servers").fetchone()[0])
        fail24 = int(conn.execute(
            "SELECT COUNT(*) FROM auth_attempts WHERE success = 0 AND ts >= ?",
            (int(time.time()) - 24 * 3600,),
        ).fetchone()[0])
    print(f"  Counts      : users {users} · sessions {sess} · "
          f"bans {bans} · factorio-servers {servers}")
    print(f"  Auth fails  : {fail24} за последние 24ч")
    print(bar)


def _menu_options() -> list[tuple[str, str, callable]]:
    """Список (key, label, action). action возвращает None или int (rc)."""
    return [
        ("1", "📊  Показать полный статус", cmd_status),
        ("2", "📜  Логи (последние 50)", lambda: cmd_logs("50")),
        ("3", "📜  Логи (live, Ctrl+C чтобы вернуться)", lambda: cmd_logs("follow")),
        ("4", "🔄  Перезапустить roboport.service", cmd_restart_service),
        ("",  "", None),  # разделитель
        ("5", "👥  Список пользователей", cmd_list_users),
        ("6", "🔑  Сбросить пароль пользователя", _menu_reset_password),
        ("7", "🚪  Снести все сессии (всех)", cmd_revoke_all_sessions),
        ("",  "", None),
        ("8", "⚡  Выписать recovery-код для входа", lambda: cmd_emergency_access(None)),
        ("9", "🔐  Выключить 2FA для пользователя (emergency)", _menu_disable_2fa),
        ("",  "", None),
        ("10", "🚫 Список IP-банов", cmd_ban_list),
        ("11", "🔓 Снять бан с IP", _menu_unban),
        ("12", "🔒 Забанить IP вручную", _menu_ban),
        ("",  "", None),
        ("0", "❌  Выход", None),
    ]


def _menu_reset_password() -> int:
    user = input("  Логин: ").strip()
    return cmd_reset_password(user) if user else 1


def _menu_disable_2fa() -> int:
    user = input("  Логин: ").strip()
    return cmd_disable_2fa(user) if user else 1


def _menu_unban() -> int:
    ip = input("  IP: ").strip()
    return cmd_unban(ip) if ip else 1


def _menu_ban() -> int:
    ip = input("  IP: ").strip()
    if not ip:
        return 1
    dur_str = input("  На сколько минут? (пусто = permanent): ").strip()
    try:
        dur = int(dur_str) if dur_str else 0
    except ValueError:
        print("  Должно быть число")
        return 1
    reason = input("  Причина (опц.): ").strip() or "manual"
    return cmd_ban(ip, dur, reason)


def cmd_menu() -> int:
    """Интерактивный цикл."""
    while True:
        _menu_header()
        opts = _menu_options()
        print()
        for key, label, _ in opts:
            if not key:
                print()
                continue
            print(f"  [{key}] {label}")
        print()
        try:
            choice = input("  Выбор: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == "0" or choice.lower() in ("q", "quit", "exit"):
            return 0
        action = next((a for k, _, a in opts if k == choice and a is not None), None)
        if action is None:
            print(f"  Неизвестный пункт: {choice!r}")
            input("  Enter чтобы продолжить…")
            continue
        try:
            action()
        except KeyboardInterrupt:
            print("\n  Прервано.")
        except Exception as e:
            print(f"  Ошибка: {type(e).__name__}: {e}")
        print()
        input("  Enter чтобы вернуться в меню…")


def main(argv: list[str]) -> int:
    if not argv:
        return cmd_menu()
    cmd = argv[0]
    if cmd == "reset-password":
        if len(argv) != 2:
            print("usage: reset-password <username>", file=sys.stderr)
            return 1
        return cmd_reset_password(argv[1])
    if cmd == "setup-code":
        return cmd_setup_code()
    if cmd == "list-users":
        return cmd_list_users()
    if cmd == "emergency-access":
        host_override = argv[1] if len(argv) > 1 else None
        return cmd_emergency_access(host_override)
    if cmd == "revoke-all-sessions":
        return cmd_revoke_all_sessions()
    if cmd == "ban-list":
        return cmd_ban_list()
    if cmd == "unban":
        if len(argv) != 2:
            print("usage: unban <ip>", file=sys.stderr); return 1
        return cmd_unban(argv[1])
    if cmd == "ban":
        if len(argv) < 2:
            print("usage: ban <ip> [<duration_min>] [<reason>]", file=sys.stderr); return 1
        ip = argv[1]
        dur = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 0
        reason = " ".join(argv[3:]) if len(argv) > 3 else ("manual" if dur == 0 else "manual")
        return cmd_ban(ip, dur, reason)
    if cmd == "disable-2fa":
        if len(argv) != 2:
            print("usage: disable-2fa <username>", file=sys.stderr); return 1
        return cmd_disable_2fa(argv[1])
    if cmd == "status":
        return cmd_status()
    if cmd == "logs":
        arg = argv[1] if len(argv) > 1 else "50"
        return cmd_logs(arg)
    if cmd == "restart":
        return cmd_restart_service()
    if cmd == "menu":
        return cmd_menu()
    print(f"Unknown command: {cmd!r}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
