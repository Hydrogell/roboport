"""Системные настройки панели (часовой пояс и прочее, не привязанное к серверу).

Хранятся в .env, читаются через os.environ. После записи в .env обновляется и
os.environ — текущий процесс подхватывает без рестарта (как уже сделано для
FACTORIO_USERNAME/TOKEN). Доступно из main.py и sampler.py без зависимости
от FastAPI / роутов — чтобы фоновый поллер не тянул роуты с собой.

PANEL_TZ принимает два формата:
  1) IANA-имя зоны: 'Europe/Moscow', 'America/New_York', 'Asia/Tokyo', 'UTC'.
     На Linux работает из коробки (stdlib zoneinfo читает /usr/share/zoneinfo).
     На Windows — только если установлен пакет `tzdata` (`pip install tzdata`).
  2) UTC-offset: '+3', '-5:30', '+0'. Работает везде, но не учитывает DST.

Пробуем сначала IANA (даёт автоматический DST на Linux), потом offset как
fallback. Если оба не парсятся — возвращаем None (система использует
дефолтную локальную зону, панель не падает).
"""
from __future__ import annotations

import os
import re
from datetime import timedelta, timezone, tzinfo


try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    _HAS_ZONEINFO = True
except ImportError:
    # Python <3.9 не имеет zoneinfo. У нас 3.11+, ветка не сработает, но
    # пусть код останется устойчивым — деградируем до offset-only.
    _HAS_ZONEINFO = False
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

# Готовые offset-варианты для UI-datalist. Не ограничение — пользователь
# может вписать любой валидный offset (см. _OFFSET_RE) или IANA-имя.
COMMON_TZ_OFFSETS = (
    "-5", "-3", "+0", "+1", "+2", "+3", "+4", "+5",
    "+6", "+7", "+8", "+9", "+10", "+11", "+12",
)

# Популярные IANA-имена для UI-datalist. Показываем только когда IANA реально
# работает на этом хосте — иначе пользователь введёт, получит ошибку, расстроится.
COMMON_TZ_IANA = (
    "UTC", "Europe/Moscow", "Europe/Kaliningrad", "Europe/Berlin",
    "Europe/London", "Europe/Kiev", "Asia/Yekaterinburg",
    "Asia/Novosibirsk", "Asia/Tokyo", "Asia/Shanghai",
    "America/New_York", "America/Chicago", "America/Los_Angeles",
)

# '+3', '+03', '-5:30', '0', '+5:00', '-12'. Часы 0..14, минуты 0..59.
# Без знака — считаем плюсом.
_OFFSET_RE = re.compile(r"^([+-]?)(\d{1,2})(?::(\d{1,2}))?$")


def has_factorio_credentials() -> bool:
    return bool(os.environ.get("FACTORIO_USERNAME", "").strip()
                and os.environ.get("FACTORIO_TOKEN", "").strip())


def panel_tz_name() -> str:
    """Сырое значение из ENV — пустая строка значит «использовать системную»."""
    return (os.environ.get("PANEL_TZ") or "").strip()


def panel_local_domain() -> str:
    """Локальное mDNS-имя по которому панель видна в LAN. Например
    'roboport.local'. Хранится в .env как PANEL_LOCAL_DOMAIN. Если пусто —
    avahi-daemon использует системный hostname (`<hostname>.local`)."""
    return (os.environ.get("PANEL_LOCAL_DOMAIN") or "").strip()


# Валидация *.local-имени. Должно заканчиваться на .local; перед .local —
# одна DNS-метка (a-z 0-9 - ; не начинается/заканчивается дефисом; до 63 символов).
# RFC 6762 (Multicast DNS): single label под TLD `.local`.
_VALID_LOCAL_DOMAIN_RE = re.compile(
    r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.local$"
)


def is_valid_local_domain(name: str) -> bool:
    """True для имени вида `что-то.local` (одна DNS-метка перед .local).
    Без вложенных доменов: `panel.roboport.local` — невалидно (mDNS не
    поддерживает sub-labels)."""
    name = (name or "").strip().lower()
    if not name or len(name) > 70:  # 63 + .local
        return False
    return bool(_VALID_LOCAL_DOMAIN_RE.match(name))


def _parse_iana(raw: str) -> tzinfo | None:
    """Попробовать как IANA-имя зоны. None если zoneinfo не доступна,
    база зон не находится, или имя не распознано."""
    if not _HAS_ZONEINFO:
        return None
    # Защита: IANA-имена выглядят как 'Area/Name', не похожи на offset
    # (с знаком/двоеточием). Выкидываем заведомо offset-подобные строки,
    # чтобы не платить за исключение в ZoneInfo.
    if not raw or raw[0] in "+-" or raw[0].isdigit():
        return None
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # ValueError — невалидное имя; OSError — нет /usr/share/zoneinfo;
        # ZoneInfoNotFoundError — имя не найдено в базе.
        return None


def _parse_offset(raw: str) -> tzinfo | None:
    m = _OFFSET_RE.match(raw)
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    h = int(m.group(2))
    mm = int(m.group(3) or 0)
    if h > 14 or mm > 59:
        return None  # IANA-максимум сейчас +14 (Pacific/Kiritimati)
    delta = timedelta(hours=h, minutes=mm) * sign
    label = f"UTC{'+' if sign >= 0 else '-'}{h:02d}:{mm:02d}"
    return timezone(delta, name=label)


def panel_tz() -> tzinfo | None:
    """tz для отображения дат и daily-расписаний. None → системная локальная
    (datetime.astimezone() / fromtimestamp без tz). Битый формат деградирует
    к None — без падения, чтобы кривой .env не положил панель.

    Сначала пробуем IANA (даёт DST), потом UTC-offset."""
    raw = panel_tz_name()
    if not raw:
        return None
    return _parse_iana(raw) or _parse_offset(raw)


def is_valid_tz(name: str) -> bool:
    if not name:
        return True  # пустая = «системная», валидно
    return _parse_iana(name) is not None or _parse_offset(name) is not None


def iana_supported() -> bool:
    """True если на этом хосте zoneinfo находит хотя бы 'UTC'. Используется
    UI чтобы показать или скрыть IANA-подсказку. На Linux всегда True;
    на Windows — только если установлен пакет `tzdata`."""
    return _parse_iana("UTC") is not None
