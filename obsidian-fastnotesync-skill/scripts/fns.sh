#!/usr/bin/env sh
# Compatibility entry point; all logic and transport live in fns.py.
set +x
set -eu
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '{"success":false,"error":{"type":"config","message":"Python 3.10+ with requests[socks] required"}}'
  exit 2
fi
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec python3 -B "$SCRIPT_DIR/fns.py" "$@"
