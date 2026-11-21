"""Linux watchdog integration tests with a fake systemd/curl boundary."""
from __future__ import annotations

import os
import sqlite3
import socket
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="requires a Linux bash runtime")
def test_legacy_caller_dispatches_independent_worker_and_uses_service_port(tmp_path: Path) -> None:
    """A v0.1.9 caller's obsolete :8000 URL must not control v1's health check."""
    repo = tmp_path / "roboport"
    deploy = repo / "deploy"
    deploy.mkdir(parents=True)
    source_script = Path(__file__).parents[1] / "deploy" / "panel-update-watchdog.sh"
    script = deploy / "panel-update-watchdog.sh"
    script.write_bytes(source_script.read_bytes())
    script.chmod(0o755)
    (repo / "VERSION").write_text("v1.0.0\n", encoding="utf-8")

    backup = repo / ".backups" / "pre-update-7"
    backup.mkdir(parents=True)
    db_path = repo / "app" / "panel.sqlite"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE panel_updates (id INTEGER PRIMARY KEY, status TEXT, finished_at INTEGER, error TEXT)")
        conn.execute("INSERT INTO panel_updates (id, status) VALUES (7, 'running')")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dispatch = tmp_path / "dispatch.txt"
    urls = tmp_path / "urls.txt"
    restart = tmp_path / "restart.txt"

    def command(name: str, body: str) -> None:
        path = bin_dir / name
        path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
        path.chmod(0o755)

    command("id", "echo 1000\n")
    command("sleep", "exit 0\n")
    command("sudo", "[ \"$1\" = -n ] && shift\nexec \"$@\"\n")
    command(
        "systemctl",
        "if [ \"$1\" = show ]; then\n"
        "  printf '%s\\n' 'ExecStart={ path=/srv/roboport/.venv/bin/python ; argv[]=/srv/roboport/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 }'\n"
        "elif [ \"$1\" = restart ]; then\n"
        f"  : > '{restart}'\n"
        "else exit 9\nfi\n",
    )
    command(
        "curl",
        "last=\nfor arg in \"$@\"; do last=$arg; done\n"
        f"printf '%s\\n' \"$last\" >> '{urls}'\n"
        "case \"$last\" in\n"
        "  */api/v1/health) printf '%s' '{\"ok\":true,\"version\":\"v1\"}' ;;\n"
        "  */api/v1/panel/version) printf '%s' '{\"version\":\"v1.0.0\"}' ;;\n"
        "  *) exit 22 ;;\nesac\n",
    )
    command(
        "systemd-run",
        f"printf '%s\\n' \"$@\" > '{dispatch}'\n"
        "while [ \"$1\" != bash ]; do shift; done\nshift\n"
        "export ROBOPORT_WATCHDOG_WORKER=1\n"
        "exec /bin/bash \"$@\"\n",
    )

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bus = socket.socket(socket.AF_UNIX)
    bus.bind(str(runtime_dir / "bus"))
    try:
        env = os.environ | {"PATH": f"{bin_dir}:/usr/bin:/bin", "XDG_RUNTIME_DIR": str(runtime_dir)}
        run = subprocess.run(
            ["/bin/bash", str(script), "7", str(backup), "http://127.0.0.1:8000/api/v1/health"],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )
    finally:
        bus.close()

    assert run.returncode == 0, run.stderr
    assert "--user" in dispatch.read_text(encoding="utf-8")
    assert restart.exists(), "restart must run even when the unit began inactive"
    assert urls.read_text(encoding="utf-8").splitlines() == [
        "http://127.0.0.1:8001/api/v1/health",
        "http://127.0.0.1:8001/api/v1/panel/version",
    ]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM panel_updates WHERE id = 7").fetchone()[0] == "success"
