#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8900}"
HOST="${2:-${ADMINBOT_HOST:-127.0.0.1}}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Reuses venv/ or .venv/; creates venv/ and installs nanobot on the first run.
bash "$SCRIPT_DIR/scripts/ensure-venv.sh"
if [[ -x "$SCRIPT_DIR/venv/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/venv/bin/python"
else
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
fi

echo ""
echo "=== Nanobot Adminbot Launcher ==="
echo "Starting local multi-bot manager on http://${HOST}:${PORT}"
if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" ]]; then
  echo "Warning: non-local bind exposes Adminbot login to the network. Use a strong password." >&2
fi
echo ""
echo "Runtime state is stored in .adminbot/ and is gitignored."
echo "Press Ctrl+C to stop Adminbot."
echo ""

cd "$SCRIPT_DIR"
# Opens the dashboard in the browser once it answers (desktop only; NANOBOT_NO_BROWSER=1 to skip).
BROWSER_HOST="$HOST"
[[ "$HOST" == "0.0.0.0" || "$HOST" == "::" ]] && BROWSER_HOST="127.0.0.1"
bash "$SCRIPT_DIR/scripts/open-browser.sh" "http://${BROWSER_HOST}:${PORT}" &
exec "$PYTHON" -m adminbot.app.main web --port "$PORT" --host "$HOST"
