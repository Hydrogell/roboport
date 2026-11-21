"""Network info — данные для вкладки «🌐 Доступ» в /settings.

Собирает: hostname, .local-имя (если avahi работает), список IPv4 хоста,
информация о TLS-сертификате (если cert path известен).

Все вызовы read-only, без side-effects. Если что-то недоступно (нет avahi,
нет cert, нет /proc/net/...) — возвращаем None / пустые списки и UI рендерит
gracefully.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Путь к TLS-cert который установлен install.sh'ем.
# Если nginx настроен по-другому — UI покажет «cert не найден».
DEFAULT_CERT_PATHS = (
    "/etc/nginx/roboport-tls/cert.pem",
    "/etc/letsencrypt/live/*/fullchain.pem",  # не glob'аем сейчас, но как референс
)


@dataclass
class CertInfo:
    """Распарсенный self-signed/Let's Encrypt cert."""
    subject: str
    issuer: str
    not_before: int           # unix-ts
    not_after: int            # unix-ts
    fingerprint_sha256: str   # uppercase hex с двоеточиями
    san: list[str]            # список Subject Alternative Names
    is_self_signed: bool      # subject == issuer
    days_remaining: int       # сколько дней до истечения (отриц. = просрочен)


@dataclass
class NetworkInfo:
    """Сводка для UI."""
    hostname: str                   # короткое имя из uname/hostname
    fqdn: str                       # полное (если задано в /etc/hosts)
    has_avahi: bool                 # avahi-daemon active
    mdns_name: str | None           # <hostname>.local если avahi работает
    ipv4_addresses: list[str]       # «реальные» non-loopback IPv4 (без docker)
    ipv4_docker_bridges: list[str]  # IP docker-bridge'ей (172.17–31.x), отдельно
    cert_path: str | None           # абсолютный путь к cert
    cert: CertInfo | None           # распарсенная инфа (None если cert не найден)
    access_urls: list[str] = field(default_factory=list)
                                    # все URL по которым панель доступна с TLS


def _hostname() -> str:
    """Короткое hostname через stdlib (без обращения к internet/DNS)."""
    return socket.gethostname()


def _fqdn() -> str:
    """Полное имя (gethostbyname_ex). Fallback на hostname если не настроено."""
    try:
        return socket.getfqdn() or _hostname()
    except Exception:
        return _hostname()


def _is_docker_bridge_ip(ip: str) -> bool:
    """IP докера? 172.16.0.0/12 (RFC1918 block, который docker по умолчанию
    режет под bridge'и: docker0=172.17, custom networks=172.18–31).
    Не идеально (172.16-31.x могут быть и легитимной LAN), но в 99% юзкейсов
    panel'и это именно docker. На VM/LXC у пользователя LAN обычно 192.168.x
    или 10.x — поэтому false-positive редок."""
    if not ip.startswith("172."):
        return False
    try:
        second = int(ip.split(".", 2)[1])
    except (ValueError, IndexError):
        return False
    return 16 <= second <= 31


def _list_ipv4() -> tuple[list[str], list[str]]:
    """Все non-loopback IPv4-адреса хоста, разделённые на (real, docker_bridge).
    Без netifaces (нет в stdlib).
    Trick: создаём UDP-сокеты и читаем bound addr — даёт routing-IP. Также
    парсим `hostname -I` если доступно (Linux)."""
    ips: list[str] = []
    # 1) routing-IP (главный исходящий)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    # 2) hostname -I (Linux) — даст все IPv4 интерфейсов
    try:
        out = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=2,
            errors="replace",  # cp1251 stdout на Windows-dev не падает
        )
        for token in (out.stdout or "").split():
            if "." in token and token not in ips:
                ips.append(token)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Уберём loopback и разделим docker-bridge от обычных
    real: list[str] = []
    docker: list[str] = []
    for ip in ips:
        if ip.startswith("127."):
            continue
        if _is_docker_bridge_ip(ip):
            docker.append(ip)
        else:
            real.append(ip)
    return real, docker


