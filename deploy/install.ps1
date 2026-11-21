# Roboport — one-click installer для Windows 11.
# Запуск из PowerShell в корне проекта:
#   powershell -ExecutionPolicy Bypass -File deploy\install.ps1
#
# Что делает:
#   1. Проверяет Python (>= 3.11). Если нет — пытается через winget; иначе говорит куда идти руками.
#   2. Проверяет Docker Desktop. Если нет — пытается winget; иначе показывает ссылку.
#   3. Создаёт .venv и ставит зависимости.
#   4. Создаёт start.bat и (опц.) ярлык автозагрузки в Startup-папке.
#
# Скрипт идемпотентный: повторный запуск ничего не сломает.

$ErrorActionPreference = 'Stop'

function Say  { Write-Host "▶ $args" -ForegroundColor Cyan }
function Ok   { Write-Host "✓ $args" -ForegroundColor Green }
function Warn { Write-Host "! $args" -ForegroundColor Yellow }
function Bad  { Write-Host "✗ $args" -ForegroundColor Red }

function AskYN([string]$q, [bool]$default = $false) {
    $hint = if ($default) { '[Y/n]' } else { '[y/N]' }
    $r = Read-Host "$q $hint"
    if ([string]::IsNullOrWhiteSpace($r)) { return $default }
    return $r -match '^[yY]'
}

# --- корень проекта ---
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir
Say "Корень проекта: $projectDir"

# --- 1. Python ---
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    Warn "Python не найден."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (AskYN "Поставить Python 3.13 через winget?" $true) {
            winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements
            $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
            $pyCmd = Get-Command python -ErrorAction SilentlyContinue
        }
    }
    if (-not $pyCmd) {
        Bad "Python всё равно нет. Поставь с https://www.python.org/downloads/windows/ (3.11+)"
        exit 1
    }
}
$pyVer = & python --version 2>&1
Ok "Python найден: $pyVer"

# --- 2. Docker Desktop ---
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Warn "Docker CLI не найден."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (AskYN "Поставить Docker Desktop через winget? (понадобится перезагрузка)" $true) {
            winget install --id Docker.DockerDesktop -e --accept-package-agreements --accept-source-agreements
            Warn "Docker Desktop поставлен. Запусти его вручную и убедись что в трее «Engine running». Потом перезапусти этот скрипт."
            exit 0
        }
    }
    if (-not $dockerCmd) {
        Bad "Docker Desktop нужен для запуска Factorio-серверов. Скачай: https://www.docker.com/products/docker-desktop/"
        if (-not (AskYN "Продолжить без docker (только инсталл панели)?" $false)) { exit 1 }
    }
} else {
    $dv = & docker version --format '{{.Server.Version}}' 2>$null
    if ($LASTEXITCODE -eq 0 -and $dv) {
        Ok "Docker daemon отвечает: $dv"
    } else {
        Warn "Docker CLI есть, но daemon не отвечает. Запусти Docker Desktop из меню Пуск."
    }
}

# --- 3. venv + deps ---
if (-not (Test-Path '.venv')) {
    Say "Создаю .venv"
    & python -m venv .venv
}
$venvPy = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Bad "Не нашёл .venv\Scripts\python.exe — что-то пошло не так при создании venv"
    exit 1
}
Say "Обновляю pip и ставлю зависимости"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Bad "pip install упал"; exit 1 }
Ok "Зависимости поставлены"

# --- 3.5. .env ---
if (-not (Test-Path '.env')) {
    Say "Создаю .env из .env.example"
    Copy-Item '.env.example' '.env'
    Warn "Отредактируй .env (FACTORIO_USERNAME/TOKEN) — или сделаешь это потом через UI: /settings"
}

# --- 4. start.bat ---
$startBat = Join-Path $projectDir 'start.bat'
$batBody = @"
@echo off
rem Roboport — запуск панели. Открой http://127.0.0.1:8000/
cd /d "$projectDir"
"$venvPy" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"@
Set-Content -Path $startBat -Value $batBody -Encoding ascii
Ok "Создал start.bat — двойной клик запустит панель"

# --- 5. автозагрузка (опционально) ---
$startupDir = [Environment]::GetFolderPath('Startup')
$autostartBat = Join-Path $startupDir 'Roboport.bat'
if (AskYN "Запускать панель автоматически при входе в Windows?" $false) {
    # Используем тот же start.bat, но с pythonw чтобы без чёрного окна
    $venvPyw = Join-Path $projectDir '.venv\Scripts\pythonw.exe'
    $silentBody = @"
@echo off
cd /d "$projectDir"
start "" "$venvPyw" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"@
    Set-Content -Path $autostartBat -Value $silentBody -Encoding ascii
    Ok "Автозагрузка включена: $autostartBat"
} elseif (Test-Path $autostartBat) {
    if (AskYN "Уже есть автозагрузка — снести?" $false) {
        Remove-Item $autostartBat -Force
        Ok "Автозагрузка снята"
    }
}

Write-Host ""
Ok "Готово."
& $venvPy -m app.cli setup-code
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "  Запуск:   двойной клик по start.bat (или $venvPy -m uvicorn app.main:app)"
Write-Host "  Панель:   http://127.0.0.1:8000/"
Write-Host "  Settings: http://127.0.0.1:8000/settings (там можно тоже включить/выключить автозагрузку)"
