#!/usr/bin/env python3
"""
Установщик/апдейтер бинаря Factorio-сервера.

  py factorio_installer.py status                  # что установлено + что доступно
  py factorio_installer.py list                    # stable/experimental версии
  py factorio_installer.py install [VERSION]       # установить (по умолчанию stable)
  py factorio_installer.py update                  # обновить до stable, если новее
  py factorio_installer.py uninstall               # снести установку

Платформа определяется автоматически:
  - Linux   → headless/linux64    (.tar.xz, без авторизации)
  - Windows → alpha/win64-manual  (portable .zip с топ-папкой Factorio_X.Y.Z/,
                                   нужны FACTORIO_USERNAME/FACTORIO_TOKEN)

Установка идёт в ./factorio/ рядом со скриптом.
Метаданные текущей установки: ./factorio/.installed.json
"""
import json
import os
import platform
import shutil
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
INSTALL_DIR = ROOT / "factorio"
META_FILE = INSTALL_DIR / ".installed.json"
DOWNLOAD_DIR = ROOT / ".cache" / "downloads"

API_RELEASES = "https://factorio.com/api/latest-releases"
DOWNLOAD_BASE = "https://factorio.com/get-download"
UA = "roboport-installer/0.0.1"


# ---------- .env ----------

def load_dotenv() -> None:
    """Подгрузить .env рядом со скриптом. Без внешних зависимостей."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------- Platform ----------

def detect_platform() -> tuple[str, str, str]:
    """Возвращает (build, platform_slug, archive_ext) для текущей ОС."""
    sysname = platform.system()
    if sysname == "Linux":
        return "headless", "linux64", "tar.xz"
    if sysname == "Windows":
        # win64-manual у factorio.com — это portable ZIP, несмотря на название.
        return "alpha", "win64-manual", "zip"
    if sysname == "Darwin":
        return "alpha", "osx", "dmg"
    raise RuntimeError(f"Неподдерживаемая ОС: {sysname}")


def needs_auth(build: str) -> bool:
    """headless/demo раздаются без авторизации, alpha — с токеном."""
    return build == "alpha"


# ---------- HTTP ----------

def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def download(url: str, dest: Path, params: dict | None = None) -> None:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        chunk = 1 << 16
        last_print = 0.0
        with tmp.open("wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                now = time.monotonic()
                if now - last_print >= 0.2 or done == total:
                    if total:
                        pct = 100 * done / total
                        print(f"\r  {done/1048576:7.1f} / {total/1048576:.1f} MB  ({pct:5.1f}%)",
                              end="", flush=True)
                    else:
                        print(f"\r  {done/1048576:7.1f} MB", end="", flush=True)
                    last_print = now
        print()
    tmp.replace(dest)


# ---------- Releases ----------

def get_releases() -> dict:
    return http_json(API_RELEASES)


def resolve_version(version: str, build: str) -> str:
    """'stable' / 'latest' → конкретная версия для нашего build."""
    if version not in ("stable", "latest"):
        return version
    rel = get_releases()
    channel = "stable" if version == "stable" else "experimental"
    if build not in rel.get(channel, {}):
        raise RuntimeError(f"Нет {build} в канале {channel}: {rel}")
    return rel[channel][build]


# ---------- Meta ----------

def read_meta() -> dict | None:
    if not META_FILE.exists():
        return None
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_meta(data: dict) -> None:
    META_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------- Install ----------

def fetch_installer(version: str, build: str, plat: str, ext: str) -> Path:
    """Скачать архив/установщик в .cache/downloads/, вернуть путь."""
    fname = f"factorio_{build}_{plat}_{version}.{ext}"
    dest = DOWNLOAD_DIR / fname
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  Кеш: {dest.relative_to(ROOT)}")
        return dest

    url = f"{DOWNLOAD_BASE}/{version}/{build}/{plat}"
    params: dict | None = None
    if needs_auth(build):
        load_dotenv()
        user = os.environ.get("FACTORIO_USERNAME")
        token = os.environ.get("FACTORIO_TOKEN")
        if not user or not token:
            raise RuntimeError(
                "Для alpha-сборки (Windows/macOS) нужен FACTORIO_USERNAME/FACTORIO_TOKEN в .env\n"
                "Токен: https://www.factorio.com/profile (reveal)"
            )
        params = {"username": user, "token": token}

    print(f"  Скачиваю {url}")
    download(url, dest, params)
    return dest


def extract_tarxz(archive: Path, target: Path) -> None:
    """Linux: tar.xz содержит верхнеуровневый factorio/. Распаковываем в target.parent
    так, чтобы получилось target/."""
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Распаковка {archive.name} → {target}")
    with tarfile.open(archive, "r:xz") as tf:
        tf.extractall(target.parent)
    # архив раскрывается в target.parent/factorio — это уже наш target


def extract_zip_strip_top(archive: Path, target: Path) -> None:
    """Windows: распаковка zip с удалением верхней папки (Factorio_X.Y.Z/ → factorio/)."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    print(f"  Распаковка {archive.name} → {target}")
    with zipfile.ZipFile(archive, "r") as zf:
        names = zf.namelist()
        # Определяем общий префикс (имя верхней папки).
        top = names[0].split("/", 1)[0] + "/"
        if not all(n.startswith(top) or n == top.rstrip("/") for n in names):
            raise RuntimeError(f"Ожидалась общая верхняя папка, нашёл смешанную структуру: {top}")
        total = len(names)
        for i, name in enumerate(names, 1):
            rel = name[len(top):]
            if not rel:
                continue  # сама верхняя папка
            out = target / rel
            if name.endswith("/"):
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            if i % 500 == 0 or i == total:
                print(f"\r  {i}/{total} файлов", end="", flush=True)
        print()


