"""DockerRuntime — поверх docker CLI через subprocess.

Зачем CLI а не SDK: нет лишних зависимостей, легко дебажить (видно команды),
кроссплатформенно из коробки. Если упрёмся в производительность или нужны
будут потоковые логи без буферизации, перейдём на docker SDK.

Используем образ factoriotools/factorio:<version>. Папку servers/<uuid>
монтируем целиком в /factorio (там образ ждёт config/, mods/, saves/).
Контейнер не пересоздаётся между start/stop — состояние сохраняется (логи и т.п.).
Если конфиг поменялся (порт, версия) — recreate() сносит контейнер и поднимает
заново; роут /config/update определяет это автоматически и сообщает пользователю.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from typing import Iterator

from ..i18n import tr
from . import rcon
from .base import ServerRuntime, ServerSpec, ServerState, ServerStatus

IMAGE = "factoriotools/factorio"


def rcon_port_for(spec: ServerSpec) -> int:
    """Используем game_port + 1 как RCON-порт. Биндим только на 127.0.0.1,
    извне он недоступен — нужен только панели."""
    return spec.port + 1


def rcon_password_for(spec: ServerSpec) -> str:
    """factoriotools пишет сгенерированный пароль в config/rconpw при первом старте."""
    pw_file = spec.data_dir / "config" / "rconpw"
    if not pw_file.exists():
        raise rcon.RconError(tr("docker.rcon_password_missing"))
    return pw_file.read_text(encoding="utf-8").strip()


class DockerError(RuntimeError):
    pass


def _run(args: list[str], check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    """Тонкий wrapper над docker CLI. На Windows subprocess.list2cmdline сам
    квотит args со спец-символами (включая пробелы в путях), это нам и нужно."""
    try:
        return subprocess.run(
            args, check=check, timeout=timeout,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as e:
        raise DockerError(tr("updates.docker_cli_not_found_path")) from e
    except subprocess.CalledProcessError as e:
        raise DockerError(tr(
            "docker.command_failed", command=" ".join(args[1:3]), code=e.returncode,
            detail=e.stderr.strip(),
        )) from e


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        _run(["docker", "version", "--format", "{{.Server.Version}}"], check=True, timeout=5)
        return True
    except Exception:
        return False


def _inspect(name: str) -> dict | None:
    """Вернуть docker inspect или None, если контейнера нет.

    Поднимаем DockerError, если проблема не в "нет контейнера", а в самом docker
    (демон выключен, нет прав). Пользователь должен видеть разницу."""
    p = _run(["docker", "inspect", name], check=False, timeout=10)
    if p.returncode == 0:
        data = json.loads(p.stdout)
        return data[0] if data else None
    err = (p.stderr or "").lower()
    # Известные паттерны "контейнера нет":
    if "no such object" in err or "no such container" in err:
        return None
    # Всё остальное — настоящая проблема (демон выключен, нет прав, и т.п.)
    detail = p.stderr.strip() or tr("docker.unknown_error")
    raise DockerError(tr("docker.inspect_failed", detail=detail))


def _state_from_inspect(insp: dict | None) -> ServerStatus:
    if insp is None:
        return ServerStatus(state=ServerState.STOPPED)
    state = insp.get("State", {})
    status = state.get("Status", "")
    if status == "running":
        s = ServerState.RUNNING
    elif status in ("created", "restarting"):
        s = ServerState.STARTING
    elif status in ("exited", "dead"):
        s = ServerState.STOPPED
    else:
        s = ServerState.UNKNOWN
    return ServerStatus(
        state=s,
        container_id=insp.get("Id"),
        started_at=state.get("StartedAt"),
        exit_code=state.get("ExitCode") if status == "exited" else None,
        detail=state.get("Error") or None,
    )


class DockerRuntime(ServerRuntime):
    def status(self, spec: ServerSpec) -> ServerStatus:
        return _state_from_inspect(_inspect(spec.container_name))

    def start(self, spec: ServerSpec) -> ServerStatus:
        cur = self.status(spec)
        if cur.state == ServerState.RUNNING:
            return cur

        insp = _inspect(spec.container_name)
        if insp is None:
            self._create_and_start(spec)
        else:
            _run(["docker", "start", spec.container_name])
        return self.status(spec)

    def _create_and_start(self, spec: ServerSpec) -> None:
        # Абсолютный путь обязательно — docker отказывается биндить относительные.
        host_path = str(spec.data_dir.resolve())
        args = [
            "docker", "run", "-d",
            "--name", spec.container_name,
            "--restart", "unless-stopped",
            "-v", f"{host_path}:/factorio",
            "-e", "SAVE_NAME=default",
            "-e", "GENERATE_NEW_SAVE=true",
        ]

        if spec.network_mode == "host":
            # LAN-видимость: контейнер сидит прямо на сетевом стеке хоста, без NAT.
            # Factorio LAN-broadcast 255.255.255.255:34197 теперь доходит, клиенты
            # автоматически находят сервер. Цена: RCON-порт оказывается на 0.0.0.0
            # (защита — пароль из rconpw, 16 случайных символов; для дополнительной
            # безопасности юзер может настроить ufw на хосте).
            #
            # Game-port управляем через env PORT — при многосерверной раскладке
            # каждый занимает свой port на хосте напрямую. RCON_PORT — port+1.
            args += [
                "--network", "host",
                "-e", f"PORT={spec.port}",
                "-e", f"RCON_PORT={rcon_port_for(spec)}",
            ]
        else:
            # bridge (default): NAT через docker0. Внутри контейнера Factorio
            # слушает 34197/udp + 27015/tcp; снаружи мапим на свободные хост-порты.
            # RCON — только на loopback, иначе bruteforce-зона.
            args += [
                "-p", f"{spec.port}:34197/udp",
                "-p", f"127.0.0.1:{rcon_port_for(spec)}:27015/tcp",
            ]

        # PUID/PGID — чтобы контейнер писал файлы (saves/, mods/, config/*.json,
        # rconpw, logs/) под uid'ом панели. Иначе они owned by 845 (дефолт
        # factoriotools), и панель не может ни нормализовать adminlist после
        # /promote, ни удалить папку при ❌ Удалить. На Windows os.getuid()
        # отсутствует — там бинд-маунты Docker Desktop через WSL2 и так
        # пишут под пользователем хоста, флаги не нужны.
        import os as _os
        if hasattr(_os, "getuid"):
            args += [
                "-e", f"PUID={_os.getuid()}",
                "-e", f"PGID={_os.getgid()}",
            ]
        args.append(f"{IMAGE}:{spec.version}")
        _run(args, timeout=180)  # на pull образа может уйти время

    def stop(self, spec: ServerSpec, timeout_sec: int = 30) -> ServerStatus:
        cur = self.status(spec)
        if cur.state == ServerState.STOPPED:
            return cur
        _run(["docker", "stop", "-t", str(timeout_sec), spec.container_name],
             timeout=timeout_sec + 10)
        return self.status(spec)

    def restart(self, spec: ServerSpec) -> ServerStatus:
        # Не используем `docker restart`: явный stop+start даёт чистую обработку
        # того, что контейнер мог быть удалён (тогда start его создаст).
        self.stop(spec)
        return self.start(spec)

    def recreate(self, spec: ServerSpec) -> ServerStatus:
        """Снести контейнер и поднять заново — нужно когда поменялись флаги
        (порты, env, version), которые в running-контейнере не поменять."""
        self.remove(spec)
        return self.start(spec)

    def remove(self, spec: ServerSpec) -> None:
        if _inspect(spec.container_name) is None:
            return
        _run(["docker", "rm", "-f", spec.container_name], check=False)

    def logs(self, spec: ServerSpec, tail: int | str = 200, timestamps: bool = False) -> str:
        """Снимок логов. tail может быть числом или 'all'."""
        if _inspect(spec.container_name) is None:
            return ""
        cmd = ["docker", "logs", "--tail", str(tail)]
        if timestamps:
            cmd.append("--timestamps")
        cmd.append(spec.container_name)
        p = _run(cmd, check=False, timeout=30)
        # У factoriotools вывод идёт в stdout; stderr иногда тоже.
        return (p.stdout or "") + (p.stderr or "")

    def stream_logs(self, spec: ServerSpec,
                    stop_event: threading.Event | None = None) -> Iterator[str]:
        """Follow logs until Docker ends them or the owning WS is closed.

        A small watcher terminates a blocked Windows pipe read when the owning
        WebSocket closes; stdout itself remains streaming and is not buffered in
        a second unbounded queue.
        """
        # Используется в WS-эндпоинте /ws/servers/{uuid}/logs.
        proc = subprocess.Popen(
            ["docker", "logs", "-f", "--tail", "100", spec.container_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        assert proc.stdout is not None
        reader_done = threading.Event()

        def stop_watcher() -> None:
            while not reader_done.is_set():
                if stop_event is not None and stop_event.wait(0.25):
                    if proc.poll() is None:
                        proc.terminate()
                    return

        watcher = None
        if stop_event is not None:
            watcher = threading.Thread(target=stop_watcher, daemon=True)
            watcher.start()
        try:
            for line in proc.stdout:
                if stop_event is not None and stop_event.is_set():
                    return
                yield line.rstrip("\n")
        finally:
            reader_done.set()
            if proc.poll() is None:
                proc.terminate()
            proc.stdout.close()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            if watcher is not None:
                watcher.join(timeout=1)

    def send_command(self, spec: ServerSpec, cmd: str) -> str:
        """Отправить команду в Factorio через RCON, вернуть текст ответа.

        Источник пароля — config/rconpw, который factoriotools пишет при первом старте.
        Если контейнер ещё не пробросил RCON-порт (старая версия флагов) — коннект
        к 127.0.0.1:rcon_port упадёт; пользователь увидит подсказку "Пересоздай контейнер".
        """
        password = rcon_password_for(spec)  # может бросить RconError если нет файла
        return rcon.execute("127.0.0.1", rcon_port_for(spec), password, cmd, timeout=5.0)

    def rcon_endpoint(self, spec: ServerSpec) -> tuple[str, int]:
        """Где слушает RCON для этого сервера (для UI / отладки)."""
        return "127.0.0.1", rcon_port_for(spec)
