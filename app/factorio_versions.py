"""Factorio release tags used by server creation, updates and notifications."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request

CACHE_TTL = 3600
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class _VersionCache:
    def __init__(self):
        self.snapshot: tuple[float, tuple[str, ...]] = (0.0, ())
        self.lock = threading.Lock()


_cache = _VersionCache()


def cache_info() -> tuple[int, int | None]:
    updated, versions = _cache.snapshot
    return len(versions), int(time.time() - updated) if updated else None


def clear_cache() -> None:
    global _cache
    # An in-flight fetch can finish for its caller without repopulating the new cache.
    _cache = _VersionCache()


def _fetch_dockerhub_tags(limit: int | None = None) -> list[str]:
    """Тянем список тегов factoriotools/factorio с Docker Hub. Берём только
    нумеровные semver-теги X.Y.Z, отбрасываем 'latest'/'stable'/беты/rebuild-теги.
    Возвращаем отсортированный по убыванию список. Стандартная либа, без зависимостей.

    На Docker Hub сейчас ~394 semver-тега (от 0.12.35 до текущего 2.0.x).
    Пагинация — по 100 на страницу; лимитом не режем, чтобы дать выбор всех версий."""
    url = "https://hub.docker.com/v2/repositories/factoriotools/factorio/tags/?page_size=100"
    out: set[str] = set()
    pages = 0
    while url and pages < 20:
        pages += 1
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        for t in data.get("results", []):
            name = t.get("name", "")
            if VERSION_RE.match(name):
                out.add(name)
        url = data.get("next")
    versions = sorted(out, key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    return versions[:limit] if limit else versions


def available_versions() -> list[str]:
    """Cache successful lookups for an hour and let concurrent requests share a refresh."""
    cache = _cache
    with cache.lock:
        updated, versions = cache.snapshot
        if versions and time.time() - updated < CACHE_TTL:
            return list(versions)
        try:
            result = _fetch_dockerhub_tags()
        except Exception:
            result = []
        if not result:
            try:
                from factorio_installer import get_releases
                releases = get_releases()
                for channel in ("experimental", "stable"):
                    version = releases.get(channel, {}).get("headless")
                    if version and version not in result:
                        result.append(version)
            except Exception:
                # Both providers may be offline. The next request can retry.
                pass
        if result:
            cache.snapshot = (time.time(), tuple(result))
        return result
