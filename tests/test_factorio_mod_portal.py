"""Тесты для factorio_mod_portal.py: парсер версий, парсер deps, satisfies."""
from pathlib import Path

import pytest

import factorio_mod_portal as fmp


def test_cli_sync_keeps_previous_catalog_if_replacement_fails(tmp_path, monkeypatch):
    cache = tmp_path / "mods_cache.json"
    cache.write_text('{"mods": []}', encoding="utf8")
    monkeypatch.setattr(fmp, "CACHE", cache)
    monkeypatch.setattr(fmp, "http_get_json", lambda url: {"results": [{"name": "demo"}]})

    def busy(source, destination):
        assert destination == cache
        raise PermissionError("busy")

    monkeypatch.setattr(Path, "replace", busy)
    with pytest.raises(PermissionError, match="busy"):
        fmp.cmd_sync()
    assert cache.read_text(encoding="utf8") == '{"mods": []}'
    assert list(tmp_path.iterdir()) == [cache]


# ---------------------------------------------------------------------------
# parse_version — '2.0.16' → (2, 0, 16)
# ---------------------------------------------------------------------------

class TestParseVersion:
    def test_three_part(self):
        assert fmp.parse_version("2.0.16") == (2, 0, 16)

    def test_two_part(self):
        assert fmp.parse_version("1.1") == (1, 1)

    def test_single_number(self):
        assert fmp.parse_version("5") == (5,)

    def test_garbage_returns_zero_tuple(self):
        # Битый input → (0,) — сравнение с нормальной версией всегда «меньше»
        assert fmp.parse_version("not-a-version") == (0,)
        assert fmp.parse_version("") == (0,)
        assert fmp.parse_version(None) == (0,)

    def test_tuples_compare_correctly(self):
        # Регрессия 2.0.10 vs 2.0.9 — string-сравнение давало "2.0.10" < "2.0.9".
        # Параллельно tuples должны сравнивать корректно.
        assert fmp.parse_version("2.0.10") > fmp.parse_version("2.0.9")
        assert fmp.parse_version("2.0.16") > fmp.parse_version("2.0.5")
        assert fmp.parse_version("1.1.105") < fmp.parse_version("2.0.0")


# ---------------------------------------------------------------------------
# _version_satisfies — совмещаем installed-версию с required + op
# ---------------------------------------------------------------------------

class TestVersionSatisfies:
    @pytest.mark.parametrize("installed,op,required,expected", [
        # Точное равенство
        ("1.0.0", "=", "1.0.0", True),
        ("1.0.0", "=", "1.0.1", False),
        # Больше или равно
        ("2.0.16", ">=", "2.0.0", True),
        ("2.0.0", ">=", "2.0.16", False),
        ("2.0.0", ">=", "2.0.0", True),
        # Меньше или равно
        ("1.0.5", "<=", "2.0.0", True),
        ("2.0.5", "<=", "1.0.0", False),
        # Строгие
        ("2.0.0", ">", "1.0.0", True),
        ("1.0.0", ">", "1.0.0", False),
        ("1.0.0", "<", "2.0.0", True),
        ("2.0.0", "<", "1.0.0", False),
        # Регрессия 2.0.10 vs 2.0.9
        ("2.0.10", ">", "2.0.9", True),
        ("2.0.9", ">", "2.0.10", False),
    ])
    def test_op_combinations(self, installed, op, required, expected):
        assert fmp._version_satisfies(installed, op, required) is expected

    def test_no_op_means_anything_satisfies(self):
        # Отсутствие оператора в декларации dep'а → принимаем любую версию
        assert fmp._version_satisfies("0.0.1", "", "")
        assert fmp._version_satisfies("999.0.0", None, None)


# ---------------------------------------------------------------------------
# parse_dep — парсер mod-зависимостей: '! conflict', '? optional', etc
# ---------------------------------------------------------------------------

class TestParseDep:
    def test_required_simple(self):
        d = fmp.parse_dep("base")
        assert d["kind"] == "required"
        assert d["name"] == "base"
        assert d["op"] is None
        assert d["version"] is None

    def test_required_with_version(self):
        d = fmp.parse_dep("base >= 2.0.0")
        assert d["kind"] == "required"
        assert d["name"] == "base"
        assert d["op"] == ">="
        assert d["version"] == "2.0.0"

    def test_optional(self):
        d = fmp.parse_dep("? cool-mod")
        assert d["kind"] == "optional"
        assert d["name"] == "cool-mod"

    def test_hidden_optional(self):
        d = fmp.parse_dep("(?) hidden-mod")
        assert d["kind"] == "hidden_optional"
        assert d["name"] == "hidden-mod"

    def test_conflict(self):
        # `!` префикс = conflict, мод НЕ должен стоять одновременно
        d = fmp.parse_dep("! incompatible-mod")
        assert d["kind"] == "conflict"
        assert d["name"] == "incompatible-mod"

    def test_no_load_order(self):
        d = fmp.parse_dep("~ unrelated-mod")
        assert d["kind"] == "no_load_order"
        assert d["name"] == "unrelated-mod"

    def test_unknown_format_does_not_raise(self):
        # Битый dep не должен класть весь парс mod-info — возвращается raw
        d = fmp.parse_dep("$$ broken")
        assert d["kind"] == "unknown"
        assert d["raw"] == "$$ broken"

    def test_strict_equality_op(self):
        d = fmp.parse_dep("space-age = 2.0.0")
        assert d["op"] == "="
        assert d["version"] == "2.0.0"
