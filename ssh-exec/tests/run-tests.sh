#!/usr/bin/env bash
# run-tests.sh - Automated test runner for ssh-exec skill
# Usage: bash tests/run-tests.sh [--quick]
#   --quick   Skip network-dependent tests (Group 2-4), run validation only
#
# This script mirrors test-spec.md. After each test run, update the
# "最后结果" and "最后测试日期" columns in test-spec.md.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL="$SCRIPT_DIR/../scripts/ssh-exec.sh"
COLOR_RED='\033[0;31m'
COLOR_GREEN='\033[0;32m'
COLOR_YELLOW='\033[1;33m'
COLOR_NC='\033[0m'

# --- Test target configuration ---
# Set these environment variables before running:
#   TEST_SERVER TEST_PORT TEST_USER TEST_KEY_PATH TEST_PASSWORD TEST_SOCKS_PROXY TEST_HTTP_PROXY
SERVER="${TEST_SERVER:-}"
PORT="${TEST_PORT:-22}"
USER="${TEST_USER:-root}"
KEY_PATH="${TEST_KEY_PATH:-}"
PASSWORD="${TEST_PASSWORD:-}"
SOCKS_PROXY="${TEST_SOCKS_PROXY:-}"
HTTP_PROXY="${TEST_HTTP_PROXY:-}"

QUICK_MODE=false
[[ "${1:-}" == "--quick" ]] && QUICK_MODE=true

PASS=0
FAIL=0
SKIP=0
declare -a FAILED_TESTS

pass() { echo -e "  ${COLOR_GREEN}✅ PASS${COLOR_NC}"; ((PASS+=1)); }
fail() {
    echo -e "  ${COLOR_RED}❌ FAIL${COLOR_NC} (expected: $1, got: $2)"
    ((FAIL+=1))
    FAILED_TESTS+=("$3")
}
skip() { echo -e "  ${COLOR_YELLOW}⏭ SKIP${COLOR_NC} (reason: $1)"; ((SKIP+=1)); }
run() {
    local exit_code=0
    bash "$SKILL" "$@" 2>&1 || exit_code=$?
    echo "$exit_code"
}

echo "============================================"
echo " ssh-exec Test Runner"
echo " Target: ${SERVER}:${PORT}"
echo " Mode: $([ "$QUICK_MODE" = true ] && echo "quick (validation only)" || echo "full")"
echo "============================================"
echo ""

# ============================================================
# Group 1: Parameter Validation (no network needed)
# ============================================================
echo "=== Group 1: Parameter Validation ==="

