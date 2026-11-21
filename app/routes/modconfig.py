"""Вкладка modconfig — редактор mod-settings.dat по модам.

Что делаем:
  GET  /servers/{uuid}/modconfig         — листинг (через server_tab handler в servers.py)
  POST /servers/{uuid}/modconfig/update  — изменить одно значение
  POST /servers/{uuid}/modconfig/revert  — удалить запись (Factorio запишет default на старте)

Стратегия:
  1) parse mod-settings.dat (наш кодек в app/mod_settings.py).
  2) Чтобы группировать по модам — индексируем mods/<*>.zip: ищем в settings*.lua
     все паттерны name = "...", связываем setting-name → mod-name (по zip-файлу).
     Heuristic, но простая и стабильная. Settings без матча идут в группу «Прочее».
  3) Тип value берётся из текущего файла (bool/double/i64/u64/string), при сохранении
     сохраняем тот же тип — иначе Factorio упадёт при чтении.
"""
from __future__ import annotations

import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import mod_settings as ms, server_data
from ..db import get_db
from ..i18n import tr
from .server_common import require_server

router = APIRouter(prefix="/servers")

_SETTING_FILES_RE = re.compile(r"(^|/)settings(-updates|-final-fixes)?\.lua$", re.IGNORECASE)
_BLOCK_COMMENT_RE = re.compile(r"--\[\[.*?\]\]", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
# field = "..." (строка без интерполяции)
_STR_FIELD_RE = re.compile(r'(?:^|[^\w])({field})\s*=\s*"((?:[^"\\]|\\.)*)"\s*[,;\n}}]')
# field = true|false
_BOOL_FIELD_RE = re.compile(r'(?:^|[^\w])({field})\s*=\s*(true|false)\b')
# field = <number>
_NUM_FIELD_RE = re.compile(r'(?:^|[^\w])({field})\s*=\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)')
# name = "PREFIX" .. anything — для динамических settings в циклах
_PREFIX_NAME_RE = re.compile(r'(?:^|[^\w])name\s*=\s*"((?:[^"\\]|\\.)*)"\s*\.\.')
# allowed_values = {"a", "b", ...}
_ALLOWED_RE = re.compile(r'(?:^|[^\w])allowed_values\s*=\s*\{([^{}]*)\}')
# allowed_values = funcname() | funcname — резолвим через таблицу локалей файла
_ALLOWED_REF_RE = re.compile(r'(?:^|[^\w])allowed_values\s*=\s*([A-Za-z_][\w]*)\s*(\(\))?')
# Сборщики локальных «строковых-списочных» определений в файле:
#   local foo = {"a", "b"}
#   local foo = function() return {"a", "b"} end
_LOCAL_LIST_RE = re.compile(
    r'\blocal\s+([A-Za-z_]\w*)\s*=\s*\{((?:[^{}]|\\\")*?)\}',
    re.DOTALL,
)
_LOCAL_FN_LIST_RE = re.compile(
    r'\blocal\s+([A-Za-z_]\w*)\s*=\s*function\s*\([^)]*\)\s*return\s*\{((?:[^{}]|\\\")*?)\}\s*end',
    re.DOTALL,
)

_TYPE_TO_KIND = {
    "bool-setting":   "bool",
    "int-setting":    "i64",
    "double-setting": "double",
    "string-setting": "string",
    # color-setting не редактируем (R/G/B/A в нашей таблице)
}


def _strip_comments(src: str) -> str:
    src = _BLOCK_COMMENT_RE.sub("", src)
    src = _LINE_COMMENT_RE.sub("", src)
    return src


def _balanced_blocks(src: str):
    """Yield все балансные {...} блоки (включая вложенные, в close-order).
    Возвращает (block_text, start_index_in_src)."""
    stack: list[int] = []
    in_str = False
    str_quote = ""
    i = 0
    while i < len(src):
        c = src[i]
        if in_str:
            if c == "\\" and i + 1 < len(src):
                i += 2
                continue
            if c == str_quote:
                in_str = False
        else:
            if c in ("'", '"'):
                in_str = True
                str_quote = c
            elif c == "{":
                stack.append(i)
            elif c == "}":
                if stack:
                    start = stack.pop()
                    yield src[start:i + 1], start
        i += 1


def _field_str(block: str, field: str) -> str | None:
    m = re.compile(_STR_FIELD_RE.pattern.format(field=re.escape(field))).search(block)
    return m.group(2) if m else None


def _field_bool(block: str, field: str) -> bool | None:
    m = re.compile(_BOOL_FIELD_RE.pattern.format(field=re.escape(field))).search(block)
    if not m:
        return None
    return m.group(2) == "true"


def _field_num(block: str, field: str) -> float | None:
    m = re.compile(_NUM_FIELD_RE.pattern.format(field=re.escape(field))).search(block)
    return float(m.group(2)) if m else None


def _field_allowed(block: str, local_lists: dict[str, list[str]] | None = None) -> list[str] | None:
    m = _ALLOWED_RE.search(block)
    if m:
        out = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
        return out or None
    # allowed_values = name() | name — поищем в локалях файла
    if local_lists:
        m = _ALLOWED_REF_RE.search(block)
        if m and m.group(1) in local_lists:
            return list(local_lists[m.group(1)])
    return None


def _collect_local_string_lists(src: str) -> dict[str, list[str]]:
    """Собирает локальные определения списков строк, на которые могут ссылаться
    allowed_values: 'local foo = {"a","b"}' и 'local foo = function() return {...} end'.
    Парсится грубо, без вложенных таблиц — для типичных allowed_values этого достаточно."""
    out: dict[str, list[str]] = {}
    for m in _LOCAL_FN_LIST_RE.finditer(src):
        name, body = m.group(1), m.group(2)
        out[name] = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
    for m in _LOCAL_LIST_RE.finditer(src):
        name, body = m.group(1), m.group(2)
        out.setdefault(name, re.findall(r'"((?:[^"\\]|\\.)*)"', body))
    return out


def _make_spec(block: str, mod_name: str, type_str: str,
               name: str | None, prefix: str | None,
               local_lists: dict[str, list[str]] | None = None) -> dict | None:
    """Извлечь spec настройки из её block-текста."""
    kind = _TYPE_TO_KIND.get(type_str)
    if kind is None:
        return None
    spec: dict = {
        "mod": mod_name, "type": type_str, "kind": kind,
        "setting_type": _field_str(block, "setting_type") or "startup",
        "name": name, "prefix": prefix,
    }
    if kind == "bool":
        spec["default"] = _field_bool(block, "default_value")
    elif kind == "string":
        spec["default"] = _field_str(block, "default_value")
        spec["allowed"] = _field_allowed(block, local_lists)
    elif kind in ("i64", "double"):
        spec["default"] = _field_num(block, "default_value")
        spec["min"] = _field_num(block, "minimum_value")
        spec["max"] = _field_num(block, "maximum_value")
    return spec


def _parse_settings_lua(src: str, mod_name: str) -> tuple[dict[str, dict], list[dict]]:
    """Парсит settings*.lua → (статические spec'и {name: spec}, prefix-spec'и [...]).
    Prefix-spec'и применяются к именам, не покрытым статикой, по startswith."""
    src = _strip_comments(src)
    local_lists = _collect_local_string_lists(src)
    statics: dict[str, dict] = {}
    prefixes: list[dict] = []
    for block, _start in _balanced_blocks(src):
        if "type" not in block or "-setting" not in block:
            continue
        type_str = _field_str(block, "type")
        if not type_str or not type_str.endswith("-setting"):
            continue
        name = _field_str(block, "name")
        if name:
            spec = _make_spec(block, mod_name, type_str, name, None, local_lists)
            if spec:
                statics[name] = spec
            continue
        m = _PREFIX_NAME_RE.search(block)
        if m:
            prefix = m.group(1)
            spec = _make_spec(block, mod_name, type_str, None, prefix, local_lists)
            if spec:
                prefixes.append(spec)
    return statics, prefixes


def build_setting_specs(mods_dir: Path) -> dict[str, dict]:
    """Сканирует моды и возвращает {setting_name: spec}.
    Для динамически-генерируемых имён собираем prefix-таблицу и затем
    применяем её к именам, не найденным статически. Это закрывает паттерн
    `name = "modname-suffix-" .. var` (alien-biomes и подобные)."""
    out: dict[str, dict] = {}
    all_prefixes: list[dict] = []
    if not mods_dir.is_dir():
        return out
    for zp in sorted(mods_dir.glob("*.zip")):
        stem = zp.stem
        mod_name = stem.rsplit("_", 1)[0] if "_" in stem else stem
        try:
            with zipfile.ZipFile(zp) as z:
                for entry in z.namelist():
                    if not _SETTING_FILES_RE.search(entry):
                        continue
                    try:
                        src = z.read(entry).decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    statics, prefixes = _parse_settings_lua(src, mod_name)
                    for k, v in statics.items():
                        out.setdefault(k, v)
                    all_prefixes.extend(prefixes)
        except (zipfile.BadZipFile, KeyError):
            continue

    # Сортируем prefix'ы по убыванию длины — длиннее побеждает (точнее матч).
    all_prefixes.sort(key=lambda p: len(p["prefix"] or ""), reverse=True)
    return _PrefixedDict(out, all_prefixes)


class _PrefixedDict(dict):
    """Dict с fallback-lookup по prefix'ам. В статике — точное совпадение,
    при miss — ищем самый длинный префикс. Используется только для чтения."""

    def __init__(self, base: dict, prefixes: list[dict]) -> None:
        super().__init__(base)
        self._prefixes = prefixes

    def get(self, key, default=None):  # type: ignore[override]
        v = super().get(key)
        if v is not None:
            return v
        for p in self._prefixes:
            pref = p.get("prefix")
            if pref and key.startswith(pref):
                # Возвращаем копию с проставленным name=key
                spec = dict(p)
                spec["name"] = key
                return spec
        return default


# Совместимость: старая build_setting_index() всё ещё может пригодиться кому-то.
def build_setting_index(mods_dir: Path) -> dict[str, str]:
    specs = build_setting_specs(mods_dir)
    return {name: spec["mod"] for name, spec in specs.items()}


# ---------- Сборка отображения для шаблона ----------

def _detect_rgba(node_value: Any) -> dict | None:
    """Если значение — Dict с ключами {r,g,b}±a и всеми числами в 0..1,
    вернуть {'hex': '#rrggbb', 'alpha': float}. Иначе None.
    Используется для подсовывания color-picker'а вместо «не редактируется»."""
    if not (isinstance(node_value, tuple) and node_value[0] == "dict"):
        return None
    items = node_value[1]
    if not isinstance(items, list):
        return None
    rgba: dict[str, float] = {}
    for k, v in items:
        if k not in ("r", "g", "b", "a"):
            return None
        if not isinstance(v, (int, float)):
            return None
        f = float(v)
        if not (-0.001 <= f <= 1.001):
            return None
        rgba[k] = max(0.0, min(1.0, f))
    if not {"r", "g", "b"} <= set(rgba.keys()):
        return None

    def to8(x: float) -> int:
        return max(0, min(255, int(round(x * 255))))
    hex_s = "#%02x%02x%02x" % (to8(rgba["r"]), to8(rgba["g"]), to8(rgba["b"]))
    return {"hex": hex_s, "alpha": rgba.get("a", 1.0)}


def _value_for_form(node: Any) -> tuple[str, Any]:
    """(kind, значение-для-input). Для booleans возвращаем bool, для int — int."""
    kind = ms.value_kind(node)
    raw = ms.get_value(node)
    if kind == "bool":
        return "bool", bool(raw)
    if kind == "double":
        return "double", raw
    if kind in ("i64", "u64"):
        return kind, raw[1] if isinstance(raw, tuple) else raw
    if kind == "string":
        return "string", raw if raw is not None else ""
    if kind == "dict":
        rgba = _detect_rgba(raw)
        if rgba is not None:
            return "color", rgba
    return kind, repr(raw)


def _mod_of_spec(spec: dict | None) -> str | None:
    return spec["mod"] if spec else None


def collect_view(parsed: dict, specs) -> dict:
    """Превращает parsed → структуру для шаблона. Для каждой настройки прокидываем
    spec (если нашли в settings.lua): default, min, max, allowed_values.
    Группировка: scope → mod (по spec.mod, fallback в «Прочее») → items."""
    out: dict[str, list[dict]] = {}
    for scope, items in parsed["sections"].items():
        groups: dict[str | None, list[dict]] = {}
        for name, node in items:
            spec = specs.get(name)
            mod = _mod_of_spec(spec) or None
            kind, val = _value_for_form(node)
            item: dict = {
                "name": name,
                "kind": kind,
                "value": val,
                "default": None, "min": None, "max": None, "allowed": None,
            }
            if spec:
                item["default"] = spec.get("default")
                item["min"] = spec.get("min")
                item["max"] = spec.get("max")
                item["allowed"] = spec.get("allowed")
                # Если spec явно указывает другой kind (например int-setting,
                # но в файле почему-то double) — оставляем kind из файла
                # как источник правды для PropertyTree, но для UI спец может
                # подсказать виджет. Тут ничего не меняем.
            groups.setdefault(mod, []).append(item)
        ordered: list[dict] = []
        for mod in sorted(groups, key=lambda m: (m is None, (m or "").lower())):
            ordered.append({
                "mod": mod if mod is not None else tr("modconfig.other_mods"),
                "count": len(groups[mod]),
                "items": sorted(groups[mod], key=lambda x: x["name"].lower()),
            })
        out[scope] = ordered
    return out


# ---------- Эндпойнты ----------

VALID_SCOPES = {"startup", "runtime-global", "runtime-per-user"}


def _settings_path(uuid: str) -> Path:
    return server_data.SERVERS_DIR / uuid / "mods" / "mod-settings.dat"


def _load_parsed(uuid: str) -> dict:
    p = _settings_path(uuid)
    if not p.is_file():
        raise HTTPException(404, tr("modconfig.file_missing"))
    try:
        return ms.parse(p.read_bytes())
    except ms.ModSettingsError as e:
        raise HTTPException(500, tr("modconfig.file_parse_failed", detail=e))


def _save_parsed(uuid: str, parsed: dict) -> None:
    p = _settings_path(uuid)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(ms.serialize(parsed))
    tmp.replace(p)


def _coerce(kind: str, raw: str) -> Any:
    """Превратить строку из формы в значение нужного типа PropertyTree."""
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "on", "yes")
    if kind == "double":
        return float(raw)
    if kind == "i64":
        return ("i64", int(raw))
    if kind == "u64":
        v = int(raw)
        if v < 0:
            raise ValueError(tr("modconfig.unsigned_number_negative"))
        return ("u64", v)
    if kind == "string":
        return raw
    if kind == "color":
        # Формат: '#rrggbb' или '#rrggbb:alpha' (alpha 0..1).
        # Возвращаем PropertyTree-Dict {r,g,b,a} с floats в 0..1.
        s = (raw or "").strip()
        alpha = 1.0
        if ":" in s:
            s, _, a = s.partition(":")
            try:
                alpha = max(0.0, min(1.0, float(a)))
            except ValueError:
                raise HTTPException(400, tr("modconfig.transparency_out_of_range"))
        s = s.lstrip("#")
        if len(s) != 6:
            raise HTTPException(400, tr("modconfig.color_format"))
        try:
            r = int(s[0:2], 16) / 255.0
            g = int(s[2:4], 16) / 255.0
            b = int(s[4:6], 16) / 255.0
        except ValueError:
            raise HTTPException(400, tr("modconfig.color_invalid"))
        return ("dict", [("r", r), ("g", g), ("b", b), ("a", alpha)])
    raise HTTPException(400, tr("modconfig.unsupported_edit_type", kind=kind))


