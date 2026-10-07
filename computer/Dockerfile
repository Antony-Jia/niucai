# niucai Computer image.
#
# Base choice is a DEPLOYMENT DECISION, not a verified niucai image:
#   kasmweb/desktop:1.18.0  digest sha256:9a94316372b8858d0238134ba507812e867fcb903d0db599c8cfd9ff8bcd0bfb
# Measured on this host (CentOS 7, kernel 3.10.0-1160, cgroup v1):
#   - container runs fine; inside is Ubuntu 22.04 / glibc 2.35
#   - Google Chrome 141.0.7390.107 at /opt/google/chrome/google-chrome
#   - Xvnc :1 + XFCE4 + KasmVNC on :6901 all start
#   - non-headless Chrome with --remote-debugging-port works on DISPLAY=:1
#   - Chrome binds CDP to 127.0.0.1 ONLY, hence the cdp-relay sidecar
FROM kasmweb/desktop:1.18.0

USER root

COPY --chown=root:root browser-launch.sh /opt/niucai/browser-launch.sh
COPY --chown=root:root healthcheck.sh    /opt/niucai/healthcheck.sh
COPY --chown=root:root entrypoint.sh     /opt/niucai/entrypoint.sh
RUN chmod 0755 /opt/niucai/browser-launch.sh /opt/niucai/healthcheck.sh /opt/niucai/entrypoint.sh \
 && mkdir -p /browser-profile /downloads /workspace \
 && chown -R 1000:1000 /browser-profile /downloads /workspace \
 # Seed a pristine copy of the desktop user's home. A fresh named volume over
 # /home/kasm-user would otherwise be EMPTY and hide the .Xauthority/.vnc
 # state the image prepared, which makes vnc_startup.sh fail with
 # "xauth: timeout in locking authority file" and exit-restart-loop.
 && mkdir -p /opt/niucai/home-seed \
 && cp -a /home/kasm-user/. /opt/niucai/home-seed/ 2>/dev/null || true

# CDP is NOT exposed on a mapped host port on purpose: it has no auth.
# It stays inside the project Docker network; cdp-relay fronts it.

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD /opt/niucai/healthcheck.sh

USER root
ENTRYPOINT ["/opt/niucai/entrypoint.sh"]
