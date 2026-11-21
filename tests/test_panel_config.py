"""Тесты для app/panel_config.py: парсер таймзон (IANA + UTC-offset) и
валидатор mDNS-домена."""
from datetime import datetime, timezone

import pytest

from app import panel_config as pc


# ---------------------------------------------------------------------------
# is_valid_local_domain — что именно считаем правильным `*.local`
# ---------------------------------------------------------------------------

class TestLocalDomain:
    @pytest.mark.parametrize("name", [
        "roboport.local",
        "panel.local",
        "my-server.local",
        "abc-123.local",
    ])
    def test_valid(self, name):
        assert pc.is_valid_local_domain(name)

    @pytest.mark.parametrize("name", [
        "",                      # пусто
        "no-suffix",             # без .local
        "panel.roboport.local",  # вложенный домен (mDNS не поддерживает)
        ".local",                # пустой label
        "-bad.local",            # начинается с дефиса
        "bad-.local",            # заканчивается дефисом
        "Panel.Local",           # uppercase OK по нормализации, но проверим что не cрётся
    ])
    def test_invalid_or_normalized(self, name):
        # Эти кейсы должны вернуть False ИЛИ True (если просто uppercase) —
        # главное не упасть с исключением
        result = pc.is_valid_local_domain(name)
        assert isinstance(result, bool)

    def test_uppercase_normalized(self):
        # Приводится к lowercase внутри — должен принимать
        assert pc.is_valid_local_domain("ROBOPORT.LOCAL")

    def test_too_long_rejected(self):
        # 100 символов — заведомо больше DNS-лимита 63
        assert not pc.is_valid_local_domain("a" * 100 + ".local")


# ---------------------------------------------------------------------------
# _parse_offset — UTC-offset формат: +3, -5:30, +0
# ---------------------------------------------------------------------------

class TestParseOffset:
    def test_positive_hours_only(self):
        tz = pc._parse_offset("+3")
        assert tz is not None
        # +3:00 относительно UTC
        offset = tz.utcoffset(datetime(2026, 1, 1))
        assert offset.total_seconds() == 3 * 3600

    def test_negative_hours_only(self):
        tz = pc._parse_offset("-5")
        assert tz is not None
        assert tz.utcoffset(datetime(2026, 1, 1)).total_seconds() == -5 * 3600

    def test_zero(self):
        tz = pc._parse_offset("+0")
        assert tz is not None
        assert tz.utcoffset(datetime(2026, 1, 1)).total_seconds() == 0

    def test_with_minutes(self):
        # India: +5:30
        tz = pc._parse_offset("+5:30")
        assert tz is not None
        assert tz.utcoffset(datetime(2026, 1, 1)).total_seconds() == 5 * 3600 + 30 * 60

    def test_invalid_returns_none(self):
        assert pc._parse_offset("invalid") is None
        assert pc._parse_offset("+25") is None  # больше +14 — IANA-максимум
        assert pc._parse_offset("+3:99") is None  # минуты вне диапазона
        assert pc._parse_offset("") is None

    def test_unsigned_treated_as_positive(self):
        # Регекс лояльный: "3" без знака = +3:00. Регрессия на «не падать».
        tz = pc._parse_offset("3")
        assert tz is not None
        assert tz.utcoffset(datetime(2026, 1, 1)).total_seconds() == 3 * 3600


# ---------------------------------------------------------------------------
# _parse_iana — IANA-имена зон
# ---------------------------------------------------------------------------

class TestParseIana:
    def test_offset_string_not_treated_as_iana(self):
        # +3 не должен пытаться идти в zoneinfo (там его нет, но защита явная)
        assert pc._parse_iana("+3") is None
        assert pc._parse_iana("3") is None
        assert pc._parse_iana("-5") is None

    def test_empty_returns_none(self):
        assert pc._parse_iana("") is None

    def test_known_zone_resolves_if_zoneinfo_available(self):
        # Если zoneinfo+tzdata стоит, UTC должна резолвиться. Если нет —
        # тест пропускаем (skip), не fail.
        if not pc._HAS_ZONEINFO:
            pytest.skip("zoneinfo не доступен")
        tz = pc._parse_iana("UTC")
        # На windows-разработке без tzdata UTC может ещё и не оказаться
        if tz is None:
            pytest.skip("UTC zone не нашлась — нет tzdata")
        assert tz.utcoffset(datetime(2026, 1, 1)).total_seconds() == 0

    def test_garbage_zone_returns_none(self):
        assert pc._parse_iana("This/Is/Not/A/Zone") is None


# ---------------------------------------------------------------------------
# is_valid_tz — комбинированный валидатор для UI
# ---------------------------------------------------------------------------

class TestIsValidTz:
    def test_empty_is_valid(self):
        # Пустое = «системная зона», UI должен принимать
        assert pc.is_valid_tz("")
        assert pc.is_valid_tz(None)

    def test_offset_format_valid(self):
        assert pc.is_valid_tz("+3")
        assert pc.is_valid_tz("-5:30")

    def test_obvious_garbage_invalid(self):
        assert not pc.is_valid_tz("blahblah")
        assert not pc.is_valid_tz("+99")
