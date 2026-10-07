#!/usr/bin/env python3
"""
CDP relay for niucai Computer.

Why this exists (measured fact, 2026-10-06):
  Chrome 141 ignores --remote-debugging-address and only listens on 127.0.0.1
  (by design, for Chrome security). So a Worker container cannot reach the
  browser directly. This relay listens on 0.0.0.0:9223 inside the project
  Docker network, forwards HTTP + WebSocket to the browser's loopback port,
  and REWRITES the discovery JSON so that webSocketDebuggerUrl points at a
  name the remote client can actually resolve.

  Without the rewrite, connect_over_cdp fails at the WebSocket step with
  getaddrinfo ENOTFOUND even though /json/version returned HTTP 200.
  That exact failure was reproduced and is why this rewrite is not optional.

Env:
  RELAY_UP_HOST / RELAY_UP_PORT  browser loopback endpoint (127.0.0.1:9222)
  RELAY_LISTEN_HOST / RELAY_LISTEN_PORT  listen addr (0.0.0.0:9223)
  RELAY_PUBLIC_HOSTPORT    host:port advertised in rewritten ws URLs
                           (e.g. "computer:9223"); if empty, no rewrite.
"""
import asyncio
import os
import re

UP_HOST = os.environ.get("RELAY_UP_HOST", "127.0.0.1")
UP_PORT = int(os.environ.get("RELAY_UP_PORT", "9222"))
LISTEN_HOST = os.environ.get("RELAY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("RELAY_LISTEN_PORT", "9223"))
PUBLIC_HOSTPORT = os.environ.get("RELAY_PUBLIC_HOSTPORT", "").strip()

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade"}


async def _pipe(r, w):
    try:
        while True:
            d = await r.read(65536)
            if not d:
                break
            w.write(d)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


def _rewrite(body: bytes) -> bytes:
    if not PUBLIC_HOSTPORT:
        return body
    try:
        s = body.decode()
    except Exception:
        return body
    for host in ("127.0.0.1", "localhost", "[::1]"):
        s = re.sub(r"ws://%s:%d/" % (re.escape(host), UP_PORT),
                   "ws://%s/" % PUBLIC_HOSTPORT, s)
    return s.encode()


async def _handle(cr, cw):
    try:
        head = await cr.readuntil(b"\r\n\r\n")
    except Exception:
        cw.close()
        return
    lines = head.decode("latin1").split("\r\n")
    parts = lines[0].split()
    if len(parts) < 2:
        cw.close()
        return
    method, path = parts[0], parts[1]

    hdrs = []
    is_ws = False
    # First pass: detect a WebSocket upgrade BEFORE filtering hop-by-hop
    # headers (Connection/Upgrade may appear before Upgrade in the request).
    for line in lines[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        if k.strip().lower() == "upgrade" and v.strip().lower() == "websocket":
            is_ws = True
            break

    for line in lines[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        kl, vl = k.strip().lower(), v.strip()
        if kl == "host":
            continue  # rewrite Host for the browser's own validation
        if kl in _HOP and not is_ws:
            continue
        if kl == "origin":
            continue  # Chrome rejects cross-origin WS; drop it
        hdrs.append("%s: %s" % (k.strip(), vl))

    if is_ws:
        # Chrome validates the Host header on the upgrade; keep it loopback.
        hdrs.append("Host: %s:%d" % (UP_HOST, UP_PORT))
    else:
        hdrs.append("Host: %s:%d" % (UP_HOST, UP_PORT))

    try:
        ur, uw = await asyncio.open_connection(UP_HOST, UP_PORT)
    except Exception:
        cw.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        await cw.drain()
        cw.close()
        return

    uw.write(("%s %s HTTP/1.1\r\n" % (method, path)).encode()
             + ("\r\n".join(hdrs) + "\r\n\r\n").encode())
    await uw.drain()

    try:
        rhead = await ur.readuntil(b"\r\n\r\n")
    except Exception:
        cw.close()
        uw.close()
        return

    status_line = rhead.split(b"\r\n", 1)[0]
    if b"101" in status_line:
        cw.write(rhead)
        await cw.drain()
        await asyncio.gather(_pipe(ur, cw), _pipe(cr, uw))
        return

    m = re.search(rb"content-length:\s*(\d+)", rhead, re.I)
    if m:
        n = int(m.group(1))
        body = await ur.readexactly(n) if n else b""
    else:
        body = await ur.read()
    try:
        uw.close()
    except Exception:
        pass

    body = _rewrite(body)
    rhead = re.sub(rb"content-length:\s*\d+",
                   b"content-length: %d" % len(body), rhead, flags=re.I)
    cw.write(rhead)
    cw.write(body)
    await cw.drain()
    cw.close()


async def main():
    srv = await asyncio.start_server(_handle, LISTEN_HOST, LISTEN_PORT)
    print("cdp-relay %s:%d -> %s:%d public=%s"
          % (LISTEN_HOST, LISTEN_PORT, UP_HOST, UP_PORT,
             PUBLIC_HOSTPORT or "(no rewrite)"), flush=True)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
