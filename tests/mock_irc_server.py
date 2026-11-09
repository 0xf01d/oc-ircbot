#!/usr/bin/env python3
"""mock_irc_server.py — a tiny plaintext IRC server for the smoke tests.

Stdlib only. It speaks just enough IRC for bot.py to register:

  * completes registration (001) after NICK+USER,
  * injects one canned channel PRIVMSG so the brain has something to answer,
  * echoes PING/PONG,
  * appends every client PRIVMSG to --out as `<target> <body>`.

It exits 0 as soon as a client PRIVMSG contains --expect (the assertion the
smoke test wants), or non-zero on --timeout with nothing seen.

Used by CI (docker compose) and by the on-cluster smoke test (k8s/).
"""
import argparse
import socket
import sys
import time


def handle(conn, nick_box, inject, out_path, expect):
    f = conn.makefile("rwb", buffering=0)
    nick = "bot"

    def send(line):
        f.write((line + "\r\n").encode())

    while True:
        raw = f.readline()
        if not raw:
            return
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        print(f"< {line}", flush=True)
        if not line:
            continue
        if line.upper().startswith("NICK "):
            nick = line.split(" ", 1)[1].strip()
            nick_box["nick"] = nick
        elif line.upper().startswith("USER "):
            send(f":mock 001 {nick} :Welcome to the mock IRC server")
            if inject:
                time.sleep(0.5)
                send(f":alice!u@h PRIVMSG {inject.split(' ', 1)[0]} "
                     f":{inject.split(' ', 1)[1]}")
        elif line.upper().startswith("PING "):
            send("PONG " + line[5:])
        elif line.upper().startswith("PRIVMSG "):
            rest = line.split(" ", 1)[1]
            target, _, body = rest.partition(" :")
            with open(out_path, "a") as o:
                o.write(f"{target} {body}\n")
            print(f">> recorded: {target} {body}", flush=True)
            if expect and expect in body:
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=6667)
    ap.add_argument("--inject", default="", help="'#chan text' to inject")
    ap.add_argument("--out", default="/tmp/mock_irc.out")
    ap.add_argument("--expect", default="", help="substring that means success")
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((a.host, a.port))
    srv.listen(1)
    srv.settimeout(a.timeout)
    print(f"mock IRC listening on {a.host}:{a.port}", flush=True)
    deadline = time.monotonic() + a.timeout
    nick_box = {"nick": "bot"}
    try:
        while time.monotonic() < deadline:
            try:
                conn, peer = srv.accept()
            except socket.timeout:
                break
            print(f"connection from {peer}", flush=True)
            done = handle(conn, nick_box, a.inject, a.out, a.expect)
            conn.close()
            if done:
                print("EXPECTED PRIVMSG SEEN", flush=True)
                return 0
        print("TIMEOUT: expected PRIVMSG not seen", flush=True)
        return 1
    finally:
        srv.close()


if __name__ == "__main__":
    sys.exit(main())