def install(version_spec: str = "stable") -> None:
    build, plat, ext = detect_platform()
    version = resolve_version(version_spec, build)
    print(f"Платформа: {platform.system()}  →  build={build}  platform={plat}")
    print(f"Версия: {version_spec}  →  {version}")

    current = read_meta()
    if current and current.get("version") == version:
        print(f"Уже установлено: {version}. Нечего делать.")
        return

    archive = fetch_installer(version, build, plat, ext)

    if ext == "tar.xz":
        extract_tarxz(archive, INSTALL_DIR)
    elif ext == "zip":
        extract_zip_strip_top(archive, INSTALL_DIR)
    else:
        raise RuntimeError(f"Не реализована распаковка для .{ext}")

    write_meta({
        "version": version,
        "build": build,
        "platform": plat,
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    print(f"\nГотово: {INSTALL_DIR}")
    print_binary_hint()


def uninstall() -> None:
    if not INSTALL_DIR.exists():
        print("Нечего удалять.")
        return
    shutil.rmtree(INSTALL_DIR)
    print(f"Удалено: {INSTALL_DIR}")


# ---------- Status / List ----------

def find_binary() -> Path | None:
    """Ищет бинарь Factorio в стандартных местах внутри INSTALL_DIR."""
    if not INSTALL_DIR.exists():
        return None
    candidates = [
        INSTALL_DIR / "bin" / "x64" / "factorio.exe",  # Windows
        INSTALL_DIR / "bin" / "x64" / "factorio",      # Linux
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def print_binary_hint() -> None:
    binp = find_binary()
    if binp:
        print(f"Бинарь: {binp}")
    else:
        print("⚠ Бинарь не найден в ожидаемых путях. Загляни в ./factorio/")


def cmd_status() -> None:
    meta = read_meta()
    if meta:
        print(f"Установлено: {meta['version']}  ({meta['build']}/{meta['platform']})")
        print(f"  Дата: {meta.get('installed_at', '?')}")
        print_binary_hint()
    else:
        print("Установлено: ничего")
    print()
    cmd_list()


def cmd_list() -> None:
    rel = get_releases()
    print("Доступно на factorio.com:")
    for channel in ("stable", "experimental"):
        print(f"  {channel}:")
        for build, ver in rel.get(channel, {}).items():
            print(f"    {build:<10} {ver}")


def cmd_update() -> None:
    build, plat, _ = detect_platform()
    latest = resolve_version("stable", build)
    meta = read_meta()
    if meta and meta.get("version") == latest:
        print(f"Уже на последней stable: {latest}")
        return
    if meta:
        print(f"Обновление {meta['version']} → {latest}")
    install("stable")


# ---------- CLI ----------

def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "status":
        cmd_status()
    elif cmd == "list":
        cmd_list()
    elif cmd == "install":
        install(sys.argv[2] if len(sys.argv) > 2 else "stable")
    elif cmd == "update":
        cmd_update()
    elif cmd == "uninstall":
        uninstall()
    else:
        print(f"Неизвестная команда: {cmd}")
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