def _validate_against_spec(spec: dict | None, kind: str, new_val: Any) -> None:
    """Проверить новое значение против ограничений из settings.lua. spec может
    отсутствовать (мод не отдаёт настройку статически) — тогда не валидируем."""
    if not spec:
        return
    if kind in ("i64", "u64", "double"):
        v = new_val[1] if isinstance(new_val, tuple) else new_val
        mn, mx = spec.get("min"), spec.get("max")
        if mn is not None and v < mn:
            raise HTTPException(400, tr("modconfig.below_minimum", value=v, minimum=mn))
        if mx is not None and v > mx:
            raise HTTPException(400, tr("modconfig.above_maximum", value=v, maximum=mx))
    if kind == "string":
        allowed = spec.get("allowed")
        if allowed and new_val not in allowed:
            raise HTTPException(400, tr("modconfig.value_not_allowed", value=repr(new_val), allowed=allowed))


def _default_to_property_value(spec: dict, kind: str) -> Any:
    """spec.default → значение для PropertyTree того же типа как в файле (kind)."""
    d = spec.get("default")
    if d is None:
        raise ValueError(tr("modconfig.default_missing"))
    if kind == "bool":
        return bool(d)
    if kind == "double":
        return float(d)
    if kind in ("i64", "u64"):
        return (kind, int(d))
    if kind == "string":
        return str(d)
    raise ValueError(tr("modconfig.unsupported_default_type", kind=kind))