# T1.1
echo -n "T1.1  缺少 -s ..."
output=$(run -u root -c "hostname" -P "test")
exit_code=${output##*$'\n'}
output=${output%$'\n'*}
if [[ "$exit_code" != "0" ]] && echo "$output" | grep -q "Error: Missing required parameters"; then
    pass
else
    fail "EXIT != 0 + 'Missing required parameters'" "EXIT=$exit_code" "T1.1"
fi

# T1.2
echo -n "T1.2  缺少 -u ..."
output=$(run -s 10.0.0.1 -c "hostname" -P "test")
exit_code=${output##*$'\n'}
if [[ "$exit_code" != "0" ]]; then
    pass
else
    fail "EXIT != 0" "EXIT=$exit_code" "T1.2"
fi

# T1.3
echo -n "T1.3  缺少 -c ..."
output=$(run -s 10.0.0.1 -u root -P "test")
exit_code=${output##*$'\n'}
if [[ "$exit_code" != "0" ]]; then
    pass
else
    fail "EXIT != 0" "EXIT=$exit_code" "T1.3"
fi

# T1.4
echo -n "T1.4  -s 无值 ..."
output=$(run -s -u root -c "hostname" -P "test")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Error: -s requires a value"; then
    pass
else
    fail "-s requires a value + EXIT=1" "EXIT=$exit_code" "T1.4"
fi

# T1.5
echo -n "T1.5  -u 无值 ..."
output=$(run -s 10.0.0.1 -u -c "hostname" -P "test")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Error: -u requires a value"; then
    pass
else
    fail "-u requires a value + EXIT=1" "EXIT=$exit_code" "T1.5"
fi

# T1.6
echo -n "T1.6  -c 无值 ..."
output=$(run -s 10.0.0.1 -u root -c -P "test")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Error: -c requires a value"; then
    pass
else
    fail "-c requires a value + EXIT=1" "EXIT=$exit_code" "T1.6"
fi

# T1.7
echo -n "T1.7  -P 无值 ..."
output=$(run -s 10.0.0.1 -u root -c "hostname" -P)
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Error: -P requires a value"; then
    pass
else
    fail "-P requires a value + EXIT=1" "EXIT=$exit_code" "T1.7"
fi

# T1.8
echo -n "T1.8  -a key 缺 -k ..."
output=$(run -s 10.0.0.1 -u root -a key -c "hostname" -P "test")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Error: -k/--key is required"; then
    pass
else
    fail "-k required + EXIT=1" "EXIT=$exit_code" "T1.8"
fi

# T1.9
echo -n "T1.9  非法 -a cert ..."
output=$(run -s 10.0.0.1 -u root -a cert -c "hostname" -P "test" -k /tmp/key)
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Invalid auth method"; then
    pass
else
    fail "Invalid auth method + EXIT=1" "EXIT=$exit_code" "T1.9"
fi

# T1.10
echo -n "T1.10 未知选项 --foo ..."
output=$(run -s 10.0.0.1 -u root -c "hostname" -P "test" --foo)
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Error: Unknown option: --foo"; then
    pass
else
    fail "Unknown option + EXIT=1" "EXIT=$exit_code" "T1.10"
fi

# T1.11
echo -n "T1.11 -p 无值 ..."
output=$(run -s 10.0.0.1 -u root -p -c "hostname" -P "test")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Error: -p requires a value"; then
    pass
else
    fail "-p requires a value + EXIT=1" "EXIT=$exit_code" "T1.11"
fi

# T1.12
echo -n "T1.12 --proxy 无值 ..."
output=$(run -s 10.0.0.1 -u root -c "hostname" -P "test" --proxy)
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Error: --proxy requires a value"; then
    pass
else
    fail "--proxy requires a value + EXIT=1" "EXIT=$exit_code" "T1.12"
fi

# T1.13
echo -n "T1.13 -k 无值 ..."
output=$(run -s 10.0.0.1 -u root -a key -k -c "hostname" -P "test")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Error: -k requires a value"; then
    pass
else
    fail "-k requires a value + EXIT=1" "EXIT=$exit_code" "T1.13"
fi

# T1.14
echo -n "T1.14 -h 帮助 ..."
output=$(run -h)
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]] && echo "$output" | grep -q "Usage:"; then
    pass
else
    fail "Usage + EXIT=0" "EXIT=$exit_code" "T1.14"
fi

if [[ "$QUICK_MODE" == true ]]; then
    echo ""
    echo "=== Quick mode: skipping network tests (Group 2-4) ==="
else

# ============================================================
# Group 2: Functional Tests (require network + proxy)
# ============================================================
echo ""
echo "=== Group 2: Functional Tests ==="

BASE_ARGS=(-s "$SERVER" -p "$PORT" -u "$USER" -a key -k "$KEY_PATH" -P "$PASSWORD" --proxy "$SOCKS_PROXY")

# T2.1
echo -n "T2.1  hostname ..."
output=$(run "${BASE_ARGS[@]}" -c "hostname")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$exit_code" "T2.1"
fi

# T2.2
echo -n "T2.2  whoami ..."
output=$(run "${BASE_ARGS[@]}" -c "whoami")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]] && echo "$output" | grep -q "root"; then
    pass
else
    fail "root + EXIT=0" "EXIT=$exit_code" "T2.2"
fi

# T2.3
echo -n "T2.3  uptime ..."
output=$(run "${BASE_ARGS[@]}" -c "uptime")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$exit_code" "T2.3"
fi

# T2.4
echo -n "T2.4  ls -la /root | head -5 ..."
output=$(run "${BASE_ARGS[@]}" -c "ls -la /root | head -5")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$exit_code" "T2.4"
fi

# T2.5
echo -n "T2.5  exit 42 ..."
output=$(run "${BASE_ARGS[@]}" -c "exit 42")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "42" ]]; then
    pass
else
    fail "EXIT=42" "EXIT=$exit_code" "T2.5"
fi

