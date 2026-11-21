#!/usr/bin/env bash
# Roboport — one-click installer для Linux (Debian/Ubuntu/Fedora/Arch).
# Запуск: bash deploy/install.sh   (можно sudo, можно от обычного юзера — sudo дёрнем сами)
#
# Что делает:
#   1. Ставит системные пакеты: python3, venv, git, docker, curl, tzdata
#   2. Включает docker.service и добавляет тебя в группу docker (если не root)
#   3. Создаёт .venv и ставит зависимости из requirements.txt
#   4. (опц.) Кладёт systemd-unit в /etc/systemd/system/roboport.service,
#      подменив пути/юзера/host на текущие, и enable --now
#   5. (опц.) Ставит nginx-конфиг с basic-auth
#
# Идемпотентен: повторный запуск ничего не ломает.
#
# Unattended-режим (без интерактивных вопросов):
#   INSTALL_SYSTEMD=yes INSTALL_NGINX=yes INSTALL_TLS=selfsigned bash deploy/install.sh
#
# Поддерживаемые env-переменные:
#   INSTALL_SYSTEMD = yes|no       (default: yes, если задана; иначе спрашиваем)
#   INSTALL_NGINX   = yes|no       (default: no, если задана; иначе спрашиваем)
#   INSTALL_TLS     = selfsigned|certbot|none
#                                  default: none (если INSTALL_NGINX=no)
#                                  - selfsigned: генерим cert через openssl + nginx-конфиг
#                                                с TLS на :443 + редирект 80→443.
#                                                BIND_HOST принудительно 127.0.0.1.
#                                  - certbot: ставим nginx (HTTP only), юзер сам
#                                             запускает certbot --nginx -d <domain>.
#                                  - none: nginx-конфиг с обычным HTTP без TLS.
#   BIND_HOST       = <ip>         (default: 127.0.0.1 — loopback, наружу через nginx)
#                                  Принудительно 127.0.0.1 если INSTALL_TLS=selfsigned.
#                                  Поставь 0.0.0.0 чтобы слушать наружу напрямую без
#                                  nginx — внутренний auth панели прикрывает периметр,
#                                  но HTTPS у тебя нет, ключи/пароли в clear text.
#   SERVER_NAME     = <hostname>   (default: '_' = catch-all). Для Let's Encrypt
#                                   замени на свой реальный домен.
#   NGINX_HTPASSWD_PASS = <pw>     (опционально: создать nginx-basic-auth юзера 'admin'
#                                   как опциональный второй слой защиты)
#
# Если stdin не TTY (scripted/CI) — все ask_yn автоматически берут default,
# не спрашивая.

set -e
set -o pipefail

INSTALL_TLS="${INSTALL_TLS:-none}"
SERVER_NAME="${SERVER_NAME:-_}"
# Если включён TLS — uvicorn слушает только loopback на :8001, nginx занимает
# :8000 для redirect старых URL на :443. Без TLS uvicorn остаётся на :8000
# для backward compat.
if [ "$INSTALL_TLS" = "selfsigned" ] || [ "$INSTALL_TLS" = "certbot" ]; then
    BIND_HOST="${BIND_HOST:-127.0.0.1}"
    UVICORN_PORT="${UVICORN_PORT:-8001}"
else
    BIND_HOST="${BIND_HOST:-127.0.0.1}"
    UVICORN_PORT="${UVICORN_PORT:-8000}"
fi

# ---------- helpers ----------
say()  { printf '\033[1;36m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; }

# yn-парсер для env-значений: yes/y/1/true → 0, no/n/0/false → 1, иначе 2 (не задано)
_parse_yn() {
    case "${1:-}" in
        y|Y|yes|YES|Yes|1|true|TRUE|True)   return 0 ;;
        n|N|no|NO|No|0|false|FALSE|False)   return 1 ;;
        *)                                  return 2 ;;
    esac
}

