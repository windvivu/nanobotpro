#!/usr/bin/env bash
set -euo pipefail

PORT="${ADMINBOT_PORT:-8900}"
HOST="${ADMINBOT_HOST:-127.0.0.1}"

# Accept the same named host option as nanobot-single.*. Keep the old
# positional form (PORT HOST) for compatibility with existing scripts.
POSITIONAL=()
while (($#)); do
  case "$1" in
    --host)
      if (($# < 2)); then
        echo "Missing value for --host" >&2
        exit 2
      fi
      HOST="$2"
      shift 2
      ;;
    --host=*)
      HOST="${1#*=}"
      shift
      ;;
    --port)
      if (($# < 2)); then
        echo "Missing value for --port" >&2
        exit 2
      fi
      PORT="$2"
      shift 2
      ;;
    --port=*)
      PORT="${1#*=}"
      shift
      ;;
    --)
      shift
      POSITIONAL+=("$@")
      break
      ;;
    -*)
      echo "Unknown option: $1 (use --host HOST or --port PORT)" >&2
      exit 2
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if ((${#POSITIONAL[@]} > 2)); then
  echo "Too many positional arguments (expected PORT HOST)" >&2
  exit 2
fi
if ((${#POSITIONAL[@]} >= 1)); then PORT="${POSITIONAL[0]}"; fi
if ((${#POSITIONAL[@]} >= 2)); then HOST="${POSITIONAL[1]}"; fi
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