# T2.6
echo -n "T2.6  nonexistent-command ..."
output=$(run "${BASE_ARGS[@]}" -c "nonexistent_command_xyz")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "127" ]]; then
    pass
else
    fail "EXIT=127" "EXIT=$exit_code" "T2.6"
fi

# ============================================================
# Group 3: Proxy Tests
# ============================================================
echo ""
echo "=== Group 3: Proxy Tests ==="

# T3.1
echo -n "T3.1  SOCKS5 proxy ..."
output=$(run -s "$SERVER" -p "$PORT" -u "$USER" -a key -k "$KEY_PATH" -P "$PASSWORD" --proxy "$SOCKS_PROXY" --proxy-type socks5 -c "hostname")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]]; then
    pass
else
    fail "EXIT=0 via SOCKS5" "EXIT=$exit_code" "T3.1"
fi

# T3.2
echo -n "T3.2  HTTP proxy ..."
output=$(run -s "$SERVER" -p "$PORT" -u "$USER" -a key -k "$KEY_PATH" -P "$PASSWORD" --proxy "$HTTP_PROXY" --proxy-type http -c "hostname")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]]; then
    pass
else
    fail "EXIT=0 via HTTP" "EXIT=$exit_code" "T3.2"
fi

# ============================================================
# Group 4: Edge Cases
# ============================================================
echo ""
echo "=== Group 4: Edge Cases ==="

# T4.1
echo -n "T4.1  密码含特殊字符 ..."
output=$(run -s "$SERVER" -p "$PORT" -u "$USER" -a key -k "$KEY_PATH" -P "$PASSWORD" --proxy "$SOCKS_PROXY" -c "echo 'special chars OK'")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]]; then
    pass
else
    fail "EXIT=0 with special chars password" "EXIT=$exit_code" "T4.1"
fi

# T4.2
echo -n "T4.2  长管道命令 ..."
output=$(run "${BASE_ARGS[@]}" -c "echo A && echo B && echo C && uname -a")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$exit_code" "T4.2"
fi

# T4.3
echo -n "T4.3  stderr 混合输出 ..."
output=$(run "${BASE_ARGS[@]}" -c "echo stdout; echo stderr >&2")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$exit_code" "T4.3"
fi

# T4.4
echo -n "T4.4  密钥无密码短语 ..."
output=$(run -s "$SERVER" -p "$PORT" -u "$USER" -a key -k "$KEY_PATH" --proxy "$SOCKS_PROXY" -c "hostname")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "255" ]]; then
    pass
else
    fail "EXIT=255 (Permission denied)" "EXIT=$exit_code" "T4.4"
fi

# T4.5
echo -n "T4.5  完整密钥路径 ..."
output=$(run "${BASE_ARGS[@]}" -c "date +%Y")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$exit_code" "T4.5"
fi

# T4.6
echo -n "T4.6  密码认证无密码 ..."
output=$(run -s "$SERVER" -p "$PORT" -u "$USER" -a password -c "hostname")
exit_code=${output##*$'\n'}
if [[ "$exit_code" == "1" ]] && echo "$output" | grep -q "Password required for password authentication"; then
    pass
else
    fail "'Password required' + EXIT=1" "EXIT=$exit_code" "T4.6"
fi

# T4.7
echo -n "T4.7  默认端口22（预期失败）..."
output=$(run -s "$SERVER" -u "$USER" -a key -k "$KEY_PATH" -P "$PASSWORD" --proxy "$SOCKS_PROXY" -c "hostname")
exit_code=${output##*$'\n'}
if [[ "$exit_code" != "0" ]]; then
    pass
else
    fail "EXIT != 0 (port 22 fails)" "EXIT=$exit_code" "T4.7"
fi

fi

echo ""
echo "============================================"
TOTAL=$((PASS + FAIL + SKIP))
echo -e " Results: ${COLOR_GREEN}${PASS} passed${COLOR_NC}, ${COLOR_RED}${FAIL} failed${COLOR_NC}, ${COLOR_YELLOW}${SKIP} skipped${COLOR_NC} (total: ${TOTAL})"
echo "============================================"

if [[ ${#FAILED_TESTS[@]} -gt 0 ]]; then
    echo ""
    echo "Failed tests:"
    for t in "${FAILED_TESTS[@]}"; do
        echo "  - $t"
    done
fi

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
