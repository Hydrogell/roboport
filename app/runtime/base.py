"""Контракт runtime'а. Реализации должны соответствовать ServerRuntime-протоколу."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable


class ServerState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    UNKNOWN = "unknown"
    ERROR = "error"


@dataclass(frozen=True)
class ServerSpec:
    """Минимум, который нужен runtime'у чтобы поднять/найти контейнер сервера."""
    uuid: str
    name: str
    version: str            # "2.0.76", "stable", ...
    port: int               # UDP-порт игры на хосте
    data_dir: Path          # абсолютный путь к servers/<uuid>/
    network_mode: str = "bridge"  # 'bridge' (NAT, default) | 'host' (LAN-видим)

    @property
    def container_name(self) -> str:
        # Docker допускает [a-zA-Z0-9_.-]+, наш uuid вписывается.
        return f"roboport-{self.uuid}"


@dataclass(frozen=True)
class ServerStatus:
    state: ServerState
    container_id: str | None = None
    started_at: str | None = None       # ISO 8601, как Docker отдаёт
    exit_code: int | None = None
    detail: str | None = None           # человекочитаемое описание (для ошибок)


@runtime_checkable
class ServerRuntime(Protocol):
    """Что должна уметь любая реализация runtime'а.

    Идемпотентность: start на уже-запущенном сервере — no-op (а не ошибка).
    Аналогично stop на остановленном.
    """

    def start(self, spec: ServerSpec) -> ServerStatus: ...
    def stop(self, spec: ServerSpec, timeout_sec: int = 30) -> ServerStatus: ...
    def restart(self, spec: ServerSpec) -> ServerStatus: ...
    def recreate(self, spec: ServerSpec) -> ServerStatus: ...
    def status(self, spec: ServerSpec) -> ServerStatus: ...
    def remove(self, spec: ServerSpec) -> None: ...
    def logs(self, spec: ServerSpec, tail: int = 200) -> str: ...
    def stream_logs(self, spec: ServerSpec) -> Iterator[str]: ...
    def send_command(self, spec: ServerSpec, cmd: str) -> str: ...
