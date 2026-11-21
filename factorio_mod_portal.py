#!/usr/bin/env python3
"""
CLI-утилита для Factorio Mod Portal API. Часть Roboport Controller.
Автор: Gellou (https://github.com/Hydrogell).

    py factorio_mod_portal.py sync                          # обновить кэш листинга
    py factorio_mod_portal.py search krastorio              # поиск по кэшу
    py factorio_mod_portal.py info Krastorio2               # последние 3 релиза
    py factorio_mod_portal.py info Krastorio2 2.0.16        # конкретный
    py factorio_mod_portal.py download Krastorio2 USER TOKEN [factorio_version]
    py factorio_mod_portal.py scan FOLDER                   # что лежит в папке
    py factorio_mod_portal.py check FOLDER                  # что устарело vs портал
    py factorio_mod_portal.py update FOLDER [USER TOKEN]    # patch-обновления (та же factorio_version)
    py factorio_mod_portal.py migrate FOLDER 2.0 [U T]      # переезд на новую factorio_version
    py factorio_mod_portal.py rollback FOLDER NAME [U T]    # откат на пред. версию
    py factorio_mod_portal.py history FOLDER                # лог изменений
    py factorio_mod_portal.py deps FOLDER                   # проверка зависимостей (warn only)

Кэш листинга: mods_cache.json рядом со скриптом.
История обновлений: .roboport-history.json в самой папке модов (источник правды для rollback).
Для update/rollback токен можно дать через env: FACTORIO_USERNAME / FACTORIO_TOKEN.
"""
import json
import re
import stat
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

API = "https://mods.factorio.com/api"
CACHE = Path(__file__).parent / "mods_cache.json"


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "roboport/0.0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def load_dotenv() -> None:
    """Подгрузить .env рядом со скриптом или в CWD. Без внешних зависимостей."""
    import os
    for candidate in (Path(__file__).parent / ".env", Path.cwd() / ".env"):
        if not candidate.exists():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # setdefault — переменные из shell имеют приоритет над .env
            os.environ.setdefault(key, value)
        return  # первый найденный .env побеждает


# ---------- Dependencies ----------

DEP_RE = re.compile(
    r"^\s*(?P<prefix>!|\?|\(\?\)|~)?\s*"
    r"(?P<name>[a-zA-Z0-9_\- ]+?)\s*"
    r"(?:(?P<op>>=|<=|=|>|<)\s*(?P<version>\d+(?:\.\d+){0,2}))?\s*$"
)
PREFIX_KIND = {None: "required", "?": "optional", "(?)": "hidden_optional",
               "!": "conflict", "~": "no_load_order"}


def parse_dep(s: str) -> dict:
    m = DEP_RE.match(s)
    if not m:
        return {"raw": s, "kind": "unknown"}
    return {"raw": s, "kind": PREFIX_KIND.get(m.group("prefix")),
            "name": m.group("name"), "op": m.group("op"), "version": m.group("version")}


# ---------- Sync (full listing) ----------

