#!/usr/bin/env python3
"""nets.py — run one bot.py per IRC network, vjt-style.

The reference runs three IRC networks as three `bot.py` processes: same code,
per-network env and files, zero shared state ("a second network is a second
process with its own env, not a threading model inside the bot"). This
supervisor is that, mechanised: networks.py derives each network's env from
IRC_NETWORKS + networks.json, and nets.py spawns and babysits one bot.py per
network — restarting dead children with a growing backoff so a netsplit does
not become a reconnect flood.

On SIGTERM it sends `QUIT` through every FIFO first and gives the bots a
grace period, so a stop parts cleanly instead of ping-timing-out (the
ExecStop behaviour the reference ships as a systemd snippet).

Usage: nets.py  (env: IRC_NETWORKS="azzurra libera", networks.json overrides)
"""
import os
import signal
import subprocess
import sys
import time
from functools import partial

import networks
from util import emit as _emit, env

emit = partial(_emit, "NETS")

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT = os.path.join(HERE, "bot.py")
RESTART_SEC = float(env("NETS_RESTART_SEC", "30"))
RESTART_MAX = float(env("NETS_RESTART_MAX", "1800"))
QUIT_GRACE = float(env("NETS_QUIT_GRACE", "10"))


class NetBot:
    """One bot.py child for one network, with its own restart backoff."""

    def __init__(self, net):
        self.net = net
        self.proc = None
        self.restarts = 0
        self.started_at = None

    def spawn(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", BOT_SCRIPT],
            cwd=HERE, env=self.net.env)
        self.started_at = time.monotonic()
        emit("SPAWN", self.net.name, f"pid={self.proc.pid}",
             f"events={self.net.events}", f"fifo={self.net.fifo}")

    def poll(self):
        """Restart with backoff when the child died; True when (re)started."""
        if self.proc is None or self.proc.poll() is None:
            return False
        code = self.proc.returncode
        # Ran over a minute: it was a real run, not a crash loop.
        if self.started_at and time.monotonic() - self.started_at > 60:
            self.restarts = 0
        delay = min(RESTART_SEC * (2 ** min(self.restarts, 6)), RESTART_MAX)
        self.restarts += 1
        emit("CHILD_EXIT", self.net.name, f"code={code}",
             f"restart in {delay:.0f}s (#{self.restarts})")
        time.sleep(delay)
        self.spawn()
        return True


def quit_all(nets, reason="bye"):
    """Best-effort clean part: `QUIT` through each FIFO, then a grace wait.

    A FIFO with no reader blocks open() — hence O_NONBLOCK + swallow, exactly
    like the brain's writer. If the bot is down the QUIT is moot anyway.
    """
    for net in nets:
        try:
            fd = os.open(net.fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            os.write(fd, f"QUIT {reason}\n".encode())
        except OSError:
            pass
        finally:
            os.close(fd)
    time.sleep(QUIT_GRACE)


def main():
    nets = networks.load()
    emit("START", f"{len(nets)} network(s):",
         " ".join(f"{n.name}({n.env.get('IRC_HOST', '?')})" for n in nets))
    bots = [NetBot(n) for n in nets]
    for b in bots:
        b.spawn()

    stopping = []

    def _term(*_):
        if stopping:
            return
        stopping.append(True)
        emit("TERM", "sending QUIT through every FIFO")
        quit_all(nets)
        for b in bots:
            if b.proc and b.proc.poll() is None:
                b.proc.terminate()
        for b in bots:
            try:
                b.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                b.proc.kill()
        emit("BYE")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    while True:
        time.sleep(1)
        for b in bots:
            b.poll()


if __name__ == "__main__":
    main()