ask_yn() {
    # ask_yn "вопрос" "default-y-or-n" "ENV_VAR_NAME"
    # Приоритет: env-переменная > stdin-not-tty (использует default) > интерактивный prompt.
    local q="$1" def="${2:-n}" env_name="${3:-}"

    # 1) env-переменная переопределяет всё
    if [ -n "$env_name" ]; then
        local val="${!env_name:-}"
        if [ -n "$val" ]; then
            _parse_yn "$val"
            local rc=$?
            if [ $rc -ne 2 ]; then
                # Печатаем что решили, чтобы лог установки был читаем
                if [ $rc -eq 0 ]; then
                    say "$q → yes (по env $env_name=$val)"
                else
                    say "$q → no (по env $env_name=$val)"
                fi
                return $rc
            fi
            warn "$env_name=$val не распознано, использую default '$def'"
        fi
    fi

    # 2) stdin не TTY — берём default без вопросов
    if [ ! -t 0 ]; then
        if [ "$def" = "y" ] || [ "$def" = "Y" ]; then
            say "$q → yes (default, stdin не TTY)"
            return 0
        else
            say "$q → no (default, stdin не TTY)"
            return 1
        fi
    fi

    # 3) интерактивный prompt
    local prompt reply
    if [ "$def" = "y" ]; then prompt="$q [Y/n] "; else prompt="$q [y/N] "; fi
    read -r -p "$prompt" reply
    reply="${reply:-$def}"
    [ "$reply" = "y" ] || [ "$reply" = "Y" ]
}

# ---------- определяем что есть ----------
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
say "Корень проекта: $PROJECT_DIR"

if [ "$EUID" -eq 0 ]; then
    SUDO=""
    PANEL_USER="${SUDO_USER:-root}"
else
    SUDO="sudo"
    PANEL_USER="$USER"
    if ! command -v sudo >/dev/null; then
        err "Нужен sudo (или запусти от root). На минимальных Debian-LXC: apt install sudo"
        exit 1
    fi
fi
say "Будем ставить под пользователя: $PANEL_USER"

# package manager
if   command -v apt-get >/dev/null; then PKG=apt
elif command -v dnf     >/dev/null; then PKG=dnf
elif command -v pacman  >/dev/null; then PKG=pacman
elif command -v zypper  >/dev/null; then PKG=zypper
else
    err "Не нашёл apt/dnf/pacman/zypper. Поставь python3, python3-venv, git, docker вручную и перезапусти."
    exit 1
fi
say "Менеджер пакетов: $PKG"

# ---------- 1) системные пакеты ----------
say "Ставлю системные пакеты"
case "$PKG" in
    apt)
        export DEBIAN_FRONTEND=noninteractive
        $SUDO apt-get update -qq
        # tzdata явно — без него на минимальном LXC stdlib zoneinfo не найдёт
        # IANA-имена (Europe/Moscow и т.п.) для PANEL_TZ.
        $SUDO apt-get install -y --no-install-recommends \
            python3 python3-venv python3-pip git curl ca-certificates docker.io tzdata
        ;;
    dnf)
        $SUDO dnf install -y python3 python3-pip git curl ca-certificates docker tzdata
        ;;
    pacman)
        $SUDO pacman -Sy --noconfirm --needed python python-pip git curl ca-certificates docker tzdata
        ;;
    zypper)
        $SUDO zypper --non-interactive install python3 python3-pip git curl ca-certificates docker timezone
        ;;
esac
ok "Системные пакеты на месте"

# ---------- 2) docker daemon ----------
if command -v systemctl >/dev/null; then
    say "Включаю docker daemon"
    $SUDO systemctl enable --now docker || warn "не получилось enable --now docker (возможно, ты в LXC без systemd-internals)"
fi
# Группа docker нужна panel-юзеру независимо от того, как запущен install.sh:
# и при `bash install.sh` (EUID!=0, юзер он же), и при `sudo bash install.sh`
# (EUID=0, $SUDO_USER он же — выставлено выше как PANEL_USER).
if [ "$PANEL_USER" != "root" ] && ! id -nG "$PANEL_USER" 2>/dev/null | grep -qw docker; then
    say "Добавляю $PANEL_USER в группу docker"
    $SUDO usermod -aG docker "$PANEL_USER"
    warn "Перелогинься (или 'newgrp docker') чтобы группа подхватилась — иначе docker-команды будут падать с permission denied"
