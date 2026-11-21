"""Runtime — абстракция над тем, КАК запускается процесс Factorio-сервера.

Сейчас одна реализация — DockerRuntime. Абстракция оставлена на случай,
если когда-нибудь добавится что-то ещё (нативный бинарь, podman, ssh-в-чужой-хост).
"""
from ..i18n import tr
from .base import ServerRuntime, ServerSpec, ServerStatus, ServerState
from .docker import DockerRuntime

__all__ = ["ServerRuntime", "ServerSpec", "ServerStatus", "ServerState",
           "DockerRuntime", "get_runtime"]


def get_runtime(name: str) -> ServerRuntime:
    """Фабрика по имени runtime из БД. Сейчас только 'docker'."""
    if name == "docker":
        return DockerRuntime()
    raise ValueError(tr("runtime.unknown", name=repr(name)))
