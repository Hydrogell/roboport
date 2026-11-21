#!/usr/bin/env bash
# Roboport — генератор self-signed TLS-сертификата.
# Запуск: bash deploy/gen-self-signed.sh [host-or-ip] [output-dir]
#
# По умолчанию:
#   host-or-ip  — autodetect (первый non-loopback IP) либо $HOSTNAME
#   output-dir  — /etc/nginx/roboport-tls/
#
# Создаёт:
#   <output-dir>/cert.pem  — публичный сертификат
#   <output-dir>/key.pem   — приватный ключ (chmod 600)
#
# SAN автоматически собирается из:
#   - localhost + 127.0.0.1 + ::1 (всегда)
#   - argv[1] если задан (primary host) — DNS или IP, по виду
#   - $(hostname)            — короткое имя машины
#   - $(hostname).local      — mDNS-имя (HA-style — homeassistant.local)
#   - все non-loopback IPv4 от `hostname -I`
#   - значения из EXTRA_DNS_SANS / EXTRA_IP_SANS env (comma-separated)
#
# Срок 10 лет (3650 дней). Для self-signed это OK — не настоящая
# аутентификация, только шифрование канала.
#
# Идемпотентен: повторный запуск перезаписывает cert (юзер должен подтвердить
# через --force, иначе пропускаем если cert уже валиден > 30 дней).

set -e

# ---------- helpers ----------
say()  { printf '\033[1;36m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; }

# ---------- args ----------
HOST=""
OUT_DIR="/etc/nginx/roboport-tls"
DAYS="${SELFSIGNED_DAYS:-3650}"
FORCE="${SELFSIGNED_FORCE:-0}"
# Парсим args: [host] [out-dir] [--force]. --force эквивалентно SELFSIGNED_FORCE=1
# (явный флаг чище env-var когда вызываем из sudoers — sudo по умолчанию env
# не пропускает, args пропускает).
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1 ;;
        --) shift; break ;;
        --*) err "неизвестный флаг: $1"; exit 1 ;;
        *)
            if [ -z "$HOST" ]; then HOST="$1"
            elif [ "$OUT_DIR" = "/etc/nginx/roboport-tls" ]; then OUT_DIR="$1"
            fi
            ;;
    esac
    shift
done

# autodetect host если не задан
if [ -z "$HOST" ]; then
    # Сначала пробуем real-IP через udp-trick
    HOST=$(ip route get 8.8.8.8 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)
    [ -z "$HOST" ] && HOST=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -z "$HOST" ] && HOST=$(hostname)
fi
say "Host для cert: $HOST"
say "Output dir:    $OUT_DIR"
say "Срок:          $DAYS дней"

# Проверка openssl
if ! command -v openssl >/dev/null; then
    err "openssl не найден. apt install openssl"
    exit 1
fi

# Sudo если нужен
if [ "$EUID" -ne 0 ]; then SUDO="sudo"; else SUDO=""; fi

# ---------- check existing ----------
CERT="$OUT_DIR/cert.pem"
KEY="$OUT_DIR/key.pem"
if [ -f "$CERT" ] && [ "$FORCE" != "1" ]; then
    # Сколько ещё валиден?
    if $SUDO openssl x509 -checkend $((30*24*3600)) -noout -in "$CERT" 2>/dev/null; then
        warn "Cert $CERT уже валиден > 30 дней. Пропускаю (передай SELFSIGNED_FORCE=1 чтобы перегенерировать)."
        exit 0
    fi
fi

# ---------- prepare dir ----------
$SUDO mkdir -p "$OUT_DIR"
$SUDO chmod 755 "$OUT_DIR"

# ---------- generate ----------
# Делаем во временном каталоге, потом перекладываем атомарно (чтобы nginx не
# успел подхватить полу-сгенерированный файл).
TMP=$(mktemp -d)
trap "rm -rf $TMP" EXIT

# Конфиг с SAN — современные браузеры (Chrome 58+) ругаются «no SAN».
# Собираем максимальный список валидных имён доступа: чтобы юзеру не пришлось
# жалеть что cert только на IP когда захочет открыть через roboport.local.
CN="$HOST"