def _has_avahi() -> bool:
    """avahi-daemon active. Проверка через systemctl is-active."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", "avahi-daemon"],
            timeout=2,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _read_cert(path: Path) -> CertInfo | None:
    """Распарсить cert через `openssl x509 -noout -...`. Без зависимости на
    cryptography/PyOpenSSL — поддержка stdlib-стиля проекта."""
    if not path.is_file():
        return None
    try:
        # Все нужные поля одной командой
        cmd = [
            "openssl", "x509", "-in", str(path), "-noout",
            "-subject", "-issuer", "-startdate", "-enddate",
            "-fingerprint", "-sha256", "-ext", "subjectAltName",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                           errors="replace")
        if r.returncode != 0:
            return None
        out = r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    def _line(prefix: str) -> str:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith(prefix):
                return line[len(prefix):].lstrip("=").strip()
        return ""

    subject = _line("subject")
    issuer = _line("issuer")
    not_before_s = _line("notBefore")
    not_after_s = _line("notAfter")
    fp_line = _line("sha256 Fingerprint")
    # SAN не помечен prefix'ом «X509v3 Subject...:»; ищем секцию.
    san_list: list[str] = []
    in_san = False
    for line in out.splitlines():
        if "Subject Alternative Name" in line:
            in_san = True
            continue
        if in_san:
            ls = line.strip()
            if not ls:
                continue
            # Format: "DNS:localhost, IP Address:127.0.0.1, IP Address:..."
            for tok in ls.split(","):
                tok = tok.strip()
                if tok:
                    san_list.append(tok)
            break  # SAN на одной строке, дальше уже не наше

    def _ts(s: str) -> int:
        # OpenSSL формат: «May  4 18:29:42 2036 GMT»
        for fmt in ("%b %d %H:%M:%S %Y %Z", "%b  %d %H:%M:%S %Y %Z"):
            try:
                return int(time.mktime(time.strptime(s, fmt))) - time.timezone
            except ValueError:
                continue
        return 0

    not_before = _ts(not_before_s)
    not_after = _ts(not_after_s)
    now = int(time.time())
    days_remaining = (not_after - now) // 86400 if not_after else 0
    return CertInfo(
        subject=subject, issuer=issuer,
        not_before=not_before, not_after=not_after,
        fingerprint_sha256=fp_line, san=san_list,
        is_self_signed=(subject == issuer and subject != ""),
        days_remaining=days_remaining,
    )


def _build_access_urls(host_names: list[str], has_tls: bool) -> list[str]:
    """Сборка списка URL (https если cert есть, иначе http). Дедуп."""
    seen: set[str] = set()
    out: list[str] = []
    proto = "https" if has_tls else "http"
    for h in host_names:
        if not h:
            continue
        # IPv6 надо в скобках, но IPv4 и DNS — как есть
        url = f"{proto}://{h}/"
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def collect() -> NetworkInfo:
    """Собрать всю сетевую инфу. Кешировать не нужно — всё дешёвое.
    Если что-то не работает — graceful degradation, не падаем."""
    cert_path: str | None = None
    cert: CertInfo | None = None
    for p in DEFAULT_CERT_PATHS:
        if "*" in p:
            continue  # glob позже если понадобится
        if Path(p).is_file():
            cert_path = p
            cert = _read_cert(Path(p))
            break

    hostname = _hostname()
    fqdn = _fqdn()
    has_avahi = _has_avahi()
    ipv4s, docker_ips = _list_ipv4()
    mdns = f"{hostname}.local" if has_avahi and hostname else None

    # access URLs только из «реальных» IP — docker bridge туда не нужен,
    # юзеру нечего ходить на 172.17.0.1 из браузера.
    candidates: list[str] = []
    if mdns:
        candidates.append(mdns)
    if fqdn and fqdn not in (hostname, "localhost"):
        candidates.append(fqdn)
    candidates.extend(ipv4s)
    access_urls = _build_access_urls(candidates, has_tls=cert is not None)

    return NetworkInfo(
        hostname=hostname, fqdn=fqdn,
        has_avahi=has_avahi, mdns_name=mdns,
        ipv4_addresses=ipv4s,
        ipv4_docker_bridges=docker_ips,
        cert_path=cert_path, cert=cert,
        access_urls=access_urls,
    )
