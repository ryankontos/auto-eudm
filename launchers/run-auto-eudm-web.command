#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  print -u2 "Python 3.10 or newer is required. Install it from python.org, then run this file again."
  exit 1
fi

exec python3 "$ROOT_DIR/start_auto_eudm.py" "$@"
