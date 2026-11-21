"""Roboport Controller — Factorio server control panel.

Автор: Gellou (https://github.com/Hydrogell)
Лицензия: MIT — см. LICENSE.

Версия читается из корневого файла VERSION.
Сюда же попадает в panel_updates таблицу как from/to_version при self-update.
"""
from pathlib import Path

__author__ = "Gellou"
GITHUB_REPOSITORY = "Hydrogell/roboport"
__url__ = f"https://github.com/{GITHUB_REPOSITORY}"
__license__ = "MIT"

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
try:
    __version__ = _VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0-unknown"
except OSError:
    __version__ = "0.0.0-unknown"