fi

# ---------- 3) venv + зависимости ----------
cd "$PROJECT_DIR"
if [ ! -d .venv ]; then
    say "Создаю .venv"
    python3 -m venv .venv
fi
say "Ставлю Python-зависимости"
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt
ok "Зависимости поставлены"

# ---------- 3.5) .env ----------
if [ ! -f .env ]; then
    say "Создаю .env из .env.example"
    cp .env.example .env
    warn "Отредактируй .env (FACTORIO_USERNAME/TOKEN/PANEL_TZ) — или сделаешь это потом через UI: /settings"
fi
# Файл содержит FACTORIO_TOKEN в plain-text — не даём другим UID'ам читать.
chmod 600 .env 2>/dev/null || true

# ---------- 3.9) chown PROJECT_DIR на PANEL_USER ----------
# При запуске через `sudo bash install.sh` EUID=0, и созданные нами .env, .venv/,
# любые pip-кешированные __pycache__/ принадлежат root:root. Панель потом крутится
# под $PANEL_USER (см. systemd-unit User=) и не может читать .env (mode 600) /
# писать в .venv/ — отсюда "PermissionError: '.env'" и зацикленный fail.
# Возвращаем владение пользователю — единократный chown -R перед systemd-секцией.
if [ "$EUID" -eq 0 ] && [ "$PANEL_USER" != "root" ]; then
    say "chown $PROJECT_DIR → $PANEL_USER (запущены под sudo)"
    chown -R "$PANEL_USER:$PANEL_USER" "$PROJECT_DIR"
fi

# ---------- 4) systemd unit ----------
if command -v systemctl >/dev/null && [ -d /etc/systemd/system ]; then
    if ask_yn "Установить systemd-unit и автозапуск панели?" "y" "INSTALL_SYSTEMD"; then
        UNIT_SRC="$PROJECT_DIR/deploy/roboport.service"
        UNIT_DST="/etc/systemd/system/roboport.service"
        say "Кладу $UNIT_DST (BIND_HOST=$BIND_HOST)"
        $SUDO bash -c "sed \
            -e 's|__PROJECT_DIR__|$PROJECT_DIR|g' \
            -e 's|__PANEL_USER__|$PANEL_USER|g' \
            -e 's|__BIND_HOST__|$BIND_HOST|g' \
            -e 's|__UVICORN_PORT__|$UVICORN_PORT|g' \
            '$UNIT_SRC' > '$UNIT_DST'"
        $SUDO systemctl daemon-reload
        $SUDO systemctl enable --now roboport
        sleep 2
        if $SUDO systemctl is-active --quiet roboport; then
            ok "roboport.service запущен — http://${BIND_HOST}:8000/"
            if [ "$BIND_HOST" = "0.0.0.0" ]; then
                warn "Слушаешь 0.0.0.0 без nginx — внутренний auth панели прикроет периметр (логин/2FA), но HTTPS у тебя нет. Для интернет-доступа поставь nginx + certbot."
            fi
        else
            err "roboport не поднялся. Логи: journalctl -u roboport -n 50 --no-pager"
        fi

        # The self-update watchdog is a separate user transient unit.  Linger
        # keeps this manager and its bus available while the system service is
        # restarted (there is normally no interactive login for PANEL_USER).
        if [ "$PANEL_USER" != "root" ] && command -v loginctl >/dev/null 2>&1; then
            say "Включаю persistent user systemd manager для self-update watchdog"
            if $SUDO loginctl enable-linger "$PANEL_USER"; then
                PANEL_UID="$(id -u "$PANEL_USER")"
                $SUDO systemctl start "user@${PANEL_UID}.service" 2>/dev/null || true
                ok "User manager для $PANEL_USER сохранён после logout"
            else
                warn "Не удалось включить linger для $PANEL_USER: self-update через UI будет недоступен"
            fi
        fi

        # Sudoers entry для self-update панели — только systemctl restart roboport.
        # Создаётся всегда при INSTALL_SYSTEMD=yes (даже без TLS), потому что UI-кнопка
        # «Обновить панель» и watchdog после auto-rollback'а требуют рестарта без пароля.
        say "Создаю /etc/sudoers.d/roboport-update (NOPASSWD restart roboport)"
        $SUDO bash -c "cat > /etc/sudoers.d/roboport-update" <<UPDSUDO
