#!/usr/bin/env bash
# Launch the single human-visible Chrome instance with CDP, inside the Kasm
# desktop's DISPLAY, with persistent profile + downloads.
#
# Contract (task doc 6.2): non-headless, one persistent context shared by the
# human (via VNC) and the agent (via CDP), fixed --user-data-dir so Chrome 136+
# allows remote debugging, and auto-restart if it dies.
#
# MEASURED PITFALLS on kasmweb/desktop:1.18.0 (2026-10-06) - all reproduced:
#   1. This image starts Xvnc + the XFCE session as ROOT, not as kasm-user
#      (env has HOME=/home/kasm-user but no USER, so vnc_startup.sh stays root).
#      Therefore /home/kasm-user/.Xauthority is root-owned and a `runuser -u
#      kasm-user` launch fails with "Authorization required".
#      => run Chrome as root with the root XAUTHORITY, plus --no-sandbox.
#   2. Chrome must not start before Xvnc is serving. Wait for BOTH the socket
#      and a successful xdpyinfo, else "Missing X server or $DISPLAY".
#   3. Chrome as root requires --no-sandbox.
set -u

DISPLAY_NUM="${DISPLAY_NUM:-:1}"
export DISPLAY="$DISPLAY_NUM"

BROWSER_BIN="${BROWSER_BIN:-/opt/google/chrome/google-chrome}"
USER_DATA_DIR="${USER_DATA_DIR:-/browser-profile}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-/downloads}"
CDP_PORT="${CDP_PORT:-9222}"
XAUTH="${XAUTH:-/home/kasm-user/.Xauthority}"
export XAUTHORITY="$XAUTH"

mkdir -p "$USER_DATA_DIR" "$DOWNLOAD_DIR" /workspace 2>/dev/null || true

# Wait for Xvnc to be genuinely ready: socket exists AND xdpyinfo answers.
for _ in $(seq 1 120); do
  if [ -S "/tmp/.X11-unix/X${DISPLAY_NUM#:}" ] && [ -f "$XAUTH" ]; then
    if xdpyinfo -display "$DISPLAY_NUM" >/dev/null 2>&1; then
      echo "[browser-launch] X display $DISPLAY_NUM ready" >&2
      break
    fi
  fi
  sleep 2
done

launch() {
  # Non-headless: the agent and the human must see the SAME window.
  setsid "$BROWSER_BIN" \
      --user-data-dir="$USER_DATA_DIR" \
      --remote-debugging-port="$CDP_PORT" \
      --no-first-run \
      --no-default-browser-check \
      --no-sandbox \
      --disable-gpu \
      --disable-dev-shm-usage \
      about:blank >>/tmp/chrome.log 2>&1 &
}

# One browser only: never let a second, CDP-less instance appear.
pkill -f "user-data-dir=$USER_DATA_DIR" 2>/dev/null || true
sleep 3

# MEASURED PITFALL: /browser-profile is a persistent volume. After a container
# is replaced (or Chrome was killed hard), it keeps a stale SingletonLock that
# names the OLD hostname, e.g.
#   "The profile appears to be in use by another Google Chrome process (461)
#    on another computer (beaa3c05634c)."
# Chrome then refuses to start forever. Remove the locks ONLY when no live
# Chrome process holds the profile.
if ! pgrep -f "user-data-dir=$USER_DATA_DIR" >/dev/null 2>&1; then
  rm -f "$USER_DATA_DIR/SingletonLock" \
        "$USER_DATA_DIR/SingletonSocket" \
        "$USER_DATA_DIR/SingletonCookie" 2>/dev/null || true
fi

launch

# Wait until CDP discovery answers before declaring readiness.
for _ in $(seq 1 30); do
  curl -sf -m 5 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1 && break
  sleep 2
done
if curl -sf -m 5 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
  echo "[browser-launch] CDP up on ${CDP_PORT}" >&2
else
  echo "[browser-launch] CDP did NOT come up" >&2
fi

# Supervisor: keep exactly one instance alive, re-verify CDP after each restart.
while true; do
  if ! pgrep -f "user-data-dir=$USER_DATA_DIR" >/dev/null 2>&1; then
    echo "[browser-launch] chrome gone, restarting" >&2
    launch
    for _ in $(seq 1 30); do
      curl -sf -m 5 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1 && break
      sleep 2
    done
    if curl -sf -m 5 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
      echo "[browser-launch] CDP recovered" >&2
    else
      echo "[browser-launch] CDP NOT recovered after restart" >&2
    fi
  fi
  sleep 15
done
