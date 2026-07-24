#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  print -u2 "Python 3 is required. Install it, then run this file again."
  exit 1
fi

if (( $# == 0 )); then
  PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m dwp_device_request.automate_device_request --help
  print ""
  print "Example:"
  print "  ./launchers/run-device-request.command --simulate --serial ABC1234 --request-for tester --target user --status 'Deployed - New Stock' --deployed-to simulated.user --submit"
  read -r "?Press Enter to close..."
  exit 0
fi

exec env PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m dwp_device_request.automate_device_request "$@"
