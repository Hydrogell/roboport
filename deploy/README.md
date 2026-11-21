# Deploy Roboport

[Project home](../README.md) · [Русская версия](README.ru.md)

Use a dedicated Linux host for a server shared with others. Windows is suitable for a local or personal server using Docker Desktop.

## Before you start

- Download **Source code** from the repository page and extract it, or copy the clone URL from **Code**.
- Keep the project directory: it contains the SQLite database, credentials, and Factorio worlds.
- Do not expose the panel to the Internet without HTTPS. Linux binds the application to loopback by default.

## Install

### Linux: Debian or Ubuntu

From the extracted source root on a dedicated host:

```bash
cd /path/to/extracted/roboport
INSTALL_SYSTEMD=yes INSTALL_NGINX=yes INSTALL_TLS=selfsigned bash deploy/install.sh
```

The installer installs Python, Docker, and nginx; creates `.venv` and `.env`; enables Docker and `roboport.service`; and creates a self-signed certificate. When mDNS is available, it also advertises `<hostname>.local`. Re-login if the installer adds your account to the `docker` group.

Open `https://<hostname>.local/` or the host IP and accept the self-signed certificate warning. In this mode Roboport listens at `127.0.0.1:8001`; nginx serves HTTPS on 443 and redirects HTTP on 80. Verify a warning before accepting it: **Panel settings → Network and address → HTTPS certificate → Certificate details** shows the SHA-256 fingerprint. From the host, the equivalent command is:

```bash
sudo openssl x509 -in /etc/nginx/roboport-tls/cert.pem -noout -fingerprint -sha256
```

For a public DNS name, use `INSTALL_TLS=certbot` and set `SERVER_NAME=panel.example.com`. After DNS points to the host and port 80 is reachable, install Certbot and its nginx plugin, then request the certificate:

```bash
sudo apt update
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d panel.example.com
```

`INSTALL_TLS=none` is unencrypted HTTP and unsuitable for Internet access.

```bash
sudo systemctl status roboport
sudo journalctl -u roboport -n 100 --no-pager
sudo systemctl restart roboport
SMOKE_BASE=http://127.0.0.1:8001 bash deploy/smoke.sh
```

### Windows 11: Docker Desktop with WSL 2