@router.post("/{uuid}/modconfig/update")
def modconfig_update(uuid: str,
                     payload: dict = Body(...),
                     db: sqlite3.Connection = Depends(get_db)):
    """JSON-body: {scope, name, kind, value}. value — строка или bool."""
    require_server(db, uuid)
    scope = (payload.get("scope") or "").strip()
    name = (payload.get("name") or "").strip()
    kind = (payload.get("kind") or "").strip()
    raw_value = payload.get("value")
    if scope not in VALID_SCOPES:
        raise HTTPException(400, tr("modconfig.invalid_scope", scopes=sorted(VALID_SCOPES)))
    if not name:
        raise HTTPException(400, tr("modconfig.name_empty"))

    parsed = _load_parsed(uuid)
    items = parsed["sections"].get(scope, [])
    specs = build_setting_specs(server_data.SERVERS_DIR / uuid / "mods")
    spec = specs.get(name)
    for i, (n, node) in enumerate(items):
        if n != name:
            continue
        try:
            new_val = _coerce(kind,
                              str(raw_value) if not isinstance(raw_value, bool)
                              else ("1" if raw_value else "0"))
        except ValueError as e:
            raise HTTPException(400, tr("modconfig.invalid_value", detail=e))
        _validate_against_spec(spec, kind, new_val)
        items[i] = (n, ms.set_value(node, new_val))
        parsed["sections"][scope] = items
        _save_parsed(uuid, parsed)
        return {"ok": True, "scope": scope, "name": name, "kind": kind}
    raise HTTPException(404, tr("modconfig.setting_not_found", scope=scope, name=name))


