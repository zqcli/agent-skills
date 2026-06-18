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

# run sets LAST_OUTPUT (combined stdout+stderr) and LAST_EXIT (exit code).
# Capturing the exit code separately is robust regardless of the command's
# output content (the old approach parsed the last output line as the code).
LAST_OUTPUT=""
LAST_EXIT=0
run() {
    set +e
    LAST_OUTPUT="$(bash "$SKILL" "$@" 2>&1)"
    LAST_EXIT=$?
    set -e
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
run -u root -c "hostname" -P "test"
if [[ "$LAST_EXIT" != "0" ]] && echo "$LAST_OUTPUT" | grep -q "Error: Missing required parameters"; then
    pass
else
    fail "EXIT != 0 + 'Missing required parameters'" "EXIT=$LAST_EXIT" "T1.1"
fi

# T1.2
echo -n "T1.2  缺少 -u ..."
run -s 10.0.0.1 -c "hostname" -P "test"
if [[ "$LAST_EXIT" != "0" ]]; then
    pass
else
    fail "EXIT != 0" "EXIT=$LAST_EXIT" "T1.2"
fi

# T1.3
echo -n "T1.3  缺少 -c ..."
run -s 10.0.0.1 -u root -P "test"
if [[ "$LAST_EXIT" != "0" ]]; then
    pass
else
    fail "EXIT != 0" "EXIT=$LAST_EXIT" "T1.3"
fi

# T1.4
echo -n "T1.4  -s 无值 ..."
run -s -u root -c "hostname" -P "test"
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Error: -s requires a value"; then
    pass
else
    fail "-s requires a value + EXIT=1" "EXIT=$LAST_EXIT" "T1.4"
fi

# T1.5
echo -n "T1.5  -u 无值 ..."
run -s 10.0.0.1 -u -c "hostname" -P "test"
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Error: -u requires a value"; then
    pass
else
    fail "-u requires a value + EXIT=1" "EXIT=$LAST_EXIT" "T1.5"
fi

# T1.6
echo -n "T1.6  -c 无值 ..."
run -s 10.0.0.1 -u root -c -P "test"
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Error: -c requires a value"; then
    pass
else
    fail "-c requires a value + EXIT=1" "EXIT=$LAST_EXIT" "T1.6"
fi

# T1.7
echo -n "T1.7  -P 无值 ..."
run -s 10.0.0.1 -u root -c "hostname" -P
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Error: -P requires a value"; then
    pass
else
    fail "-P requires a value + EXIT=1" "EXIT=$LAST_EXIT" "T1.7"
fi

# T1.8
echo -n "T1.8  -a key 缺 -k ..."
run -s 10.0.0.1 -u root -a key -c "hostname" -P "test"
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Error: -k/--key is required"; then
    pass
else
    fail "-k required + EXIT=1" "EXIT=$LAST_EXIT" "T1.8"
fi

# T1.9
echo -n "T1.9  非法 -a cert ..."
run -s 10.0.0.1 -u root -a cert -c "hostname" -P "test" -k /tmp/key
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Invalid auth method"; then
    pass
else
    fail "Invalid auth method + EXIT=1" "EXIT=$LAST_EXIT" "T1.9"
fi

# T1.10
echo -n "T1.10 未知选项 --foo ..."
run -s 10.0.0.1 -u root -c "hostname" -P "test" --foo
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Error: Unknown option: --foo"; then
    pass
else
    fail "Unknown option + EXIT=1" "EXIT=$LAST_EXIT" "T1.10"
fi

# T1.11
echo -n "T1.11 -p 无值 ..."
run -s 10.0.0.1 -u root -p -c "hostname" -P "test"
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Error: -p requires a value"; then
    pass
else
    fail "-p requires a value + EXIT=1" "EXIT=$LAST_EXIT" "T1.11"
fi

# T1.12
echo -n "T1.12 --proxy 无值 ..."
run -s 10.0.0.1 -u root -c "hostname" -P "test" --proxy
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Error: --proxy requires a value"; then
    pass
else
    fail "--proxy requires a value + EXIT=1" "EXIT=$LAST_EXIT" "T1.12"
fi

# T1.13
echo -n "T1.13 -k 无值 ..."
run -s 10.0.0.1 -u root -a key -k -c "hostname" -P "test"
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Error: -k requires a value"; then
    pass
else
    fail "-k requires a value + EXIT=1" "EXIT=$LAST_EXIT" "T1.13"
fi

# T1.14
echo -n "T1.14 -h 帮助 ..."
run -h
if [[ "$LAST_EXIT" == "0" ]] && echo "$LAST_OUTPUT" | grep -q "Usage:"; then
    pass
else
    fail "Usage + EXIT=0" "EXIT=$LAST_EXIT" "T1.14"
fi

# T1.15
echo -n "T1.15 密钥文件不存在 ..."
run -s 10.0.0.1 -u root -a key -k /nonexistent/key_xyz -c "hostname" -P "test"
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Error: SSH key not found or not readable"; then
    pass
else
    fail "SSH key not found + EXIT=1" "EXIT=$LAST_EXIT" "T1.15"
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
run "${BASE_ARGS[@]}" -c "hostname"
if [[ "$LAST_EXIT" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$LAST_EXIT" "T2.1"
fi

# T2.2
echo -n "T2.2  whoami ..."
run "${BASE_ARGS[@]}" -c "whoami"
if [[ "$LAST_EXIT" == "0" ]] && echo "$LAST_OUTPUT" | grep -q "root"; then
    pass
else
    fail "root + EXIT=0" "EXIT=$LAST_EXIT" "T2.2"
fi

# T2.3
echo -n "T2.3  uptime ..."
run "${BASE_ARGS[@]}" -c "uptime"
if [[ "$LAST_EXIT" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$LAST_EXIT" "T2.3"
fi

# T2.4
echo -n "T2.4  ls -la /root | head -5 ..."
run "${BASE_ARGS[@]}" -c "ls -la /root | head -5"
if [[ "$LAST_EXIT" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$LAST_EXIT" "T2.4"
fi

# T2.5
echo -n "T2.5  exit 42 ..."
run "${BASE_ARGS[@]}" -c "exit 42"
if [[ "$LAST_EXIT" == "42" ]]; then
    pass
else
    fail "EXIT=42" "EXIT=$LAST_EXIT" "T2.5"
fi

# T2.6
echo -n "T2.6  nonexistent-command ..."
run "${BASE_ARGS[@]}" -c "nonexistent_command_xyz"
if [[ "$LAST_EXIT" == "127" ]]; then
    pass
else
    fail "EXIT=127" "EXIT=$LAST_EXIT" "T2.6"
fi

# ============================================================
# Group 3: Proxy Tests
# ============================================================
echo ""
echo "=== Group 3: Proxy Tests ==="

# T3.1
echo -n "T3.1  SOCKS5 proxy ..."
run -s "$SERVER" -p "$PORT" -u "$USER" -a key -k "$KEY_PATH" -P "$PASSWORD" --proxy "$SOCKS_PROXY" --proxy-type socks5 -c "hostname"
if [[ "$LAST_EXIT" == "0" ]]; then
    pass
else
    fail "EXIT=0 via SOCKS5" "EXIT=$LAST_EXIT" "T3.1"
fi

# T3.2
echo -n "T3.2  HTTP proxy ..."
run -s "$SERVER" -p "$PORT" -u "$USER" -a key -k "$KEY_PATH" -P "$PASSWORD" --proxy "$HTTP_PROXY" --proxy-type http -c "hostname"
if [[ "$LAST_EXIT" == "0" ]]; then
    pass
else
    fail "EXIT=0 via HTTP" "EXIT=$LAST_EXIT" "T3.2"
fi

# ============================================================
# Group 4: Edge Cases
# ============================================================
echo ""
echo "=== Group 4: Edge Cases ==="

# T4.1
echo -n "T4.1  密码含特殊字符 ..."
run -s "$SERVER" -p "$PORT" -u "$USER" -a key -k "$KEY_PATH" -P "$PASSWORD" --proxy "$SOCKS_PROXY" -c "echo 'special chars OK'"
if [[ "$LAST_EXIT" == "0" ]]; then
    pass
else
    fail "EXIT=0 with special chars password" "EXIT=$LAST_EXIT" "T4.1"
fi

# T4.2
echo -n "T4.2  长管道命令 ..."
run "${BASE_ARGS[@]}" -c "echo A && echo B && echo C && uname -a"
if [[ "$LAST_EXIT" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$LAST_EXIT" "T4.2"
fi

# T4.3
echo -n "T4.3  stderr 混合输出 ..."
run "${BASE_ARGS[@]}" -c "echo stdout; echo stderr >&2"
if [[ "$LAST_EXIT" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$LAST_EXIT" "T4.3"
fi

# T4.4
echo -n "T4.4  密钥无密码短语 ..."
run -s "$SERVER" -p "$PORT" -u "$USER" -a key -k "$KEY_PATH" --proxy "$SOCKS_PROXY" -c "hostname"
if [[ "$LAST_EXIT" == "255" ]]; then
    pass
else
    fail "EXIT=255 (Permission denied)" "EXIT=$LAST_EXIT" "T4.4"
fi

# T4.5
echo -n "T4.5  完整密钥路径 ..."
run "${BASE_ARGS[@]}" -c "date +%Y"
if [[ "$LAST_EXIT" == "0" ]]; then
    pass
else
    fail "EXIT=0" "EXIT=$LAST_EXIT" "T4.5"
fi

# T4.6
echo -n "T4.6  密码认证无密码 ..."
run -s "$SERVER" -p "$PORT" -u "$USER" -a password -c "hostname"
if [[ "$LAST_EXIT" == "1" ]] && echo "$LAST_OUTPUT" | grep -q "Password required for password authentication"; then
    pass
else
    fail "'Password required' + EXIT=1" "EXIT=$LAST_EXIT" "T4.6"
fi

# T4.7
echo -n "T4.7  默认端口22（预期失败）..."
run -s "$SERVER" -u "$USER" -a key -k "$KEY_PATH" -P "$PASSWORD" --proxy "$SOCKS_PROXY" -c "hostname"
if [[ "$LAST_EXIT" != "0" ]]; then
    pass
else
    fail "EXIT != 0 (port 22 fails)" "EXIT=$LAST_EXIT" "T4.7"
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
