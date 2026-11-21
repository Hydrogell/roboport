"""Codec для Factorio mod-settings.dat (PropertyTree-формат).

Формат (см. https://wiki.factorio.com/Property_tree):
  Header (9 байт):
    u8 major, u8 minor, u8 patch, u8 dev, u32 build, u8 quality
    (для современных версий — наблюдалось 02 00 00 00 4c 00 00 00 00)
  Root: PropertyTree, тип Dictionary.

PropertyTree node:
  u8 type      — 0=None, 1=Bool, 2=Number(double), 3=String, 4=List, 5=Dict,
                 6=SignedInteger (i64), 7=UnsignedInteger (u64) — последние два
                 добавлены в Factorio 2.0.
  u8 any_type  — флаг (обычно 0; для нас прозрачный)
  body:
    None:        ничего
    Bool:        u8
    Number:      double little-endian (8 байт)
    String:      u8 empty_flag; если не empty — SpaceOptimisedUInt длина + UTF-8 байты
    List/Dict:   u32 count, затем count раз: String key + PropertyTree value
                 (List по сути словарь с пустыми ключами — мы храним и ключи, и значения,
                 чтобы round-trip был байт-в-байт, но в API раскрываем как list-of-values).

SpaceOptimisedUInt:
  u8 v; если v == 0xFF — следом u32. Иначе число = v.

mod-settings.dat — root Dict со ключами 'startup', 'runtime-global', 'runtime-per-user';
каждое значение — Dict, где ключи = имена настроек, значения = Dict {'value': ...} (и
иногда дополнительные ключи для квалити/расширений).
"""
from __future__ import annotations

import struct
from typing import Any
from .i18n import tr


class ModSettingsError(RuntimeError):
    pass


# ---------- Низкоуровневые primitives ----------

def _read_uint_so(buf: bytes, off: int) -> tuple[int, int]:
    n = buf[off]
    off += 1
    if n == 0xFF:
        n = struct.unpack_from("<I", buf, off)[0]
        off += 4
    return n, off


def _write_uint_so(out: bytearray, n: int) -> None:
    if n < 0xFF:
        out.append(n)
    else:
        out.append(0xFF)
        out += struct.pack("<I", n)


def _read_string(buf: bytes, off: int) -> tuple[str | None, int]:
    empty = buf[off]
    off += 1
    if empty:
        return None, off
    n, off = _read_uint_so(buf, off)
    s = buf[off:off + n].decode("utf-8")
    off += n
    return s, off


def _write_string(out: bytearray, s: str | None) -> None:
    if s is None:
        out.append(1)
        return
    b = s.encode("utf-8")
    out.append(0)
    _write_uint_so(out, len(b))
    out += b


# ---------- Узлы PropertyTree ----------
#
# Внутреннее представление:
#   None        → None
#   Bool        → bool
#   Number      → float
#   String      → str
#   List        → ('list', [(key_or_None, child_node), ...])
#   Dict        → ('dict', [(key, child_node), ...])  — list of pairs, чтобы сохранить порядок
#
# any_type-флаги мы НЕ храним (все наблюдаемые файлы пишут 0); при кодировании всегда
# выставляем 0, что на практике не отличается.


def _read_value(buf: bytes, off: int) -> tuple[Any, int]:
    ptype = buf[off]
    off += 1
    _any = buf[off]  # noqa: F841
    off += 1
    if ptype == 0:
        return None, off
    if ptype == 1:
        return bool(buf[off]), off + 1
    if ptype == 2:
        v = struct.unpack_from("<d", buf, off)[0]
        return v, off + 8
    if ptype == 3:
        s, off = _read_string(buf, off)
        return s if s is not None else "", off
    if ptype == 4 or ptype == 5:
        n = struct.unpack_from("<I", buf, off)[0]
        off += 4
        items: list[tuple[Any, Any]] = []
        for _ in range(n):
            key, off = _read_string(buf, off)
            val, off = _read_value(buf, off)
            items.append((key, val))
        kind = "list" if ptype == 4 else "dict"
        return (kind, items), off
    if ptype == 6:
        v = struct.unpack_from("<q", buf, off)[0]
        return ("i64", v), off + 8
    if ptype == 7:
        v = struct.unpack_from("<Q", buf, off)[0]
        return ("u64", v), off + 8
    raise ModSettingsError(tr("mod_settings.property_tree_type_invalid", type=ptype, offset=off-2))


