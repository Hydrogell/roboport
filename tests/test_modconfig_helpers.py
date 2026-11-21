"""Тесты для app/routes/modconfig.py — _detect_rgba и _coerce(kind='color').

Чистые helpers: на вход PropertyTree-узлы / строки с формы, на выход —
структура для UI / для записи в .dat. БД и FastAPI не трогаем."""
import pytest
from fastapi import HTTPException

from app.routes import modconfig as mc


# ---------------------------------------------------------------------------
# _detect_rgba — поиск {r,g,b} ± a в Dict-узле
# ---------------------------------------------------------------------------

class TestDetectRgba:
    def test_rgb_only(self):
        node = ("dict", [("r", 1.0), ("g", 0.5), ("b", 0.0)])
        out = mc._detect_rgba(node)
        assert out is not None
        assert out["hex"] == "#ff8000"
        assert out["alpha"] == 1.0

    def test_rgba(self):
        node = ("dict", [
            ("r", 0.40), ("g", 0.62), ("b", 0.40), ("a", 0.5),
        ])
        out = mc._detect_rgba(node)
        assert out is not None
        # 0.40 * 255 = 102.0 → "66"; 0.62 * 255 = 158.1 → "9e"
        assert out["hex"].startswith("#66")
        assert out["alpha"] == 0.5

    def test_int_values_accepted(self):
        # Иногда мод пишет 0/1 как int — должно считываться
        node = ("dict", [("r", 1), ("g", 0), ("b", 0)])
        out = mc._detect_rgba(node)
        assert out is not None
        assert out["hex"] == "#ff0000"

    def test_extra_key_rejected(self):
        # Левый ключ в Dict ⇒ это не RGBA
        node = ("dict", [("r", 1.0), ("g", 0.0), ("b", 0.0), ("size", 5)])
        assert mc._detect_rgba(node) is None

    def test_missing_required_rejected(self):
        # Без 'b' — не RGBA
        node = ("dict", [("r", 1.0), ("g", 0.0)])
        assert mc._detect_rgba(node) is None

    def test_out_of_range_rejected(self):
        # Factorio-цвет в 0..1, не в 0..255
        node = ("dict", [("r", 255), ("g", 0), ("b", 0)])
        assert mc._detect_rgba(node) is None

    def test_non_dict_rejected(self):
        assert mc._detect_rgba(None) is None
        assert mc._detect_rgba(("list", [])) is None
        assert mc._detect_rgba("red") is None

    def test_clamp_near_boundaries(self):
        # Числа с плавающей точкой иногда дают 1.0000001 после round-trip
        node = ("dict", [("r", 1.0001), ("g", -0.0001), ("b", 0.5)])
        out = mc._detect_rgba(node)
        assert out is not None
        assert out["hex"].startswith("#ff00")  # clamp работает


# ---------------------------------------------------------------------------
# _coerce — форма → PropertyTree-значение
# ---------------------------------------------------------------------------

class TestCoerceColor:
    def test_hex_only(self):
        out = mc._coerce("color", "#ff0000")
        # ('dict', [('r', 1.0), ('g', 0.0), ('b', 0.0), ('a', 1.0)])
        assert out[0] == "dict"
        d = dict(out[1])
        assert d["r"] == pytest.approx(1.0)
        assert d["g"] == pytest.approx(0.0)
        assert d["b"] == pytest.approx(0.0)
        assert d["a"] == 1.0

    def test_hex_with_alpha(self):
        out = mc._coerce("color", "#00ff80:0.5")
        d = dict(out[1])
        assert d["r"] == 0.0
        assert d["g"] == pytest.approx(1.0)
        assert d["b"] == pytest.approx(128 / 255)
        assert d["a"] == 0.5

    def test_alpha_clamped(self):
        # Alpha вне 0..1 должна быть зажата
        out_hi = mc._coerce("color", "#000000:5.0")
        out_lo = mc._coerce("color", "#000000:-1.0")
        assert dict(out_hi[1])["a"] == 1.0
        assert dict(out_lo[1])["a"] == 0.0

    def test_no_hash_prefix(self):
        # '#' необязателен
        out = mc._coerce("color", "ff0000")
        assert dict(out[1])["r"] == pytest.approx(1.0)

    def test_invalid_hex_length(self):
        with pytest.raises(HTTPException) as exc:
            mc._coerce("color", "#fff")  # short hex
        assert exc.value.status_code == 400

    def test_invalid_hex_chars(self):
        with pytest.raises(HTTPException) as exc:
            mc._coerce("color", "#ggzzhh")
        assert exc.value.status_code == 400

    def test_invalid_alpha(self):
        with pytest.raises(HTTPException):
            mc._coerce("color", "#ffffff:not-a-number")


class TestCoerceUnchanged:
    """Регрессионные проверки — старые kind'ы не должны были измениться."""

    def test_bool(self):
        assert mc._coerce("bool", "true") is True
        assert mc._coerce("bool", "off") is False

    def test_double(self):
        assert mc._coerce("double", "3.14") == 3.14

    def test_i64(self):
        assert mc._coerce("i64", "-42") == ("i64", -42)

    def test_u64_negative_rejected(self):
        with pytest.raises(ValueError):
            mc._coerce("u64", "-1")

    def test_string(self):
        assert mc._coerce("string", "hello") == "hello"

    def test_unknown_kind(self):
        with pytest.raises(HTTPException):
            mc._coerce("list", "[]")
