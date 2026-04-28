#!/usr/bin/env bash
# ssh-exec.sh - SSH remote execution with automatic password input
# Supports: SSH key + passphrase, password authentication, proxy
# Designed for MSYS2 UCRT64 / Git Bash environment

set -eo pipefail

# Default values
SERVER=""
USER=""
COMMAND=""
PORT=22
AUTH_METHOD="key"
KEY_PATH=""
PASSWORD=""
PROXY=""
PROXY_TYPE="socks5"
TIMEOUT=30

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Help message
usage() {
    cat <<EOF
Usage: ssh-exec.sh [OPTIONS]

SSH remote execution with automatic password/passphrase input.

Required:
  -s, --server SERVER      SSH server address
  -u, --user USER          SSH username
  -c, --command COMMAND    Command to execute on remote server

Optional:
  -p, --port PORT          SSH port (default: 22)
  -a, --auth METHOD        Authentication method: key or password (default: key)
  -k, --key PATH           SSH private key path (required when -a key)
  -P, --password PASS      Password or key passphrase
      --proxy ADDR         Proxy address (format: host:port)
      --proxy-type TYPE    Proxy type: socks5 or http (default: socks5)
  -t, --timeout SECONDS    Connection timeout (default: 30)
  -h, --help               Show this help message

Examples:
  # Password authentication
  ssh-exec.sh -s 10.0.0.1 -u admin -P secret -c "hostname"

  # Key with passphrase
  ssh-exec.sh -s 10.0.0.1 -u admin -k ~/.ssh/id_ed25519 -P passphrase -c "uptime"

  # Custom port
  ssh-exec.sh -s 10.0.0.1 -u admin -p 443 -P secret -c "ls -la"

  # With proxy
  ssh-exec.sh -s 10.0.0.1 -u admin -P secret --proxy 127.0.0.1:1080 -c "whoami"

EOF
}

validate_arg() {
    local flag="$1" value="${2:-}"
    if [[ -z "$value" || "$value" == -* ]]; then
        echo -e "${RED}Error: $flag requires a value${NC}" >&2
        exit 1
    fi
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -s|--server)
            validate_arg "$1" "$2"
            SERVER="$2"
            shift 2
            ;;
        -u|--user)
            validate_arg "$1" "$2"
            USER="$2"
            shift 2
            ;;
        -c|--command)
            validate_arg "$1" "$2"
            COMMAND="$2"
            shift 2
            ;;
        -p|--port)
            validate_arg "$1" "$2"
            PORT="$2"
            shift 2
            ;;
        -a|--auth)
            validate_arg "$1" "$2"
            AUTH_METHOD="$2"
            shift 2
            ;;
        -k|--key)
            validate_arg "$1" "$2"
            KEY_PATH="$2"
            shift 2
            ;;
        -P|--password)
            validate_arg "$1" "$2"
            PASSWORD="$2"
            shift 2
            ;;
        --proxy)
            validate_arg "$1" "$2"
            PROXY="$2"
            shift 2
            ;;
        --proxy-type)
            validate_arg "$1" "$2"
            PROXY_TYPE="$2"
            shift 2
            ;;
        -t|--timeout)
            validate_arg "$1" "$2"
            TIMEOUT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option: $1${NC}"
            usage
            exit 1
            ;;
    esac
done

# Validate required parameters
if [[ -z "$SERVER" || -z "$USER" || -z "$COMMAND" ]]; then
    echo -e "${RED}Error: Missing required parameters${NC}"
    usage
    exit 1
fi

if [[ "$AUTH_METHOD" != "key" && "$AUTH_METHOD" != "password" ]]; then
    echo -e "${RED}Error: Invalid auth method. Use 'key' or 'password'${NC}"
    exit 1
fi

if [[ "$AUTH_METHOD" == "key" && -z "$KEY_PATH" ]]; then
    echo -e "${RED}Error: -k/--key is required when using key authentication${NC}"
    exit 1
fi

# Build SSH options
SSH_OPTS=(
    -o "StrictHostKeyChecking=no"
    -o "UserKnownHostsFile=/dev/null"
    -o "LogLevel=ERROR"
    -o "ConnectTimeout=$TIMEOUT"
    -p "$PORT"
)

# Add proxy if specified
if [[ -n "$PROXY" ]]; then
    PROXY_FLAG="-S"
    [[ "$PROXY_TYPE" == "http" ]] && PROXY_FLAG="-H"
    
    SSH_OPTS+=(-o "ProxyCommand=connect.exe $PROXY_FLAG $PROXY %h %p")
fi

# Create askpass helper
ASKPASS_SCRIPT=$(mktemp)
cat > "$ASKPASS_SCRIPT" <<'EOF'
#!/usr/bin/env bash
printf '%s' "$SSH_ASKPASS_PASSWORD"
EOF
chmod +x "$ASKPASS_SCRIPT"

# Cleanup function
cleanup() {
    rm -f "$ASKPASS_SCRIPT"
}
trap cleanup EXIT

# Execute SSH with askpass
execute_ssh() {
    local ssh_args=("${SSH_OPTS[@]}")
    
    if [[ "$AUTH_METHOD" == "key" ]]; then
        # Key authentication
        if [[ ! -f "$KEY_PATH" ]]; then
            echo -e "${RED}Error: SSH key not found: $KEY_PATH${NC}"
            exit 1
        fi
        ssh_args+=(-o "IdentityFile=$KEY_PATH")
    fi
    
    ssh_args+=("${USER}@${SERVER}")
    ssh_args+=("$COMMAND")
    
    # Set up askpass environment
    export SSH_ASKPASS="$ASKPASS_SCRIPT"
    export SSH_ASKPASS_PASSWORD="$PASSWORD"
    export SSH_ASKPASS_REQUIRE="force"
    export DISPLAY="dummy:0"
    
    ssh "${ssh_args[@]}" 2>&1
}

# Main execution
if [[ -z "$PASSWORD" ]]; then
    # No password provided, try without askpass
    if [[ "$AUTH_METHOD" == "key" ]]; then
        # Key without passphrase
        local_no_pass_opts=("${SSH_OPTS[@]}")
        local_no_pass_opts+=(-o "IdentityFile=$KEY_PATH")
        local_no_pass_opts+=("${USER}@${SERVER}")
        local_no_pass_opts+=("$COMMAND")
        ssh "${local_no_pass_opts[@]}" 2>&1
    else
        echo -e "${RED}Error: Password required for password authentication${NC}" >&2
        exit 1
    fi
else
    # Password provided, use askpass
    execute_ssh
fi