def _write_value(out: bytearray, v: Any) -> None:
    if v is None:
        out += b"\x00\x00"
        return
    if isinstance(v, bool):
        out += b"\x01\x00"
        out.append(1 if v else 0)
        return
    if isinstance(v, (int, float)):
        out += b"\x02\x00"
        out += struct.pack("<d", float(v))
        return
    if isinstance(v, str):
        out += b"\x03\x00"
        _write_string(out, v)
        return
    if isinstance(v, tuple) and len(v) == 2 and v[0] in ("list", "dict"):
        kind, items = v
        out.append(4 if kind == "list" else 5)
        out.append(0)
        out += struct.pack("<I", len(items))
        for k, child in items:
            _write_string(out, k)
            _write_value(out, child)
        return
    if isinstance(v, tuple) and len(v) == 2 and v[0] == "i64":
        out += b"\x06\x00"
        out += struct.pack("<q", int(v[1]))
        return
    if isinstance(v, tuple) and len(v) == 2 and v[0] == "u64":
        out += b"\x07\x00"
        out += struct.pack("<Q", int(v[1]))
        return
    raise ModSettingsError(tr("mod_settings.property_tree_value_invalid", type=type(v).__name__, value=repr(v)))


# ---------- Файл целиком ----------

HEADER_LEN = 9


def parse(data: bytes) -> dict:
    """Развернуть mod-settings.dat в удобный dict:
    {
      'header': bytes (9 байт; чтобы вернуть как было при записи),
      'sections': {
        'startup':           [(name, raw_inner_dict), ...],
        'runtime-global':    [...],
        'runtime-per-user':  [...],
      },
    }
    raw_inner_dict — это исходный ('dict', items) узел, в котором обычно есть ключ 'value'.
    Так мы сохраняем порядок и любые дополнительные ключи/типы, не предполагая структуру.
    """
    if len(data) < HEADER_LEN + 4:
        raise ModSettingsError(tr("mod_settings.file_too_short"))
    header = data[:HEADER_LEN]
    root, off = _read_value(data, HEADER_LEN)
    if not (isinstance(root, tuple) and root[0] == "dict"):
        raise ModSettingsError(tr("mod_settings.invalid_root"))
    sections: dict[str, list[tuple[str, Any]]] = {}
    for key, val in root[1]:
        if isinstance(val, tuple) and val[0] == "dict":
            sections[key] = list(val[1])
        else:
            sections[key] = []  # неожиданно, но не валим
    return {"header": header, "sections": sections}


def serialize(parsed: dict) -> bytes:
    """Обратное к parse(). Записывает байты для mod-settings.dat."""
    out = bytearray()
    out += parsed["header"]
    section_items: list[tuple[str, Any]] = []
    for sect_name, settings in parsed["sections"].items():
        section_items.append((sect_name, ("dict", list(settings))))
    _write_value(out, ("dict", section_items))
    return bytes(out)


# ---------- Удобные хелперы для одного значения ----------

def get_value(node: Any) -> Any:
    """Из inner-dict-узла настройки достать поле 'value' (число/строка/bool).
    Возвращает None если 'value' отсутствует или нет внутреннего dict."""
    if not (isinstance(node, tuple) and node[0] == "dict"):
        return None
    for k, v in node[1]:
        if k == "value":
            return v
    return None


def value_kind(node: Any) -> str:
    """Какого типа value лежит в настройке. Возвращает один из:
    'bool' | 'double' | 'i64' | 'u64' | 'string' | 'list' | 'dict' | 'none' | 'unknown'."""
    v = get_value(node)
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, float):
        return "double"
    if isinstance(v, str):
        return "string"
    if isinstance(v, tuple) and len(v) == 2:
        return v[0]  # 'i64' / 'u64' / 'list' / 'dict'
    return "unknown"


def set_value(node: Any, new_value: Any) -> Any:
    """Заменить поле 'value' внутри inner-dict-узла настройки. Если поля не было — добавим.
    Тип нового значения должен совпадать с существующим (чтобы Factorio не упала на чтении)."""
    if not (isinstance(node, tuple) and node[0] == "dict"):
        raise ModSettingsError(tr("mod_settings.dictionary_node_required"))
    items = list(node[1])
    for i, (k, _v) in enumerate(items):
        if k == "value":
            items[i] = (k, new_value)
            return ("dict", items)
    items.append(("value", new_value))
    return ("dict", items)
