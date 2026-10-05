#!/usr/bin/env bash
# Open a dashboard URL in the default browser as soon as its port answers (gives up after 2 min).
# Started in the background by nanobot-launcher.sh and nanobot-single.sh.
# Does nothing without a desktop (VPS, Docker) or with NANOBOT_NO_BROWSER=1.
URL="$1"
[ "${NANOBOT_NO_BROWSER:-}" = "1" ] && exit 0
if [ "$(uname)" = "Darwin" ]; then
  OPEN=open
elif command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  OPEN=xdg-open
else
  exit 0
fi
HOSTPORT="${URL#*://}"
HOSTPORT="${HOSTPORT%%/*}"
for _ in $(seq 1 240); do
  if (exec 3<>"/dev/tcp/${HOSTPORT%:*}/${HOSTPORT##*:}") 2>/dev/null; then
    "$OPEN" "$URL" >/dev/null 2>&1
    exit 0
  fi
  sleep 0.5
done
