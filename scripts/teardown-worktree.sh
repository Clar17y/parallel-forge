#!/usr/bin/env bash
set -euo pipefail
exec python -m forge.cli.main worktree teardown "$@"
