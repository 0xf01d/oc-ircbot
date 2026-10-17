#!/usr/bin/env python3
"""tmuxsup.py — run a service inside a detached tmux session, vjt-style.

The container's main process is this tiny supervisor: it starts
`tmux new -d -s <name> <command>` and restarts the session when it dies, so
the process is always live-viewable from outside:

    docker compose exec brain tmux attach -t brain          # full terminal
    docker compose exec brain tmux capture-pane -p -t brain | tail   # peek

Pane output is also duplicated to the container's stdout (`tmux pipe-pane`
into /proc/1/fd/1), so `docker logs` keeps showing what it showed before
tmux — the session is the interactive window, not the only record.

Crash-loop guard: five deaths within five minutes suspend the restarts —
the container stays up with a loud, periodic CRASH_LOOP marker in the log.
SIGTERM/SIGINT go to the pane process, so each service's own clean-shutdown
path still runs (nets.py parts with QUIT, brain.py sets its stop flag).
"""
import os
import signal
import subprocess
import sys
import time
from collections import deque
from functools import partial

from util import emit as _emit, env

emit = partial(_emit, "TMUXSUP")

POLL = float(env("TMUXSUP_POLL", "1.0"))
RESTART_SEC = float(env("TMUXSUP_RESTART_SEC", "1.0"))
CRASH_WINDOW = float(env("TMUXSUP_CRASH_WINDOW", "300"))
CRASH_MAX = int(env("TMUXSUP_CRASH_MAX", "5"))
STOP_GRACE = float(env("TMUXSUP_STOP_GRACE", "15"))

USAGE = "usage: tmuxsup.py SESSION COMMAND [ARG ...]"


def crash_decision(crashes, now, window=CRASH_WINDOW, max_crashes=CRASH_MAX):
    """Record a death at `now`; 'suspend' at max_crashes within the window.

    `crashes` is pruned in place, so a single survivor outside the window
    resets the count — five crashes in five minutes, not five ever.
    """
    crashes.append(now)
    while crashes and now - crashes[0] > window:
        crashes.popleft()
    return "suspend" if len(crashes) >= max_crashes else "restart"


class Supervisor:
    def __init__(self, session, command):
        self.session = session
        self.command = list(command)
        self.stopping = []

    def tmux(self, *args, **kw):
        return subprocess.run(["tmux", *args], **kw)

    def alive(self):
        return self.tmux("has-session", "-t", self.session,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL).returncode == 0

    def start(self):
        self.tmux("new-session", "-d", "-s", self.session, *self.command,
                  check=True)
        # Pane output -> container stdout too (fd 1 of PID 1 is what
        # `docker logs` reads). The pane itself still shows everything.
        self.tmux("pipe-pane", "-o", "-t", self.session,
                  "cat >> " + env("TMUXSUP_LOG", "/proc/1/fd/1"), check=True)

    def pane_pid(self):
        r = self.tmux("list-panes", "-t", self.session, "-F", "#{pane_pid}",
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                      text=True)
        out = (r.stdout or "").strip().splitlines()
        return int(out[0]) if out and out[0].isdigit() else None

    def shutdown(self):
        """SIGTERM: the service's own handler does the clean part."""
        pid = self.pane_pid()
        if pid:
            emit("TERM", f"SIGTERM -> pane pid {pid}", f"grace {STOP_GRACE:.0f}s")
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.monotonic() + STOP_GRACE
            while self.alive() and time.monotonic() < deadline:
                time.sleep(0.2)
        self.tmux("kill-server", stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL)
        emit("BYE")
        sys.exit(0)

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.append(True))
        signal.signal(signal.SIGINT, lambda *_: self.stopping.append(True))
        self.start()
        emit("START", self.session, " ".join(self.command))
        crashes = deque()
        suspended_at = None
        while True:
            if self.stopping:
                self.shutdown()
            if suspended_at is None and not self.alive():
                verdict = crash_decision(crashes, time.monotonic())
                if verdict == "suspend":
                    emit("CRASH_LOOP",
                         f"{CRASH_MAX} crashes in {CRASH_WINDOW:.0f}s —"
                         " restarts suspended, container stays up")
                    suspended_at = time.monotonic()
                else:
                    emit("SESSION_EXIT",
                         f"restarting in {RESTART_SEC:.0f}s"
                         f" ({len(crashes)}/{CRASH_MAX}"
                         f" in {CRASH_WINDOW:.0f}s)")
                    time.sleep(RESTART_SEC)
                    self.start()
            elif suspended_at is not None:
                # A session started by hand (exec + tmux new) lifts the
                # suspension: someone is driving, watch over their shoulder.
                if self.alive():
                    emit("SESSION_BACK", f"{self.session} is up — resuming")
                    suspended_at = None
                    crashes.clear()
                elif time.monotonic() - suspended_at >= 60:
                    emit("CRASH_LOOP", f"{self.session} still down,"
                         " restarts remain suspended")
                    suspended_at = time.monotonic()
                time.sleep(POLL)
            else:
                time.sleep(POLL)


def main(argv):
    if len(argv) < 3:
        print(USAGE, file=sys.stderr)
        return 2
    Supervisor(argv[1], argv[2:]).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
