#!/usr/bin/env bash
# Smoke-тесты против работающей панели. Бьём curl'ом по ключевым эндпоинтам
# и проверяем коды. Не лезем в авторизованные роуты — проверяем только что
# бэк отвечает и middleware-цепочка собрана правильно.
#
# Usage:
#   bash deploy/smoke.sh                              # против https://roboport.local
#   SMOKE_BASE=https://<your-server-ip> bash smoke.sh # против IP
#   SMOKE_BASE=http://localhost:8000 bash smoke.sh    # против локального dev'а

set -uo pipefail   # без -e: проверки сами решают что делать на fail

BASE="${SMOKE_BASE:-https://roboport.local}"
CURL=(curl -ksS --max-time 5)

fail=0
pass=0

# $1 = name, $2 = expected-status, $3 = path, [@:4 = extra curl args ARRAY]
check_status() {
    local name="$1" expected="$2" path="$3"
    shift 3
    local actual
    actual=$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$@" "$BASE$path" 2>/dev/null || echo "000")
    if [ "$actual" = "$expected" ]; then
        printf "  ✅ %-40s → %s\n" "$name" "$actual"
        pass=$((pass + 1))
    else
        printf "  ❌ %-40s → %s (ожидался %s)\n" "$name" "$actual" "$expected"
        fail=$((fail + 1))
    fi
}

# $1 = name, $2 = expected-substring, $3 = path
check_body() {
    local name="$1" expected="$2" path="$3"
    local body
    body=$("${CURL[@]}" "$BASE$path" 2>/dev/null || echo "")
    if [[ "$body" == *"$expected"* ]]; then
        printf "  ✅ %-40s → contains %q\n" "$name" "$expected"
        pass=$((pass + 1))
    else
        printf "  ❌ %-40s → body не содержит %q\n" "$name" "$expected"
        printf "         (got: %s)\n" "${body:0:120}"
        fail=$((fail + 1))
    fi
}

echo "[smoke] base=$BASE"

# --- публичные эндпоинты -----------------------------------------------------
check_status  "/api/v1/health"            200 "/api/v1/health"
check_body    "/api/v1/health body"       '"ok":true' "/api/v1/health"
check_status  "/login (GET form)"         200 "/login"

# --- default-deny на залогиненные --------------------------------------------
# /settings без cookie → 303 на /login
check_status  "/settings (без auth)"      303 "/settings" --max-redirs 0
# /servers тоже
check_status  "/servers (без auth)"       303 "/servers" --max-redirs 0
# / (dashboard) тоже
check_status  "/ (без auth)"              303 "/" --max-redirs 0

# --- API без ключа -----------------------------------------------------------
check_status  "/api/v1/servers (без key)" 401 "/api/v1/servers"
check_status  "/api/v1/audit (без key)"   401 "/api/v1/audit"

# --- Bogus key short-circuit -------------------------------------------------
check_status  "API с фальшивым ключом"    401 "/api/v1/servers" -H "Authorization: Bearer bogus"

# --- статика отдаётся --------------------------------------------------------
check_status  "/static/css/style.css"     200 "/static/css/style.css"

# --- security-headers --------------------------------------------------------
hdrs=$("${CURL[@]}" -o /dev/null -D - "$BASE/login" 2>/dev/null || echo "")
if echo "$hdrs" | grep -qi "^content-security-policy:"; then
    echo "  ✅ CSP-заголовок присутствует"
    pass=$((pass + 1))
else
    echo "  ❌ CSP-заголовок отсутствует на /login"
    fail=$((fail + 1))
fi
if echo "$hdrs" | grep -qi "^x-frame-options:.*deny"; then
    echo "  ✅ X-Frame-Options: DENY"
    pass=$((pass + 1))
else
    echo "  ❌ X-Frame-Options не DENY"
    fail=$((fail + 1))
fi

# --- итог --------------------------------------------------------------------
echo ""
if [ "$fail" -eq 0 ]; then
    echo "[smoke] ✅ all $pass checks passed"
    exit 0
fi
echo "[smoke] ❌ $fail failed, $pass passed"
exit 1
