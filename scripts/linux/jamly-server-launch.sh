#!/usr/bin/env bash
# jamly HTTP status server — start/stop/status/restart helper around systemd.
#
# Mirrors windows/start-jamly-server.bat but manages the service so it survives
# reboots (see linux/jamly-server.service). On a box without systemd, fall back to
# running the foreground server directly with the same arguments.
#
# Usage:  ./jamly-server-launch.sh {start|stop|status|restart} [PORT]

set -euo pipefail

# Walk up from this file until we reach the repo root (the dir containing src/jamly). The wrappers
# now live at <root>/scripts/<os>/<file>, so a depth-independent search keeps this correct.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && cd .. && pwd)"   # repo root = two levels up from scripts/linux/
PORT="${1:-8765}"
PY="python3"

case "${1:-}" in
  start)   systemctl daemon-reload; systemctl enable --now jamly-server.service ;;
  stop)    systemctl disable --now jamly-server.service || true ;;
  restart) systemctl restart jamly-server.service ;;
  status)  systemctl status jamly-server.service --no-pager ;;
  *) echo "usage: $0 {start|stop|status|restart} [PORT]"; exit 1 ;;
esac
