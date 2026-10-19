#!/usr/bin/env python3
"""serve_bind_check.py — assert the REAL `opencode serve` binds loopback only.

Run inside a container that has the opencode CLI (the brain image; CI does
`docker run --rm oc-ircbot-brain python3 /app/tests/serve_bind_check.py`).
Spawns the brain's managed Serve, then reads /proc/net/tcp{,6}: every LISTEN
socket on the serve port must be 127.0.0.1 (or ::1) — nothing may bind
0.0.0.0, ::, or a routable address. Exit 0 + SERVE_BIND_OK on success.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain  # noqa: E402

V4_LOOPBACK = "0100007f"        # /proc/net/tcp local_address, little-endian
V6_LOOPBACK = "0" * 28 + "1"    # /proc/net/tcp6 ::1


def listen_locals(port):
    """Local IPs of every LISTEN socket on `port`, as /proc hex strings."""
    out = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(path).read().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            f = ln.split()
            if f[3] != "0A":                      # 0A = TCP_LISTEN
                continue
            ip_hex, port_hex = f[1].split(":")
            if int(port_hex, 16) == port:
                out.append(ip_hex.lower())
    return out


def main():
    os.makedirs(brain.WORKDIR, exist_ok=True)   # Serve.spawned runs in there
    serve = brain.Serve.spawned()
    try:
        assert serve.url.startswith("http://127.0.0.1:"), \
            f"serve URL is not loopback: {serve.url}"
        locals_ = listen_locals(serve.port)
        assert locals_, f"nothing is LISTENing on port {serve.port}"
        bad = sorted({ip for ip in locals_
                      if ip not in (V4_LOOPBACK, V6_LOOPBACK)})
        assert not bad, \
            f"non-loopback LISTEN on {serve.port}: {bad} " \
            f"(0.0.0.0 is {'00000000'!r}, :: is {'0' * 32!r})"
        print(f"SERVE_BIND_OK {serve.url} "
              f"listeners={sorted(set(locals_))}")
    finally:
        serve.stop()


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, RuntimeError) as e:
        print(f"SERVE_BIND_FAIL {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