# Auto-generated by Roboport install.sh — нужно для self-update через UI.
# Watchdog (deploy/panel-update-watchdog.sh) после установки нового кода
# делает systemctl restart roboport; без NOPASSWD он повиснет.
$PANEL_USER ALL=(root) NOPASSWD: /bin/systemctl restart roboport
$PANEL_USER ALL=(root) NOPASSWD: /usr/sbin/systemctl restart roboport
UPDSUDO
        $SUDO chmod 440 /etc/sudoers.d/roboport-update
        $SUDO visudo -c -f /etc/sudoers.d/roboport-update
        ok "Sudoers entry для self-update установлен"
    fi
fi

# ---------- 5) nginx + TLS (опционально) ----------
if ask_yn "Поставить nginx (для TLS / интернет-доступа)?" "n" "INSTALL_NGINX"; then
    case "$PKG" in
        apt)    $SUDO apt-get install -y nginx apache2-utils openssl ;;
        dnf)    $SUDO dnf install -y nginx httpd-tools openssl ;;
        pacman) $SUDO pacman -S --noconfirm --needed nginx apache openssl ;;
        zypper) $SUDO zypper --non-interactive install nginx apache2-utils openssl ;;
    esac

    # ----- TLS-сертификат -----
    SSL_CERT=""; SSL_KEY=""
    case "$INSTALL_TLS" in
        selfsigned)
            # Avahi-daemon рекламирует <hostname>.local в LAN (как HomeAssistant
            # делает homeassistant.local). После установки можно открыть панель
            # через https://roboport.local/ — без необходимости помнить IP.
            # mDNS поддерживается всеми современными ОС: Windows 10+, macOS,
            # iOS, Android (Pie+), Linux (через nss-mdns).
            say "Ставлю avahi-daemon для mDNS (<hostname>.local)"
            case "$PKG" in
                apt)    $SUDO apt-get install -y avahi-daemon ;;
                dnf)    $SUDO dnf install -y avahi ;;
                pacman) $SUDO pacman -S --noconfirm --needed avahi nss-mdns ;;
                zypper) $SUDO zypper --non-interactive install avahi ;;
            esac
            $SUDO systemctl enable --now avahi-daemon 2>/dev/null || \
                warn "не получилось enable avahi-daemon (LXC?)"

            say "Генерирую self-signed TLS-сертификат (host: $SERVER_NAME)"
            CERT_DIR="/etc/nginx/roboport-tls"
            CERT_HOST="$SERVER_NAME"
            [ "$CERT_HOST" = "_" ] && CERT_HOST=""  # autodetect в gen-script
            # gen-self-signed автоматически добавит hostname + .local + все
            # non-loopback IP. Достаточно передать primary host (для CN).
            $SUDO env SELFSIGNED_FORCE=1 \
                bash "$PROJECT_DIR/deploy/gen-self-signed.sh" "$CERT_HOST" "$CERT_DIR"
            SSL_CERT="$CERT_DIR/cert.pem"
            SSL_KEY="$CERT_DIR/key.pem"
            ok "Self-signed cert: $SSL_CERT"

            # Sudoers entry для UI-кнопок «Перевыпустить cert» / «Загрузить cert».
            # Whitelist на конкретные команды — panel-юзер может их без пароля,
            # на остальное sudo по-прежнему просит пароль.
            say "Создаю /etc/sudoers.d/roboport-tls (NOPASSWD whitelist)"
            $SUDO bash -c "cat > /etc/sudoers.d/roboport-tls" <<SUDOEOF
