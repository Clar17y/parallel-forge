#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="python3"

if [[ -x "${SCRIPT_DIR}/../.venv/bin/python" ]]; then
    PYTHON="${SCRIPT_DIR}/../.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON="python"
fi

exec "$PYTHON" "${SCRIPT_DIR}/dev.py" "$@"