# 1) Базовые имена которые всегда валидны
DNS_SANS=("localhost")
IP_SANS=("127.0.0.1" "::1")

# 2) Primary HOST — DNS или IP по виду
is_ipv4() { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; }
add_dns() { local v="$1"; for x in "${DNS_SANS[@]}"; do [ "$x" = "$v" ] && return; done; DNS_SANS+=("$v"); }
add_ip()  { local v="$1"; for x in "${IP_SANS[@]}";  do [ "$x" = "$v" ] && return; done; IP_SANS+=("$v"); }

if is_ipv4 "$HOST"; then add_ip "$HOST"; else add_dns "$HOST"; fi

# 3) Auto-detect: hostname и hostname.local (для mDNS / avahi)
HOSTNAME_SHORT=$(hostname 2>/dev/null || true)
if [ -n "$HOSTNAME_SHORT" ]; then
    add_dns "$HOSTNAME_SHORT"
    add_dns "$HOSTNAME_SHORT.local"
fi

# 4) Все non-loopback IPv4 из `hostname -I`
for ip in $(hostname -I 2>/dev/null); do
    if is_ipv4 "$ip"; then add_ip "$ip"; fi
done

# 5) EXTRA_DNS_SANS / EXTRA_IP_SANS env (comma-separated)
if [ -n "${EXTRA_DNS_SANS:-}" ]; then
    IFS=',' read -ra arr <<< "$EXTRA_DNS_SANS"
    for v in "${arr[@]}"; do v=$(echo "$v" | xargs); [ -n "$v" ] && add_dns "$v"; done
fi
if [ -n "${EXTRA_IP_SANS:-}" ]; then
    IFS=',' read -ra arr <<< "$EXTRA_IP_SANS"
    for v in "${arr[@]}"; do v=$(echo "$v" | xargs); [ -n "$v" ] && add_ip "$v"; done
fi

# Сборка SAN-блока для openssl.cnf
say "DNS SANs: ${DNS_SANS[*]}"
say "IP  SANs: ${IP_SANS[*]}"
SAN_LINES=""
i=1; for v in "${DNS_SANS[@]}"; do SAN_LINES+="DNS.$i=$v"$'\n'; i=$((i+1)); done
i=1; for v in "${IP_SANS[@]}";  do SAN_LINES+="IP.$i=$v"$'\n';  i=$((i+1)); done

cat > "$TMP/openssl.cnf" <<EOF
[req]
distinguished_name = req_distinguished_name
req_extensions     = v3_req
prompt             = no

[req_distinguished_name]
CN = $CN
O  = Roboport (self-signed)

[v3_req]
keyUsage         = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName   = @alt_names

[alt_names]
$SAN_LINES
EOF

say "Генерирую RSA 2048 + self-signed cert"
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$TMP/key.pem" \
    -out    "$TMP/cert.pem" \
    -days   "$DAYS" \
    -config "$TMP/openssl.cnf" \
    -extensions v3_req \
    2>/dev/null

# ---------- install ----------
$SUDO mv "$TMP/cert.pem" "$CERT"
$SUDO mv "$TMP/key.pem" "$KEY"
$SUDO chmod 644 "$CERT"
$SUDO chmod 600 "$KEY"

# ---------- info ----------
ok "Cert установлен"
say "Subject:"
$SUDO openssl x509 -in "$CERT" -noout -subject
say "Subject Alternative Names:"
$SUDO openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null \
    | sed 's/^/    /' || true
say "Срок действия:"
$SUDO openssl x509 -in "$CERT" -noout -dates | sed 's/^/    /'
say "SHA-256 fingerprint (для сравнения с тем что покажет браузер):"
$SUDO openssl x509 -in "$CERT" -noout -fingerprint -sha256 | sed 's/^/    /'

echo
ok "Готово. Cert: $CERT"
ok "         Key: $KEY"
echo "  Дальше: nginx-конфиг (см. deploy/roboport.nginx.conf) должен"
echo "          указать ssl_certificate $CERT;"
echo "                  ssl_certificate_key $KEY;"
