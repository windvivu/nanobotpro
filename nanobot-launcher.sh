#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8900}"
HOST="${2:-${ADMINBOT_HOST:-127.0.0.1}}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x "$SCRIPT_DIR/venv/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/venv/bin/python"
elif [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
else
  echo "Missing local venv Python." >&2
  echo "Expected one of:" >&2
  echo "  $SCRIPT_DIR/venv/bin/python" >&2
  echo "  $SCRIPT_DIR/.venv/bin/python" >&2
  echo "" >&2
  echo "Create and install the environment first:" >&2
  echo "  python3 -m venv venv" >&2
  echo "  source venv/bin/activate" >&2
  echo "  python -m pip install -U pip" >&2
  echo "  python -m pip install -e '.[web,dev]'" >&2
  exit 1
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
exec "$PYTHON" -m adminbot.app.main web --port "$PORT" --host "$HOST"
