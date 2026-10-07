#!/usr/bin/env bash
# niucai Computer entrypoint.
#
# The Kasm base image's own entrypoint is /dockerstartup/vnc_startup.sh, which
# starts Xvnc(:1) + XFCE4 + KasmVNC(:6901) and then blocks in the foreground.
#
# MEASURED PITFALL (2026-10-06): passing our launcher as an extra argument
# (vnc_startup.sh /opt/niucai/browser-launch.sh) does NOT work - the script
# blocks, so the browser never starts, and it would also run as root while
# Chrome refuses to be root-owned-desktop-user.
#
# Correct approach: background the browser supervisor, then exec the Kasm
# startup in the foreground as PID 1.
set -u

# A named volume is mounted over /home/kasm-user (task doc 6.3: desktop-home
# must be persistent). On FIRST start that volume is empty and hides the
# image's prepared .Xauthority / .vnc / .kasmpasswd state, which makes
# vnc_startup.sh die with "xauth: timeout in locking authority file" and the
# container enter a restart loop. Seed it from the pristine copy in the image
# only when it is empty, so subsequent starts keep real user data.
if [ -d /opt/niucai/home-seed ] && [ -z "$(ls -A /home/kasm-user 2>/dev/null)" ]; then
  echo "[entrypoint] seeding empty desktop-home from image snapshot"
  cp -a /opt/niucai/home-seed/. /home/kasm-user/
fi

# MEASURED PITFALL: Chrome binds CDP to 127.0.0.1 ONLY and ignores
# --remote-debugging-address (reproduced on Chrome 141). A relay running in a
# SEPARATE container therefore gets "Connection refused" when it dials
# computer:9222, because that resolves to the container IP, not loopback.
# => the relay must live in THIS container, sharing Chrome's loopback.
RELAY_LISTEN_PORT="${RELAY_LISTEN_PORT:-9223}"
RELAY_PUBLIC_HOSTPORT="${RELAY_PUBLIC_HOSTPORT:-computer:${RELAY_LISTEN_PORT}}"
RELAY_UP_HOST=127.0.0.1
RELAY_UP_PORT="$CDP_PORT"
export RELAY_LISTEN_PORT RELAY_PUBLIC_HOSTPORT RELAY_UP_HOST RELAY_UP_PORT

/opt/niucai/browser-launch.sh >>/tmp/browser-launch.out 2>&1 &
echo "[entrypoint] browser supervisor backgrounded pid=$!"

# Invoke via python rather than executing the file directly: /relay is a
# bind mount from the host, so a lost executable bit (e.g. after the file is
# rewritten on the host) would otherwise kill the relay with
# "/relay/cdp-relay.py: Permission denied" and silently break CDP for the Worker.
python3 /relay/cdp-relay.py >>/tmp/cdp-relay.out 2>&1 &
echo "[entrypoint] cdp relay backgrounded pid=$!"

exec /dockerstartup/vnc_startup.sh "$@"
