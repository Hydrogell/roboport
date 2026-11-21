#!/usr/bin/env bash
# Self-update watchdog. The entrypoint hands work to a transient systemd
# service; the worker therefore survives restarting roboport.service.
# Args: update_id backup_dir [legacy_health_url_ignored]

set -uo pipefail
UPDATE_ID="${1:?update_id required}"
BACKUP_DIR="${2:?backup_dir required}"
REPO_ROOT="${ROBOPORT_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON="$REPO_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 || command -v python)"
SERVICE_NAME="roboport"
case "$UPDATE_ID" in ''|*[!0-9]*) echo "[watchdog] invalid update id" >&2; exit 2 ;; esac

log() { echo "[watchdog $(date +%H:%M:%S)] $*"; }
set_status() {
    local status="$1" err="${2:-}"
    UPDATE_STATUS="$status" UPDATE_ERROR="$err" UPDATE_ID="$UPDATE_ID" "$PYTHON" - "$REPO_ROOT/app/panel.sqlite" <<'PYEOF'
import os, sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1])
conn.execute("UPDATE panel_updates SET finished_at = ?, status = ?, error = ? WHERE id = ?", (int(time.time()), os.environ["UPDATE_STATUS"], os.environ["UPDATE_ERROR"] or None, int(os.environ["UPDATE_ID"])))
conn.commit()
conn.close()
PYEOF
}

dispatch_worker() {
    local unit="roboport-update-watchdog-${UPDATE_ID}"
    if [ "$(id -u)" -eq 0 ]; then
        systemd-run --system --unit="$unit" --collect --service-type=exec --setenv=ROBOPORT_WATCHDOG_WORKER=1 --setenv=ROBOPORT_PROJECT_ROOT="$REPO_ROOT" bash "$0" "$UPDATE_ID" "$BACKUP_DIR"
        return
    fi
    local uid runtime_dir
    uid="$(id -u)"; runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$uid}"
    if [ ! -S "$runtime_dir/bus" ]; then log "user systemd bus is unavailable at $runtime_dir/bus"; return 1; fi
    XDG_RUNTIME_DIR="$runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" systemd-run --user --unit="$unit" --collect --service-type=exec --setenv=ROBOPORT_WATCHDOG_WORKER=1 --setenv=ROBOPORT_PROJECT_ROOT="$REPO_ROOT" bash "$0" "$UPDATE_ID" "$BACKUP_DIR"
}

# v0.1.9 reaches this new file after its overlay, although its Python caller
# still uses nohup/setsid and sends an obsolete nginx-facing health URL.
if [ "${ROBOPORT_WATCHDOG_WORKER:-}" != "1" ]; then
    if ! dispatch_worker; then set_status "failed" "could not start independent systemd watchdog"; exit 1; fi
    exit 0
fi

service_port() {
    local exec_start port
    exec_start="$(systemctl show -p ExecStart --value "$SERVICE_NAME" 2>/dev/null)" || return 1
    if [[ "$exec_start" =~ --port=([0-9]{1,5}) ]]; then port="${BASH_REMATCH[1]}"
    elif [[ "$exec_start" =~ --port[[:space:]]+([0-9]{1,5}) ]]; then port="${BASH_REMATCH[1]}"
    else return 1; fi
    ((10#$port >= 1 && 10#$port <= 65535)) || return 1
    printf '%s\n' "$port"
}
restart_panel() {
    command -v systemctl >/dev/null 2>&1 || return 1
    # Do not require an active unit: rollback must recover an inactive one.
    if [ "$(id -u)" -eq 0 ]; then systemctl restart "$SERVICE_NAME"; return; fi
    sudo -n systemctl restart "$SERVICE_NAME"
}
health_ok() {
    local health version expected
    health="$(curl -fsS --max-time 5 "http://127.0.0.1:${PANEL_PORT}/api/v1/health" 2>/dev/null)" || return 1
    version="$(curl -fsS --max-time 5 "http://127.0.0.1:${PANEL_PORT}/api/v1/panel/version" 2>/dev/null)" || return 1
    expected="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")" || return 1
    "$PYTHON" - "$health" "$version" "$expected" <<'PYEOF'
import json, sys
health, panel = (json.loads(value) for value in sys.argv[1:3])
ok = health.get("ok") is True and health.get("version") == "v1"
sys.exit(0 if ok and panel.get("version") == sys.argv[3] else 1)
PYEOF
}
restore_backup() {
    ROBOPORT_REPO_ROOT="$REPO_ROOT" ROBOPORT_BACKUP_DIR="$BACKUP_DIR" "$PYTHON" - <<'PYEOF'
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["ROBOPORT_REPO_ROOT"])
from app.panel_update import install_requirements, restore_from_backup
restore_from_backup(Path(os.environ["ROBOPORT_BACKUP_DIR"]))
try: install_requirements()
except Exception as exc: print(f"pip install after rollback failed: {exc}", file=sys.stderr)
PYEOF
}

if ! PANEL_PORT="$(service_port)"; then log "could not determine roboport --port from systemd ExecStart"; set_status "failed" "could not determine local roboport service port"; exit 1; fi
log "stage 1: sleep 2 (let endpoint flush response)"; sleep 2
log "stage 2: restart $SERVICE_NAME (local health port $PANEL_PORT)"
if restart_panel; then
    log "stage 3: health and identity check (20x3s)"
    for i in $(seq 1 20); do
        sleep 3
        if health_ok; then log "health and identity OK on attempt $i"; set_status "success"; exit 0; fi
    done
fi
log "health did not recover; restoring $BACKUP_DIR"
if ! restore_backup; then set_status "failed" "new version failed health-check and backup restore failed"; exit 1; fi
log "rollback restored; restarting again"; restart_panel || log "rollback restart failed; continuing recovery check"
for i in $(seq 1 10); do sleep 3; if health_ok; then log "rolled back and healthy on attempt $i"; set_status "rolled-back" "new version failed health-check; backup restored"; exit 0; fi; done
set_status "failed" "new version failed health-check and rollback did not recover panel"
exit 1
