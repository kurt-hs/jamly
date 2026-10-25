#!/usr/bin/env bash
# splitfy HTTP status server — start/stop/status/restart helper around systemd.
#
# Mirrors windows/start-splitfy-server.bat but manages the service so it survives
# reboots (see linux/splitfy-server.service). On a box without systemd, fall back to
# running the foreground server directly with the same arguments.
#
# Usage:  ./splitfy-server-launch.sh {start|stop|status|restart} [PORT]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"   # repo root = one level up from linux/
PORT="${1:-8765}"
PY="python3"

case "${1:-}" in
  start)   systemctl daemon-reload; systemctl enable --now splitfy-server.service ;;
  stop)    systemctl disable --now splitfy-server.service || true ;;
  restart) systemctl restart splitfy-server.service ;;
  status)  systemctl status splitfy-server.service --no-pager ;;
  *) echo "usage: $0 {start|stop|status|restart} [PORT]"; exit 1 ;;
esac
