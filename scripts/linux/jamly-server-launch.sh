#!/usr/bin/env bash
# jamly HTTP status server — start/stop/status/restart helper around systemd.
#
# Mirrors windows/start-jamly-server.bat but manages the service so it survives
# reboots (see linux/jamly-server.service). On a box without systemd, fall back to
# running the foreground server directly with the same arguments.
#
# Usage:  ./jamly-server-launch.sh {start|stop|status|restart|foreground} [PORT]

set -euo pipefail

# Walk up from this file until we reach the repo root (the dir containing src/jamly). The wrappers
# now live at <root>/scripts/<os>/<file>, so a depth-independent search keeps this correct.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && cd .. && pwd)"   # repo root = two levels up from scripts/linux/
ACTION="${1:-}"
PORT="${2:-8765}"
PY="${PYTHON:-$(command -v python3 || true)}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/jamly-server.service"

if [[ -z "$PY" ]]; then
  echo "python3 not found on PATH" >&2
  exit 1
fi

write_unit() {
  mkdir -p "$UNIT_DIR"
  cat >"$UNIT_FILE" <<EOF
[Unit]
Description=jamly audio-separation HTTP server
After=network.target

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
Environment="JAMLY_SCOPE=$REPO_ROOT"
Environment="PYTHONPATH=$REPO_ROOT/src"
ExecStart=$PY -m jamly.server --port $PORT --scope "$REPO_ROOT"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
}

has_user_systemd() {
  command -v systemctl >/dev/null 2>&1 &&
    systemctl --user show-environment >/dev/null 2>&1
}

case "$ACTION" in
  start)
    if has_user_systemd; then
      write_unit
      systemctl --user daemon-reload
      systemctl --user enable --now jamly-server.service
    else
      exec env PYTHONPATH="$REPO_ROOT/src" "$PY" -m jamly.server --port "$PORT" --scope "$REPO_ROOT"
    fi
    ;;
  stop)
    has_user_systemd || { echo "user systemd is not available" >&2; exit 1; }
    systemctl --user disable --now jamly-server.service
    ;;
  restart)
    has_user_systemd || { echo "user systemd is not available" >&2; exit 1; }
    write_unit
    systemctl --user daemon-reload
    systemctl --user restart jamly-server.service
    ;;
  status)
    has_user_systemd || { echo "user systemd is not available" >&2; exit 1; }
    systemctl --user status jamly-server.service --no-pager
    ;;
  foreground)
    exec env PYTHONPATH="$REPO_ROOT/src" "$PY" -m jamly.server --port "$PORT" --scope "$REPO_ROOT"
    ;;
  *)
    echo "usage: $0 {start|stop|status|restart|foreground} [PORT]"
    exit 1
    ;;
esac
