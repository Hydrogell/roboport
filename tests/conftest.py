"""pytest конфигурация. Всё что нужно сейчас — добавить repo-root в sys.path,
чтобы тесты могли импортировать `factorio_mod_portal` (top-level модуль рядом
с `app/`). Сам пакет `app` pytest обнаруживает автоматически."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
