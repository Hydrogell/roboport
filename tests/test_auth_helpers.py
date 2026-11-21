"""Тесты для чистых helpers из app/auth.py: маскирование ключей, scope-парсер,
формат ключей, scrypt round-trip, recovery-token нормализация."""
import pytest

from app import auth


# ---------------------------------------------------------------------------
# mask_api_key — маска для логов: rpk_AbCd…wXyZ
# ---------------------------------------------------------------------------

class TestMaskApiKey:
    def test_typical_key_returns_prefix_dots_suffix(self):
        # 32-символьный body — типичный сгенерированный ключ
        key = "rpk_" + "A" * 4 + "B" * 24 + "Z" * 4
        assert auth.mask_api_key(key) == "rpk_AAAA…ZZZZ"

    def test_none_returns_placeholder(self):
        assert auth.mask_api_key(None) == "<no-key>"

    def test_empty_string_returns_placeholder(self):
        assert auth.mask_api_key("") == "<no-key>"

    def test_wrong_prefix_is_masked_completely(self):
        # Чтобы из лога нельзя было восстановить даже хвост чужого формата
        assert auth.mask_api_key("fpk_legacy_key_format") == "<bad-fmt>"
        assert auth.mask_api_key("Bearer xxx") == "<bad-fmt>"

    def test_short_body_uses_short_marker(self):
        assert auth.mask_api_key("rpk_abc") == "rpk_<short>"

    def test_does_not_leak_middle_of_key(self):
        # Регрессия: убедимся что middle не попадает в маску
        secret_middle = "MIDDLESECRETBYTES"
        key = f"rpk_AAAA{secret_middle}ZZZZ"
        masked = auth.mask_api_key(key)
        assert secret_middle not in masked


# ---------------------------------------------------------------------------
# parse_scopes / format_scopes — roundtrip через CSV
# ---------------------------------------------------------------------------

class TestScopes:
    def test_parse_typical(self):
        assert auth.parse_scopes("read,command") == {"read", "command"}

    def test_parse_strips_whitespace(self):
        assert auth.parse_scopes(" read , command ") == {"read", "command"}

    def test_parse_dedups(self):
        assert auth.parse_scopes("read,read,command") == {"read", "command"}

    def test_parse_unknown_scopes_dropped(self):
        # Безопасное поведение: неизвестное name не повышает privileges
        assert auth.parse_scopes("read,sudo,owner") == {"read"}

    def test_parse_empty(self):
        assert auth.parse_scopes("") == set()
        assert auth.parse_scopes(None) == set()

    def test_format_is_alphabetical(self):
        # Детерминированный порядок — нужен для diff'ов в audit-логах
        assert auth.format_scopes({"command", "admin", "read"}) == "admin,command,read"

    def test_format_filters_unknown(self):
        # Mirror parse: unknown не попадают в БД даже если дать их в format
        assert auth.format_scopes({"read", "owner"}) == "read"

    def test_format_empty(self):
        assert auth.format_scopes(set()) == ""

    def test_roundtrip(self):
        original = {"read", "control", "admin"}
        assert auth.parse_scopes(auth.format_scopes(original)) == original


# ---------------------------------------------------------------------------
# generate_api_key_plain / hash_api_key — формат и детерминизм sha256
# ---------------------------------------------------------------------------

class TestApiKeyGeneration:
    def test_starts_with_prefix(self):
        key = auth.generate_api_key_plain()
        assert key.startswith(auth.API_KEY_PREFIX)

    def test_unique_each_call(self):
        # 100 ключей — все разные. Если два совпали — сломан secrets/CSPRNG
        keys = {auth.generate_api_key_plain() for _ in range(100)}
        assert len(keys) == 100

    def test_body_length_around_32(self):
        # 24 байта в base64url ≈ 32 символа. Допускаем ±2 на padding-stripping.
        body = auth.generate_api_key_plain()[len(auth.API_KEY_PREFIX):]
        assert 30 <= len(body) <= 34

    def test_hash_is_64_hex_chars(self):
        # sha256-hex
        h = auth.hash_api_key("rpk_anything")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_is_deterministic(self):
        assert auth.hash_api_key("rpk_x") == auth.hash_api_key("rpk_x")

    def test_hash_differs_for_different_keys(self):
        assert auth.hash_api_key("rpk_a") != auth.hash_api_key("rpk_b")


# ---------------------------------------------------------------------------
# hash_password / verify_password — scrypt round-trip
# ---------------------------------------------------------------------------

class TestPasswordHash:
    def test_roundtrip_correct_password(self):
        h = auth.hash_password("correct horse battery staple")
        assert auth.verify_password("correct horse battery staple", h)

    def test_wrong_password_rejected(self):
        h = auth.hash_password("correct horse battery staple")
        assert not auth.verify_password("Tr0ub4dor&3", h)

    def test_two_hashes_differ_due_to_salt(self):
        # Соль случайная — два хэша одного пароля должны отличаться
        h1 = auth.hash_password("password123")
        h2 = auth.hash_password("password123")
        assert h1 != h2
        # …но оба валидны для исходного пароля
        assert auth.verify_password("password123", h1)
        assert auth.verify_password("password123", h2)

    def test_verify_rejects_garbage_stored(self):
        # Битый формат не должен ронять процесс — должен вернуть False
        assert not auth.verify_password("any", "")
        assert not auth.verify_password("any", "not-scrypt-format")
        assert not auth.verify_password("any", "scrypt$broken")

    def test_too_short_password_raises(self):
        with pytest.raises(auth.AuthError):
            auth.hash_password("")


# ---------------------------------------------------------------------------
# format_recovery_token_for_display — К7М3 → K7M3-X9P2
# ---------------------------------------------------------------------------

class TestRecoveryTokenDisplay:
    def test_8_char_gets_dash_in_middle(self):
        assert auth.format_recovery_token_for_display("K7M3X9P2") == "K7M3-X9P2"

    def test_already_lowercase_is_uppercased(self):
        assert auth.format_recovery_token_for_display("k7m3x9p2") == "K7M3-X9P2"

    def test_non_8_char_returned_uppercase_no_dash(self):
        # Edge — если длина не 8, не пытаемся ставить дефис не на том месте
        assert auth.format_recovery_token_for_display("ABC") == "ABC"