def save_catalog(path: Path, mods: list) -> dict:
    """Publish a complete listing for both CLI and panel readers."""
    payload = {"synced_at": int(time.time()), "count": len(mods), "mods": mods}
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".mod-catalog-") as directory:
        staged = Path(directory) / path.name
        with staged.open("w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False)
        if path.exists():
            staged.chmod(stat.S_IMODE(path.stat().st_mode))
        staged.replace(path)
    return payload


def cmd_sync() -> None:
    """Скачать весь листинг модов в локальный JSON.
       page_size=max — отдаёт всё за один запрос."""
    print("Качаю полный листинг с mods.factorio.com ...")
    t0 = time.time()
    url = f"{API}/mods?page_size=max"
    data = http_get_json(url)
    mods = data.get("results", [])
    save_catalog(CACHE, mods)
    print(f"Сохранено {len(mods)} модов в {CACHE.name} за {time.time()-t0:.1f}s "
          f"({CACHE.stat().st_size/1024/1024:.1f} MB)")


def load_cache() -> list:
    if not CACHE.exists():
        print(f"Кэша нет. Запусти: py {Path(__file__).name} sync")
        sys.exit(1)
    return json.loads(CACHE.read_text(encoding="utf-8"))["mods"]


# ---------- Search (local) ----------

def cmd_search(query: str) -> None:
    mods = load_cache()
    q = query.lower()
    matches = [
        m for m in mods
        if q in (m.get("name") or "").lower()
        or q in (m.get("title") or "").lower()
    ]
    matches.sort(key=lambda m: m.get("downloads_count", 0), reverse=True)

    print(f"{'name':<35} {'title':<45} downloads")
    print("-" * 95)
    for m in matches[:20]:
        title = (m.get('title') or '')[:43]
        print(f"{m['name']:<35} {title:<45} {m.get('downloads_count', 0):>10}")
    if len(matches) > 20:
        print(f"... ещё {len(matches) - 20} результатов")


# ---------- Info (online, /full) ----------

def cmd_info(name: str, only_version: str = None) -> None:
    url = f"{API}/mods/{urllib.parse.quote(name)}/full"
    data = http_get_json(url)

    print(f"Title:    {data.get('title')}")
    print(f"Owner:    {data.get('owner')}")
    print(f"Summary:  {(data.get('summary') or '')[:200]}")
    print(f"Releases: {len(data.get('releases', []))}")
    print()

    releases = data.get("releases", [])
    if only_version:
        releases = [r for r in releases if r["version"] == only_version]
    else:
        releases = releases[-3:]

    for rel in releases:
        info = rel.get("info_json", {})
        deps_raw = info.get("dependencies", []) or []
        print(f"v{rel['version']}  factorio={info.get('factorio_version')}  sha1={rel['sha1'][:12]}...")

        groups = {"required": [], "optional": [], "hidden_optional": [],
                  "conflict": [], "no_load_order": []}
        for d in deps_raw:
            p = parse_dep(d)
            groups.setdefault(p["kind"], []).append(p)

        for kind, label in [("required", "REQUIRED (auto-install)"),
                            ("optional", "OPTIONAL (ask user)"),
                            ("conflict", "CONFLICTS (block install)"),
                            ("hidden_optional", "hidden optional (skip in UI)")]:
            if groups[kind]:
                print(f"  {label}:")
                for d in groups[kind]:
                    c = f" {d['op']} {d['version']}" if d.get('op') else ""
                    print(f"    - {d['name']}{c}")
        print()


# ---------- Download ----------

def cmd_download(name: str, username: str, token: str, factorio_version: str = None) -> None:
    import hashlib
    url = f"{API}/mods/{urllib.parse.quote(name)}/full"
    data = http_get_json(url)

    releases = data.get("releases", [])
    if factorio_version:
        releases = [r for r in releases
                    if r.get("info_json", {}).get("factorio_version") == factorio_version]
    if not releases:
        print(f"No releases for factorio_version={factorio_version}")
        return

    rel = releases[-1]
    full_url = (f"https://mods.factorio.com{rel['download_url']}"
                f"?username={urllib.parse.quote(username)}&token={urllib.parse.quote(token)}")
    out = Path(rel["file_name"])
    print(f"Downloading {out.name} (v{rel['version']}, factorio {rel['info_json']['factorio_version']})...")

    req = urllib.request.Request(full_url, headers={"User-Agent": "roboport/0.0.1"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out.write_bytes(r.read())

    actual = hashlib.sha1(out.read_bytes()).hexdigest()
    ok = "OK" if actual == rel["sha1"] else f"MISMATCH (expected {rel['sha1']}, got {actual})"
    print(f"Saved {out} ({out.stat().st_size:,} bytes), sha1: {ok}")


# ---------- Local mod folder ----------

def parse_version(v: str) -> tuple:
    """'2.0.16' -> (2, 0, 16). Используется для сравнения версий."""
    try:
        return tuple(int(x) for x in v.split('.'))
    except (ValueError, AttributeError):
        return (0,)


def read_mod_info(zip_path: Path) -> dict | None:
    """Достать info.json из mod-zip. Factorio кладёт его в {name}_{version}/info.json."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            # info.json лежит в одной верхнеуровневой папке внутри архива
            candidates = [n for n in z.namelist()
                          if n.endswith('/info.json') and n.count('/') == 1]
            if not candidates:
                return None
            return json.loads(z.read(candidates[0]).decode('utf-8'))
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, KeyError):
        return None


def cmd_scan(folder: str) -> None:
    p = Path(folder)
    if not p.is_dir():
        print(f"Не папка: {folder}")
        return

    print(f"{'name':<35} {'version':<12} {'factorio':<10} file")
    print("-" * 90)

    valid = []
    invalid = []
    for zf in sorted(p.glob("*.zip")):
        info = read_mod_info(zf)
        if not info:
            invalid.append(zf.name)
            continue
        valid.append((info, zf))
        print(f"{info.get('name', '?'):<35} {info.get('version', '?'):<12} "
              f"{info.get('factorio_version', '?'):<10} {zf.name}")

    print(f"\nВсего: {len(valid)} модов")
    if invalid:
        print(f"Невалидные zip-файлы: {len(invalid)}")
        for n in invalid:
            print(f"  - {n}")

    print_warnings(check_local_deps(p))


def cmd_check(folder: str) -> None:
    p = Path(folder)
    if not p.is_dir():
        print(f"Не папка: {folder}")
        return

    print(f"{'name':<35} {'installed':<12} {'latest':<12} status")
    print("-" * 85)

    updates = 0
    for zf in sorted(p.glob("*.zip")):
        info = read_mod_info(zf)
        if not info:
            continue

        name = info['name']
        installed_v = info['version']
        factorio_v = info.get('factorio_version', '?')

        try:
            url = f"{API}/mods/{urllib.parse.quote(name)}/full"
            data = http_get_json(url)
        except urllib.error.HTTPError as e:
            status = f"portal HTTP {e.code}"
            print(f"{name:<35} {installed_v:<12} {'?':<12} {status}")
            continue

        # Только релизы под нужную версию Factorio
        compatible = [r for r in data.get('releases', [])
                      if r.get('info_json', {}).get('factorio_version') == factorio_v]
        if not compatible:
            print(f"{name:<35} {installed_v:<12} {'-':<12} нет релизов под factorio {factorio_v}")
            continue

        latest_v = compatible[-1]['version']
        iv = parse_version(installed_v)
        lv = parse_version(latest_v)

        if iv < lv:
            status = f"UPDATE доступен"
            updates += 1
        elif iv > lv:
            status = "ahead of portal (ручная сборка?)"
        else:
            status = "up to date"

        print(f"{name:<35} {installed_v:<12} {latest_v:<12} {status}")

    print(f"\nОбновлений доступно: {updates}")
    print_warnings(check_local_deps(p))


# ---------- History log ----------

HISTORY_FILE = ".roboport-history.json"


def load_history(folder: Path) -> list:
    f = folder / HISTORY_FILE
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def append_history(folder: Path, entry: dict) -> None:
    h = load_history(folder)
    h.append(entry)
    (folder / HISTORY_FILE).write_text(
        json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")


def get_credentials(username: str = None, token: str = None) -> tuple:
    import os
    username = username or os.environ.get("FACTORIO_USERNAME")
    token = token or os.environ.get("FACTORIO_TOKEN")
    if not username or not token:
        return None, None
    return username, token


def download_release(rel: dict, username: str, token: str, target_dir: Path) -> Path:
    """Скачать релиз с проверкой sha1. Возвращает путь к файлу или кидает исключение."""
    import hashlib
    full_url = (f"https://mods.factorio.com{rel['download_url']}"
                f"?username={urllib.parse.quote(username)}"
                f"&token={urllib.parse.quote(token)}")
    target = target_dir / rel['file_name']
    tmp = target_dir / (rel['file_name'] + ".tmp")

    req = urllib.request.Request(full_url, headers={"User-Agent": "roboport/0.0.1"})
    with urllib.request.urlopen(req, timeout=300) as r:
        tmp.write_bytes(r.read())

    actual = hashlib.sha1(tmp.read_bytes()).hexdigest()
    if actual != rel['sha1']:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"sha1 mismatch: ждали {rel['sha1'][:12]}.., получили {actual[:12]}..")

    if target.exists():
        target.unlink()
    tmp.rename(target)
    return target


def find_release(mod_name: str, version: str, factorio_version: str) -> dict:
    """Найти конкретный релиз мода в портале. None если не нашли."""
    data = http_get_json(f"{API}/mods/{urllib.parse.quote(mod_name)}/full")
    for r in data.get('releases', []):
        if (r['version'] == version and
                r.get('info_json', {}).get('factorio_version') == factorio_version):
            return r
    return None


def cmd_update(folder: str, username: str = None, token: str = None) -> None:
    username, token = get_credentials(username, token)
    if not username:
        print("Нужны username и token (аргументами или в env FACTORIO_USERNAME/FACTORIO_TOKEN)")
        return

    p = Path(folder)
    if not p.is_dir():
        print(f"Не папка: {folder}")
        return

    installed = []
    for zf in sorted(p.glob("*.zip")):
        info = read_mod_info(zf)
        if info:
            installed.append((info['name'], info['version'],
                              info.get('factorio_version', '?'), zf))

    if not installed:
        print("В папке нет валидных модов")
        return

    print("Проверяю портал...")
    todo = []
    for name, installed_v, factorio_v, zip_path in installed:
        try:
            data = http_get_json(f"{API}/mods/{urllib.parse.quote(name)}/full")
        except urllib.error.HTTPError as e:
            print(f"  {name}: portal HTTP {e.code}, пропускаю")
            continue

        compatible = [r for r in data.get('releases', [])
                      if r.get('info_json', {}).get('factorio_version') == factorio_v]
        if not compatible:
            continue

        latest = compatible[-1]
        if parse_version(installed_v) >= parse_version(latest['version']):
            continue

        # sha1 текущего файла — нужен для истории, чтобы откатиться смогли
        import hashlib
        installed_sha1 = hashlib.sha1(zip_path.read_bytes()).hexdigest()
        todo.append((name, installed_v, installed_sha1, factorio_v, latest, zip_path))

    if not todo:
        print("\nВсё актуально")
        return

    print(f"\nК обновлению ({len(todo)}):")
    for name, iv, _, _, latest, _ in todo:
        print(f"  {name}: {iv} -> {latest['version']}")

    # Pre-flight: что станет с зависимостями
    future = collect_state_from_folder(p)
    for name, _, _, factorio_v, latest, _ in todo:
        future[name] = {
            "version": latest["version"],
            "factorio_version": factorio_v,
            "dependencies": latest.get("info_json", {}).get("dependencies", []) or [],
        }
    print_diff(p, future)

    if input("\nПродолжить? (y/N): ").strip().lower() != "y":
        print("Отменено")
        return

    ok_count = 0
    for name, iv, isha, factorio_v, rel, old_path in todo:
        print(f"\n[{name}] {iv} -> {rel['version']}")
        try:
            new_path = download_release(rel, username, token, p)
        except Exception as e:
            print(f"  провал: {e}")
            continue

        # Удаляем старый файл (если у него другое имя — например смена версии в имени)
        if old_path.exists() and old_path != new_path:
            old_path.unlink()

        append_history(p, {
            "ts": int(time.time()),
            "action": "update",
            "name": name,
            "factorio_version": factorio_v,
            "from_version": iv,
            "from_sha1": isha,
            "to_version": rel['version'],
            "to_sha1": rel['sha1'],
        })

        print(f"  скачан {new_path.name}, sha1 OK, история записана")
        ok_count += 1

    print(f"\nГотово: {ok_count}/{len(todo)} модов обновлено")
    print_warnings(check_local_deps(p))


def cmd_rollback(folder: str, mod_name: str, username: str = None, token: str = None) -> None:
    username, token = get_credentials(username, token)
    if not username:
        print("Нужны username и token (аргументами или в env FACTORIO_USERNAME/FACTORIO_TOKEN)")
        return

    p = Path(folder)
    if not p.is_dir():
        print(f"Не папка: {folder}")
        return

    history = load_history(p)
    relevant = [h for h in history if h.get("name") == mod_name]
    if not relevant:
        print(f"В истории нет записей по моду '{mod_name}'")
        return

    last = relevant[-1]
    target_version = last["from_version"]
    target_sha1 = last["from_sha1"]
    factorio_v = last["factorio_version"]
    current_version = last["to_version"]

    print(f"Откат: {mod_name} {current_version} -> {target_version}")

    rel = find_release(mod_name, target_version, factorio_v)
    if not rel:
        print(f"Не нашёл релиз {target_version} под factorio {factorio_v} в портале "
              f"(автор мог удалить эту версию)")
        return

    if rel['sha1'] != target_sha1:
        print(f"  ПРЕДУПРЕЖДЕНИЕ: sha1 на портале сейчас {rel['sha1'][:12]}.., "
              f"в истории был {target_sha1[:12]}.. (автор перевыложил версию)")
        if input("  Продолжить? (y/N): ").strip().lower() != "y":
            return

    # Удалить текущий файл (ищем по name мода, не доверяя имени файла)
    current_file = None
    for zf in p.glob("*.zip"):
        info = read_mod_info(zf)
        if info and info.get('name') == mod_name:
            current_file = zf
            break

    try:
        new_path = download_release(rel, username, token, p)
    except Exception as e:
        print(f"  провал: {e}")
        return

    if current_file and current_file != new_path and current_file.exists():
        current_file.unlink()

    append_history(p, {
        "ts": int(time.time()),
        "action": "rollback",
        "name": mod_name,
        "factorio_version": factorio_v,
        "from_version": current_version,
        "from_sha1": last["to_sha1"],
        "to_version": target_version,
        "to_sha1": rel['sha1'],
    })

    print(f"  откачен на {target_version}, история записана")
    print_warnings(check_local_deps(p))


def cmd_migrate(folder: str, target_factorio: str,
                username: str = None, token: str = None) -> None:
    import hashlib
    username, token = get_credentials(username, token)
    if not username:
        print("Нужны username и token (аргументами или в env FACTORIO_USERNAME/FACTORIO_TOKEN)")
        return

    p = Path(folder)
    if not p.is_dir():
        print(f"Не папка: {folder}")
        return

    installed = []
    for zf in sorted(p.glob("*.zip")):
        info = read_mod_info(zf)
        if info:
            installed.append((info['name'], info['version'],
                              info.get('factorio_version', '?'), zf))

    if not installed:
        print("В папке нет валидных модов")
        return

    print(f"Проверяю совместимость с Factorio {target_factorio}...")

    todo = []           # уже не на target и есть совместимый релиз
    no_target = []      # нет релизов под target
    already = []        # уже на target

    for name, old_v, old_fv, old_path in installed:
        if old_fv == target_factorio:
            already.append((name, old_v))
            continue
        try:
            data = http_get_json(f"{API}/mods/{urllib.parse.quote(name)}/full")
        except urllib.error.HTTPError as e:
            no_target.append((name, f"portal HTTP {e.code}"))
            continue

        compatible = [r for r in data.get('releases', [])
                      if r.get('info_json', {}).get('factorio_version') == target_factorio]
        if not compatible:
            no_target.append((name, f"автор не выпустил релиз под {target_factorio}"))
            continue

        todo.append((name, old_v, old_fv, old_path, compatible[-1]))

    if already:
        print(f"\nУже на Factorio {target_factorio}:")
        for n, v in already:
            print(f"  {n} ({v})")

    if todo:
        print(f"\nБудут переведены на Factorio {target_factorio}:")
        for name, ov, ofv, _, rel in todo:
            print(f"  {name}: {ov} (fa {ofv}) -> {rel['version']} (fa {target_factorio})")

    if no_target:
        print(f"\nНЕТ совместимых релизов под Factorio {target_factorio}:")
        for n, why in no_target:
            print(f"  {n}: {why}")
        print("  -> либо отказаться от мода, либо оставить на старой версии")

    if not todo:
        print("\nНечего мигрировать")
        return

    print("\nВНИМАНИЕ: миграция между major-версиями Factorio. "
          "Старые сейвы на новых модах могут не открыться или потребовать миграции в игре.")

    # Pre-flight: что станет с зависимостями после миграции
    future = collect_state_from_folder(p)
    for name, _, _, _, rel in todo:
        future[name] = {
            "version": rel["version"],
            "factorio_version": target_factorio,
            "dependencies": rel.get("info_json", {}).get("dependencies", []) or [],
        }
    print_diff(p, future)

    if input("\nПродолжить? (y/N): ").strip().lower() != "y":
        print("Отменено")
        return

    ok_count = 0
    for name, old_v, old_fv, old_path, rel in todo:
        print(f"\n[{name}] {old_v} (fa {old_fv}) -> {rel['version']} (fa {target_factorio})")
        old_sha1 = hashlib.sha1(old_path.read_bytes()).hexdigest()

        try:
            new_path = download_release(rel, username, token, p)
        except Exception as e:
            print(f"  провал: {e}")
            continue

        if old_path.exists() and old_path != new_path:
            old_path.unlink()

        append_history(p, {
            "ts": int(time.time()),
            "action": "migrate",
            "name": name,
            "factorio_version": target_factorio,
            "from_version": old_v,
            "from_sha1": old_sha1,
            "from_factorio_version": old_fv,
            "to_version": rel['version'],
            "to_sha1": rel['sha1'],
        })

        print(f"  скачан {new_path.name}, sha1 OK")
        ok_count += 1

    print(f"\nГотово: {ok_count}/{len(todo)} модов мигрировано на Factorio {target_factorio}")
    print_warnings(check_local_deps(p))


# ---------- Local dependency checker (no network) ----------

# Эти моды не лежат в папке /mods, они идут с DLC и активируются автоматически
DLC_MODS = {"space-age", "quality", "elevated-rails"}


def _version_satisfies(installed_v: str, op: str, required_v: str) -> bool:
    if not op or not required_v:
        return True
    iv = parse_version(installed_v)
    rv = parse_version(required_v)
    return {
        ">=": iv >= rv, "<=": iv <= rv,
        ">":  iv > rv,  "<":  iv < rv,
        "=":  iv == rv,
    }.get(op, True)


def collect_state_from_folder(folder: Path) -> dict:
    """name -> {version, factorio_version, dependencies}"""
    state = {}
    for zf in folder.glob("*.zip"):
        info = read_mod_info(zf)
        if not info:
            continue
        state[info["name"]] = {
            "version": info["version"],
            "factorio_version": info.get("factorio_version"),
            "dependencies": info.get("dependencies", []) or [],
        }
    return state


def check_deps_for_state(state: dict) -> list[dict]:
    """Проверить state на нарушения зависимостей. Категории:
       CONFLICT — несовместимый мод установлен (kind=conflict сработал)
       MISSING  — required мод отсутствует
       VERSION  — required установлен, но версия не та
       DLC      — required = моду из DLC, нужно DLC активировать"""
    warnings = []
    for mod_name, mod in state.items():
        for dep_raw in mod["dependencies"]:
            dep = parse_dep(dep_raw)
            kind = dep.get("kind")
            dep_name = dep.get("name")
            if not dep_name or kind in (None, "unknown", "no_load_order",
                                         "optional", "hidden_optional"):
                continue

            if kind == "conflict":
                if dep_name in state:
                    warnings.append({
                        "category": "CONFLICT", "level": "warn", "mod": mod_name,
                        "msg": f"конфликт с {dep_name} {state[dep_name]['version']} (установлен)",
                    })

            elif kind == "required":
                if dep_name == "base":
                    continue
                if dep_name in DLC_MODS and dep_name not in state:
                    warnings.append({
                        "category": "DLC", "level": "info", "mod": mod_name,
                        "msg": f"требует '{dep_name}' (Space Age DLC). "
                               f"DLC должен быть активирован на сервере (DLC_SPACE_AGE=true).",
                    })
                    continue
                if dep_name not in state:
                    constraint = f" {dep['op']} {dep['version']}" if dep.get("op") else ""
                    warnings.append({
                        "category": "MISSING", "level": "warn", "mod": mod_name,
                        "msg": f"требует '{dep_name}{constraint}', но он не установлен",
                    })
                else:
                    iv = state[dep_name]["version"]
                    if not _version_satisfies(iv, dep.get("op"), dep.get("version")):
                        warnings.append({
                            "category": "VERSION", "level": "warn", "mod": mod_name,
                            "msg": f"требует {dep_name} {dep['op']} {dep['version']}, "
                                   f"установлен {iv}",
                        })
    return warnings


def check_local_deps(folder: Path) -> list[dict]:
    return check_deps_for_state(collect_state_from_folder(folder))


def _wkey(w: dict) -> tuple:
    return (w["category"], w["mod"], w["msg"])


def diff_warnings(before: list[dict], after: list[dict]) -> tuple:
    """Возвращает (исчезли, появились, остались)."""
    before_keys = {_wkey(w): w for w in before}
    after_keys = {_wkey(w): w for w in after}
    gone = [w for k, w in before_keys.items() if k not in after_keys]
    added = [w for k, w in after_keys.items() if k not in before_keys]
    same = [w for k, w in after_keys.items() if k in before_keys]
    return gone, added, same


def print_warnings(warnings: list, header: str = "Зависимости") -> None:
    if not warnings:
        return
    # Сортируем: CONFLICT первыми (они опаснее), потом MISSING, VERSION, DLC
    order = {"CONFLICT": 0, "MISSING": 1, "VERSION": 2, "DLC": 3}
    warnings = sorted(warnings, key=lambda w: order.get(w["category"], 99))
    print(f"\n{header}:")
    for w in warnings:
        tag = f"[{w['category']}]"
        print(f"  {tag:<11} [{w['mod']}] {w['msg']}")


def print_diff(folder: Path, future_state: dict) -> None:
    """Показать какие проблемы появятся/исчезнут при переходе в future_state."""
    before = check_local_deps(folder)
    after = check_deps_for_state(future_state)
    gone, added, _ = diff_warnings(before, after)
    if gone:
        print("\nПосле операции ИСЧЕЗНУТ:")
        for w in gone:
            print(f"  - [{w['category']}] [{w['mod']}] {w['msg']}")
    if added:
        print("\nПосле операции ПОЯВЯТСЯ:")
        for w in added:
            print(f"  + [{w['category']}] [{w['mod']}] {w['msg']}")


def cmd_deps(folder: str) -> None:
    p = Path(folder)
    if not p.is_dir():
        print(f"Не папка: {folder}")
        return
    warnings = check_local_deps(p)
    if not warnings:
        print("Зависимости в порядке: required удовлетворены, конфликтов нет")
        return
    print_warnings(warnings, header="Найдено проблем по зависимостям")


def cmd_history(folder: str) -> None:
    p = Path(folder)
    if not p.is_dir():
        print(f"Не папка: {folder}")
        return

    h = load_history(p)
    if not h:
        print("История пуста")
        return

    from datetime import datetime
    print(f"{'when':<20} {'action':<10} {'name':<30} change")
    print("-" * 95)
    for e in h:
        when = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        change = f"{e['from_version']} -> {e['to_version']}"
        print(f"{when:<20} {e['action']:<10} {e['name']:<30} {change}")


if __name__ == "__main__":
    load_dotenv()
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    try:
        if cmd == "sync":
            cmd_sync()
        elif cmd == "search" and len(sys.argv) >= 3:
            cmd_search(sys.argv[2])
        elif cmd == "info" and len(sys.argv) >= 3:
            cmd_info(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        elif cmd == "download" and len(sys.argv) >= 5:
            cmd_download(sys.argv[2], sys.argv[3], sys.argv[4],
                         sys.argv[5] if len(sys.argv) > 5 else None)
        elif cmd == "scan" and len(sys.argv) >= 3:
            cmd_scan(sys.argv[2])
        elif cmd == "check" and len(sys.argv) >= 3:
            cmd_check(sys.argv[2])
        elif cmd == "update" and len(sys.argv) >= 3:
            user = sys.argv[3] if len(sys.argv) > 3 else None
            tok = sys.argv[4] if len(sys.argv) > 4 else None
            cmd_update(sys.argv[2], user, tok)
        elif cmd == "rollback" and len(sys.argv) >= 4:
            user = sys.argv[4] if len(sys.argv) > 4 else None
            tok = sys.argv[5] if len(sys.argv) > 5 else None
            cmd_rollback(sys.argv[2], sys.argv[3], user, tok)
        elif cmd == "migrate" and len(sys.argv) >= 4:
            user = sys.argv[4] if len(sys.argv) > 4 else None
            tok = sys.argv[5] if len(sys.argv) > 5 else None
            cmd_migrate(sys.argv[2], sys.argv[3], user, tok)
        elif cmd == "history" and len(sys.argv) >= 3:
            cmd_history(sys.argv[2])
        elif cmd == "deps" and len(sys.argv) >= 3:
            cmd_deps(sys.argv[2])
        else:
            print(__doc__); sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}")
        sys.exit(1)