# Auto-generated by Roboport install.sh — UI-actions для обновления TLS-cert.
# Без этого UI-кнопка «Перевыпустить cert» не работает (нужен write в
# /etc/nginx/roboport-tls/ + nginx reload).
$PANEL_USER ALL=(root) NOPASSWD: $PROJECT_DIR/deploy/gen-self-signed.sh
$PANEL_USER ALL=(root) NOPASSWD: /usr/sbin/nginx -t
$PANEL_USER ALL=(root) NOPASSWD: /bin/systemctl reload nginx
$PANEL_USER ALL=(root) NOPASSWD: /usr/sbin/systemctl reload nginx
$PANEL_USER ALL=(root) NOPASSWD: /usr/bin/install -m 644 -o root -g root /tmp/roboport-cert-upload-* $CERT_DIR/cert.pem
$PANEL_USER ALL=(root) NOPASSWD: /usr/bin/install -m 600 -o root -g root /tmp/roboport-cert-upload-* $CERT_DIR/key.pem
$PANEL_USER ALL=(root) NOPASSWD: /usr/bin/install -m 644 -o root -g root /tmp/roboport-nginx-* /etc/nginx/sites-available/roboport
$PANEL_USER ALL=(root) NOPASSWD: /usr/bin/install -m 644 -o root -g root /tmp/roboport-avahi-* /etc/avahi/avahi-daemon.conf
$PANEL_USER ALL=(root) NOPASSWD: /bin/systemctl restart avahi-daemon
$PANEL_USER ALL=(root) NOPASSWD: /usr/sbin/systemctl restart avahi-daemon
$PANEL_USER ALL=(root) NOPASSWD: /bin/systemctl restart roboport
$PANEL_USER ALL=(root) NOPASSWD: /usr/sbin/systemctl restart roboport
SUDOEOF
            $SUDO chmod 440 /etc/sudoers.d/roboport-tls
            $SUDO visudo -c -f /etc/sudoers.d/roboport-tls
            ok "Sudoers entry установлен (whitelist на cert-обновление)"
            ;;
        certbot)
            say "INSTALL_TLS=certbot — ставлю nginx без cert. После этого:"
            echo "    certbot certonly --webroot -w /var/www/certbot -d <твой-домен>"
            echo "    либо: certbot --nginx -d <твой-домен>"
            echo "  certbot сам отредактирует ssl_* директивы в nginx-конфиге."
            $SUDO mkdir -p /var/www/certbot
            ;;
        none|"")
            say "INSTALL_TLS=none — nginx будет работать по HTTP без TLS"
            ;;
        *)
            warn "Неизвестный INSTALL_TLS=$INSTALL_TLS, использую 'none'"
            INSTALL_TLS="none"
            ;;
    esac

    # ----- nginx-конфиг -----
    NGINX_CONF="$PROJECT_DIR/deploy/roboport.nginx.conf"
    DST="/etc/nginx/sites-available/roboport"
    if [ ! -d /etc/nginx/sites-available ]; then
        # RHEL/Arch — кладём в conf.d
        DST="/etc/nginx/conf.d/roboport.conf"
    fi
    say "Кладу $DST"
    if [ "$INSTALL_TLS" = "selfsigned" ]; then
        # Полный TLS-конфиг с подстановкой cert/key/server_name/project_dir/port
        $SUDO bash -c "sed \
            -e 's|__PROJECT_DIR__|$PROJECT_DIR|g' \
            -e 's|__SERVER_NAME__|$SERVER_NAME|g' \
            -e 's|__SSL_CERT__|$SSL_CERT|g' \
            -e 's|__SSL_KEY__|$SSL_KEY|g' \
            -e 's|__UVICORN_PORT__|$UVICORN_PORT|g' \
            '$NGINX_CONF' > '$DST'"
    else
        # HTTP-only fallback: вырезаем TLS-блок (server :443) и редирект на HTTPS,
        # оставляем простой proxy на :80. Достигается через быструю ad-hoc-замену:
        # генерируем урезанный конфиг inline.
        $SUDO bash -c "cat > '$DST'" <<NGINXEOF
