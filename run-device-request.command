#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  print -u2 "Python 3 is required. Install it, then run this file again."
  exit 1
fi

if (( $# == 0 )); then
  python3 automate_device_request.py --help
  print ""
  print "Example:"
  print "  ./run-device-request.command --simulate --serial ABC1234 --request-for tester --target user --status 'Deployed - New Stock' --deployed-to simulated.user --submit"
  read -r "?Press Enter to close..."
  exit 0
fi

exec python3 automate_device_request.py "$@"
