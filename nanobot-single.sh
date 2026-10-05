#!/usr/bin/env bash
# Run one nanobot bot with its web dashboard, without Adminbot.
#   bash nanobot-single.sh                                default bot (~/.nanobot/config.json)
#   bash nanobot-single.sh --config path/to/config.json   another bot, e.g. one created in Adminbot
# Refuses to start while Adminbot or a bot it started is running (the account would answer twice).
if pids=$(pgrep -f 'adminbot[.]app[.]main|[.]adminbot/instances'); then
  echo "Adminbot or one of its bots is running (PID $(echo $pids)). Stop it in Adminbot first." >&2
  exit 1
fi
DIR="$(dirname "$0")"
# Reuses venv/ or .venv/; creates venv/ and installs nanobot on the first run.
bash "$DIR/scripts/ensure-venv.sh" || exit 1
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY="$DIR/.venv/bin/python"

# Opens the dashboard in the browser once it answers (desktop only; NANOBOT_NO_BROWSER=1 to skip).
# Its address comes from the config the gateway reads (default port 8899).
URL="$("$PY" - "$@" <<'EOF'
import json, sys
from pathlib import Path
args, cfg, host = sys.argv[1:], Path.home() / ".nanobot" / "config.json", None
for i, a in enumerate(args):
    nxt = args[i + 1] if i + 1 < len(args) else None
    if a in ("--config", "-c") and nxt:
        cfg = Path(nxt).expanduser()
    elif a.startswith("--config="):
        cfg = Path(a[len("--config="):]).expanduser()
    elif a == "--host" and nxt:
        host = nxt
try:
    web = json.loads(cfg.read_text(encoding="utf-8")).get("gateway", {}).get("web", {})
except Exception:
    web = {}
host = host or web.get("host") or "127.0.0.1"
print(f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{web.get('port') or 8899}")
EOF
)"
case " $* " in *" --help "*) ;; *) bash "$DIR/scripts/open-browser.sh" "$URL" & ;; esac

exec "$PY" -m nanobot gateway --web "$@"