upstream roboport {
    server 127.0.0.1:$UVICORN_PORT;
    keepalive 16;
}

server {
    listen 80;
    listen [::]:80;
    server_name $SERVER_NAME;

    client_max_body_size 200M;
    access_log /var/log/nginx/roboport.access.log;
    error_log  /var/log/nginx/roboport.error.log;

    # ACME challenge для будущей выдачи certbot --webroot.
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass http://roboport;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host  \$host;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout  3600s;
        proxy_send_timeout  3600s;
        proxy_buffering     off;
    }

    location /static/ {
        alias $PROJECT_DIR/app/static/;
        expires 1h;
        add_header Cache-Control "public";
        access_log off;
    }
}
NGINXEOF
    fi

    if [ -d /etc/nginx/sites-enabled ] && [ ! -L /etc/nginx/sites-enabled/roboport ]; then
        $SUDO ln -s "$DST" /etc/nginx/sites-enabled/roboport
    fi
    # Снять default catch-all site если он включен — он слушает 80 catch-all
    # и может конфликтовать с нашим (особенно при server_name=_).
    if [ -L /etc/nginx/sites-enabled/default ]; then
        say "Отключаю /etc/nginx/sites-enabled/default (был в conflict с нашим catch-all)"
        $SUDO rm -f /etc/nginx/sites-enabled/default
    fi

    # ----- htpasswd (опциональный второй слой) -----
    if [ -n "${NGINX_HTPASSWD_PASS:-}" ]; then
        if [ ! -f /etc/nginx/.roboport.htpasswd ]; then
            say "Создаю basic-auth юзера 'admin' (NGINX_HTPASSWD_PASS)"
            $SUDO htpasswd -bc /etc/nginx/.roboport.htpasswd admin "$NGINX_HTPASSWD_PASS"
            warn "basic-auth настроен, но в текущем конфиге раскомментировать его надо вручную ($DST)"
        fi
    fi

    # ----- запуск -----
    if $SUDO nginx -t 2>&1 | grep -q "successful"; then
        $SUDO systemctl enable --now nginx
        $SUDO systemctl reload nginx
        case "$INSTALL_TLS" in
            selfsigned)
                ok "nginx + TLS подхватил конфиг. Открой https://$SERVER_NAME/"
                warn "Self-signed cert — браузер покажет warning, кликни «продолжить» один раз."
                ;;
            certbot)
                ok "nginx (HTTP) запущен. Дальше выпуск cert через certbot:"
                echo "    sudo certbot certonly --webroot -w /var/www/certbot -d $SERVER_NAME"
                echo "    sudo certbot --nginx -d $SERVER_NAME   # (альтернатива — certbot сам всё сделает)"
                ;;
            *)
                ok "nginx подхватил конфиг (HTTP). Открой http://$SERVER_NAME/"
                ;;
        esac
    else
        warn "nginx -t ругается — проверь $DST и переподними руками"
    fi
fi

echo
ok "Готово."
if [ "$EUID" -eq 0 ] && [ "$PANEL_USER" != "root" ]; then
    sudo -u "$PANEL_USER" .venv/bin/python -m app.cli setup-code
else
    .venv/bin/python -m app.cli setup-code
fi
echo "  Открыть:   http://${BIND_HOST}:8000/  (на первом заходе → /setup для админа)"
echo "  Логи:      journalctl -u roboport -f"
echo "  CLI:       cd $PROJECT_DIR && .venv/bin/python -m app.cli  (меню: пароль/2FA/баны/recovery)"
echo "  Ручной запуск: cd $PROJECT_DIR && .venv/bin/python -m uvicorn app.main:app --host $BIND_HOST --port 8000"
