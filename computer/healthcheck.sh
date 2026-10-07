#!/usr/bin/env bash
# Computer healthcheck: display up, browser process alive, CDP discovery live.
set -u
DISPLAY_NUM="${DISPLAY_NUM:-:1}"
CDP_PORT="${CDP_PORT:-9222}"

# 1) X display socket present
[ -S "/tmp/.X11-unix/X${DISPLAY_NUM#:}" ] || { echo "no X display"; exit 1; }

# 2) browser process alive
pgrep -f "user-data-dir=/browser-profile" >/dev/null 2>&1 || { echo "no browser"; exit 1; }

# 3) CDP discovery answers and reports a page target
curl -sf -m 5 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1 || { echo "no CDP"; exit 1; }
curl -sf -m 5 "http://127.0.0.1:${CDP_PORT}/json/list" | grep -q '"type": *"page"' || { echo "no page target"; exit 1; }

# 4) KasmVNC websocket port listening
(ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ':6901' || { echo "no KasmVNC"; exit 1; }

echo "ok"
exit 0
