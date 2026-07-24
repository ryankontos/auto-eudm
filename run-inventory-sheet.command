#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  print -u2 "Python 3 is required. Install it, then run this file again."
  exit 1
fi

exec python3 inventory_sheet_cli.py "$@"
