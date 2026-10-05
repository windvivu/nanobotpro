#!/usr/bin/env bash
# Make sure venv/ (or .venv/) exists and nanobot is installed in it; set it up on the first run.
# Used by nanobot-launcher.sh and nanobot-single.sh. Docker images do not need it.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
READY="import importlib.metadata as m, importlib.util as u; m.distribution('nanobot'); assert all(u.find_spec(x) for x in ('fastapi', 'uvicorn', 'openpyxl', 'docx', 'pypdf'))"

PY=""
for venv in venv .venv; do
  if [ -x "$ROOT/$venv/bin/python" ]; then PY="$ROOT/$venv/bin/python"; break; fi
done
if [ -n "$PY" ] && "$PY" -c "$READY" 2>/dev/null; then
  exit 0
fi

echo "Setting up the Python environment (first run only, may take a few minutes)..."
if [ -z "$PY" ]; then
  # First Python 3.11+ found; e.g. Ubuntu 22.04's python3 is 3.10 next to a python3.12.
  BASE=""
  for cand in python3 python python3.13 python3.12 python3.11; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      BASE="$cand"
      break
    fi
  done
  if [ -z "$BASE" ]; then
    echo "Python 3.11 or newer is required. Install it and run again." >&2
    exit 1
  fi
  "$BASE" -m venv "$ROOT/venv" || {
    echo "Could not create $ROOT/venv (on Debian/Ubuntu: apt install python3-venv or python3.X-venv)." >&2
    exit 1
  }
  PY="$ROOT/venv/bin/python"
fi

(cd "$ROOT" && "$PY" -m pip install -e ".[web,office]") || {
  echo "Installing nanobot failed. Fix the error above and run again." >&2
  exit 1
}
echo "Python environment ready."
