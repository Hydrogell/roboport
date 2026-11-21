"""Минимальный клиент Source RCON (Valve protocol).

Формат пакета (little-endian):
  4 bytes: size  (длина оставшегося, не считая этих 4 байт)
  4 bytes: id
  4 bytes: type
  <body bytes> + 0x00
  0x00

Типы:
  3 — SERVERDATA_AUTH (запрос аутентификации)
  2 — SERVERDATA_AUTH_RESPONSE (ответ; id == -1 = неверный пароль)
  2 — SERVERDATA_EXECCOMMAND
  0 — SERVERDATA_RESPONSE_VALUE

Документация: https://developer.valvesoftware.com/wiki/Source_RCON_Protocol
"""
from __future__ import annotations

import socket
import struct
from ..i18n import tr

AUTH = 3
EXEC = 2
RESPONSE = 0


class RconError(RuntimeError):
    pass


class RconAuthError(RconError):
    pass


def _pack(req_id: int, ptype: int, body: str) -> bytes:
    payload = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RconError(tr("rcon.connection_closed"))
        buf += chunk
    return buf


def _read_packet(sock: socket.socket) -> tuple[int, int, str]:
    (size,) = struct.unpack("<i", _read_exact(sock, 4))
    if size < 10 or size > 4096:
        raise RconError(tr("rcon.packet_size_invalid", size=size))
    payload = _read_exact(sock, size)
    req_id, ptype = struct.unpack("<ii", payload[:8])
    body = payload[8:-2].decode("utf-8", errors="replace")
    return req_id, ptype, body


def execute(host: str, port: int, password: str, command: str,
            timeout: float = 5.0) -> str:
    """Подключиться, аутентифицироваться, выполнить command, вернуть ответ строкой.

    Бросает RconAuthError если пароль неверный, RconError на любой другой сбой.
    Соединение закрывается перед возвратом — короткоживущие коннекты для одной команды.
    """
    if not password:
        raise RconError(tr("rcon.empty_password"))

    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)

        # AUTH
        s.sendall(_pack(1, AUTH, password))
        # Factorio (как и многие сервера) шлёт сначала пустой RESPONSE_VALUE,
        # потом AUTH_RESPONSE. Читаем до получения AUTH_RESPONSE.
        while True:
            req_id, ptype, _ = _read_packet(s)
            if ptype == 2:  # AUTH_RESPONSE
                if req_id == -1:
                    raise RconAuthError(tr("rcon.invalid_password"))
                break

        # EXEC + marker-trick. Особенность Factorio:
        #   - на пустой EXEC (body='') Factorio НЕ отвечает (классический Valve-трюк
        #     с type=0 RESPONSE_VALUE тоже игнорирует);
        #   - но на EXEC с body=' ' (один пробел) — отвечает пакетом с пустым body
        #     и нашим маркер-id.
        # Это даёт нам точный признак "ответ закончился", без timing-эвристик и без
        # риска порезать длинные multi-packet ответы.
        EXEC_ID = 100
        MARKER_ID = 101
        s.sendall(_pack(EXEC_ID, EXEC, command))
        s.sendall(_pack(MARKER_ID, EXEC, " "))

        body: list[str] = []
        while True:
            req_id, ptype, chunk = _read_packet(s)
            if req_id == MARKER_ID:
                # маркер пришёл — все ответы на основной EXEC уже собраны
                break
            if req_id == EXEC_ID and ptype == RESPONSE:
                body.append(chunk)
            # любые иные пакеты (асинхронный шум) игнорим
        return "".join(body)