Docker Desktop is free for personal use. Organizations can check whether their use requires a paid subscription in the [Docker Desktop license terms](https://docs.docker.com/subscription-billing/desktop-license/).

Install Docker Desktop, enable its WSL 2 backend, start Docker Desktop, then open PowerShell in the extracted source root:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install.ps1
```

The script can install Python 3.13 and Docker Desktop through `winget`, creates `.venv` and `.env`, and writes `start.bat`. If it installed Docker Desktop, start it (and reboot if requested) before rerunning the installer.

Start with `start.bat`. The panel is local-only at `http://127.0.0.1:8000/`. The optional Windows startup entry is named `Roboport.bat` and starts the same panel after sign-in.

## First use

The first visit opens `/setup`. Create the administrator using the setup code printed by the installer. The code is valid for 10 minutes. For a manual install or a fresh code, run `.venv/bin/python -m app.cli setup-code` on the host (Windows `.\.venv\Scripts\python.exe -m app.cli setup-code`). In **Panel settings → General**, enter the Factorio.com username/token for mod downloads or public listing, and choose the panel time zone if needed.

Choose **Servers → Create new server**, select a free UDP port, then start the server. Allow the game server's UDP port through the firewall for remote players. RCON runs locally.

To import an existing server, place its directory directly under `servers/` and click **Import** on the dashboard. Importing and cloning require ordinary files and directories. If the source contains symbolic links or Windows junctions, replace them with copies of the linked data first.

Enable 2FA in **Panel settings → Security** and keep its one-time backup codes securely. A backup code can replace the authenticator code once.

## Operations

### Backup

Back up `.env`, `app/panel.sqlite`, and `servers/`. The last contains Factorio configuration, mods, logs, and worlds.

For a consistent live Linux backup, open each server's **Saves** tab, choose **Save now**, and wait for it to complete. Stop those servers in the UI before archiving `servers/`. The following uses SQLite's backup API, so the panel may remain running:

```bash
umask 077
cd /path/to/roboport
backup_dir="$HOME/roboport-backups/$(date +%F-%H%M%S)"
mkdir -p "$backup_dir"
.venv/bin/python -c 'import sqlite3, sys; s=sqlite3.connect("file:app/panel.sqlite?mode=ro", uri=True); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); s.close()' "$backup_dir/panel.sqlite"
cp .env "$backup_dir/.env"
tar -C servers -czf "$backup_dir/servers.tar.gz" .
```

On Windows, open each server's **Saves** tab, choose **Save now**, stop each Factorio server, stop `start.bat` with `Ctrl+C`, then copy `.env`, `app\panel.sqlite`, and `servers` to a separate drive. Start `start.bat` again afterwards.

### Restore

Restore only a trusted backup. Stop `roboport.service` on Linux (or `start.bat` on Windows) and stop Factorio servers first. Keep the current data as a safety copy. Restore worlds only into a clean `servers/` directory; do not merge an old and restored tree. Replace `app/panel.sqlite` with the consistent backup and remove any old `app/panel.sqlite-wal` and `app/panel.sqlite-shm` sidecars rather than mixing them with the restored database. Then start the service:

```bash
sudo systemctl stop roboport
# restore .env and app/panel.sqlite; restore servers/ into a clean directory
sudo systemctl start roboport
```

After restoring a world archive, select it in **Saves** and make it current while its Factorio server is stopped.

### Update and rollback

On Linux, open **Panel settings → Updates** and select **Check for updates**. Installing a release backs up the current version under `.backups/` and preserves your settings, accounts and worlds. After the restart, a separate process checks that the panel answers on its configured port and reports the expected version. If this check fails, it restores the previous code. The same page lets you install a trusted release archive or roll back an earlier update.

**Upgrading from the beta for the first time?** On the Updates page, expand **Update source**, enter `Hydrogell/roboport` in **GitHub repository** and save.

If your existing Linux service runs under a regular user account, run the following once to keep the update process available after logout. The installer does this for new installations.

```bash
panel_user=$(systemctl show -p User --value roboport)
sudo loginctl enable-linger "$panel_user"
sudo systemctl start "user@$(id -u "$panel_user").service"
```

On Windows, stop `start.bat`, make a backup, extract the new source over the existing installation while preserving `.env`, `app\panel.sqlite`, and `servers`, rerun `deploy\install.ps1`, then start `start.bat`. Never replace those data paths with empty archive copies.

## Access recovery

Run Linux commands from the project root. On Windows replace `.venv/bin/python` with `.\.venv\Scripts\python.exe`.

| Situation | Action |
| --- | --- |
| Forgotten password | On sign-in open **Forgot your password?** and choose **Write code to log**. On Linux read the 10-minute code with `sudo journalctl -u roboport --since '10 minutes ago'`, then enter it at `/recovery` with a new password. Leave the username blank if the panel has one account. |
| Host-console recovery alternative | Run `.venv/bin/python -m app.cli emergency-access` (Windows: `.\.venv\Scripts\python.exe -m app.cli emergency-access`) to print a one-time `/recovery` code. |
| Forgotten username | `.venv/bin/python -m app.cli list-users` |
| Console password reset | `.venv/bin/python -m app.cli reset-password USERNAME` securely prompts for a password and revokes that user's sessions. |
| Authenticator lost; backup code available | Enter a backup code in the 2FA field after the password. It is single-use. |
| Authenticator and backup codes lost | `.venv/bin/python -m app.cli disable-2fa USERNAME` disables 2FA and revokes that user's sessions. Sign in with the password, enable 2FA again, and save new codes. |
| IP banned | `.venv/bin/python -m app.cli unban IP_ADDRESS` |

Running `.venv/bin/python -m app.cli` without arguments opens the interactive host console. Treat recovery codes, `.env`, database backups, and RCON passwords as secrets.

## Troubleshooting and references

| Symptom | Check |
| --- | --- |
| Linux panel will not start | `sudo journalctl -u roboport -n 100 --no-pager`, then `sudo systemctl status roboport`. |
| Docker actions fail | Confirm Docker is running. On Linux re-login after being added to `docker`; on Windows wait for Docker Desktop to show **Engine running**. |
| Browser cannot reach the panel | On Linux check nginx and use the HTTPS URL; on Windows use `http://127.0.0.1:8000/` from the same PC. |
| Factorio server cannot be joined | Check its configured UDP port and firewall/NAT rule; keep RCON private. |

Interactive API documentation is at `/api/v1/docs` on your panel. Translation contributors: [docs/localization.md](../docs/localization.md).
