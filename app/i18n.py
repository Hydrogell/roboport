"""File-based UI messages. Run python -m app.i18n --check to validate a translation."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from babel.core import Locale, UnknownLocaleError

LOCALES = Path(__file__).with_name("locales")
COOKIE = "roboport_lang"
_current = ContextVar("roboport_locale", default="ru")
_field = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")
_tag = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")


@dataclass(frozen=True)
class Catalog:
    name: str
    direction: str
    messages: dict[str, str | dict[str, str]]
    version: str


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


@lru_cache(maxsize=1)
def languages() -> dict[str, Catalog]:
    result = {}
    for path in sorted(LOCALES.glob("*.json")):
        data = path.read_bytes()
        try:
            doc = json.loads(data, object_pairs_hook=_unique)
            name = doc["name"]
            direction = doc.get("direction", "ltr")
            messages = doc["messages"]
            if not _tag.fullmatch(path.stem) or not isinstance(name, str) or not name.strip():
                raise ValueError("Invalid language code or native name")
            if direction not in {"ltr", "rtl"} or not isinstance(messages, dict):
                raise ValueError("Invalid direction or messages")
            for key, message in messages.items():
                forms = message if isinstance(message, dict) else {"other": message}
                if "other" not in forms or forms.keys() - {"zero", "one", "two", "few", "many", "other"}:
                    raise ValueError(f"Invalid plural forms for {key}")
                if any(not isinstance(v, str) or not v.strip() for v in forms.values()):
                    raise ValueError(f"Empty or invalid message for {key}")
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
        result[path.stem] = Catalog(name, direction, messages, hashlib.sha256(data).hexdigest()[:12])
    return result


@lru_cache(maxsize=1)
def _codes():
    return {code.lower(): code for code in languages()}


def _match(tag: str) -> str | None:
    tag = tag.lower().replace("_", "-")
    codes = _codes()
    if tag in codes:
        return codes[tag]
    base, *parts = tag.split("-")
    if base == "zh":
        traditional = "hant" in parts or ("hans" not in parts and bool({"tw", "hk", "mo"}.intersection(parts)))
        return codes.get("zh-tw" if traditional else "zh-cn")
    if base == "es":
        latin_america = bool({"419", "ar", "bo", "br", "bz", "cl", "co", "cr", "cu", "do", "ec", "gt",
                              "hn", "mx", "ni", "pa", "pe", "pr", "py", "sv", "us", "uy", "ve"}.intersection(parts))
        return codes.get("es-419" if latin_america else "es-es")
    if base == "pt":
        return codes.get("pt-pt" if {"pt", "ao", "mz", "cv", "gw", "st", "tl"}.intersection(parts) else "pt-br")
    if base in {"nb", "nn"}:
        return codes.get("no")
    while "-" in tag:
        tag = tag.rsplit("-", 1)[0]
        if tag in codes:
            return codes[tag]
    return codes.get(base)


def choose_language(cookie: str | None, accept_language: str) -> str:
    if cookie in languages():
        return cookie
    if not accept_language:
        return "ru"
    choices = []
    excluded = set()
    for item in accept_language[:2048].split(","):
        tag, _, weight = item.strip().partition(";")
        try:
            q = float(weight.strip().removeprefix("q=")) if weight else 1.0
        except ValueError:
            continue
        matched = "en" if tag == "*" else _match(tag)
        if not matched:
            continue
        if 0 < q <= 1:
            choices.append((q, matched))
        elif q == 0:
            excluded.add(matched)
    choices.sort(key=lambda pair: pair[0], reverse=True)
    return next((code for _, code in choices if code not in excluded), "ru" if "en" in excluded else "en")


@contextmanager
def use_language(code: str):
    token = _current.set(code)
    try:
        yield
    finally:
        _current.reset(token)


def current_language() -> str:
    return _current.get()


@lru_cache(maxsize=128)
def cldr_locale(code: str) -> Locale:
    try:
        return Locale.parse(code, sep="-")
    except (ValueError, UnknownLocaleError):
        return Locale("en")


def tr(key: str, /, **values) -> str:
    catalogs = languages()
    text = catalogs[current_language()].messages.get(key)
    if text is None:
        text = catalogs["en"].messages.get(key, key)
    if isinstance(text, dict):
        try:
            category = cldr_locale(current_language()).plural_form(float(values["count"]))
        except (KeyError, ValueError, TypeError, OverflowError):
            category = "other"
        text = text.get(category, text["other"])
    return _field.sub(lambda m: str(values[m[1]]) if m[1] in values else m[0], text)


def check_catalogs() -> list[str]:
    catalogs = languages()
    if "en" not in catalogs or "ru" not in catalogs:
        return ["Required catalogs en and ru must exist"]
    base = catalogs["en"].messages
    errors = []
    for code, catalog in catalogs.items():
        for key in sorted(base.keys() - catalog.messages.keys()):
            errors.append(f"{code}: missing {key}")
        for key in sorted(catalog.messages.keys() - base.keys()):
            errors.append(f"{code}: unknown {key}")
        for key in base.keys() & catalog.messages.keys():
            source = base[key]
            message = catalog.messages[key]
            if isinstance(source, dict) != isinstance(message, dict):
                errors.append(f"{code}: inconsistent plural message {key}")
                continue
            required = set(_field.findall(source["other"] if isinstance(source, dict) else source))
            forms = message.values() if isinstance(message, dict) else [message]
            if any(set(_field.findall(form)) != required for form in forms):
                errors.append(f"{code}: placeholders differ for {key}")
    return errors


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args()
    errors = check_catalogs()
    for error in errors:
        print(error)
    if not errors:
        print(f"{len(languages())} locales, {len(languages()['en'].messages)} messages each")
    raise SystemExit(bool(errors))