@router.post("/{uuid}/modconfig/revert")
def modconfig_revert(uuid: str,
                     payload: dict = Body(...),
                     db: sqlite3.Connection = Depends(get_db)):
    """Откатить настройку к default. Если default известен из settings.lua —
    устанавливаем его в файл (UI сразу обновится). Если нет — удаляем запись
    из файла, и Factorio запишет default на следующем старте контейнера."""
    require_server(db, uuid)
    scope = (payload.get("scope") or "").strip()
    name = (payload.get("name") or "").strip()
    if scope not in VALID_SCOPES:
        raise HTTPException(400, tr("modconfig.invalid_scope", scopes=sorted(VALID_SCOPES)))
    parsed = _load_parsed(uuid)
    items = parsed["sections"].get(scope, [])

    # Найдём текущую запись и определим её kind
    found_idx = -1
    cur_kind = ""
    for i, (n, node) in enumerate(items):
        if n == name:
            found_idx = i
            cur_kind = ms.value_kind(node)
            break
    if found_idx < 0:
        raise HTTPException(404, tr("modconfig.setting_not_found", scope=scope, name=name))

    specs = build_setting_specs(server_data.SERVERS_DIR / uuid / "mods")
    spec = specs.get(name)
    if spec and spec.get("default") is not None and cur_kind in ("bool", "double", "i64", "u64", "string"):
        try:
            new_val = _default_to_property_value(spec, cur_kind)
        except ValueError:
            new_val = None
        if new_val is not None:
            items[found_idx] = (name, ms.set_value(items[found_idx][1], new_val))
            parsed["sections"][scope] = items
            _save_parsed(uuid, parsed)
            display = new_val[1] if isinstance(new_val, tuple) else new_val
            return {"ok": True, "kind": "reset", "value": display, "kind_from_file": cur_kind}
    # Default неизвестен — удаляем запись.
    parsed["sections"][scope] = [(n, v) for (n, v) in items if n != name]
    _save_parsed(uuid, parsed)
    return {"ok": True, "kind": "removed"}
