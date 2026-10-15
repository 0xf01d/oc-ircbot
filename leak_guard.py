#!/usr/bin/env python3
"""leak_guard.py — reserved-content leak gate for bot.py.

The original repo ships this module; the `.reference/` copy here does not
include it, so this is an equivalent re-implementation of the same contract
that bot.py relies on:

    g = LeakGuard(host, reserved_file, store_file)
    g.record(target, body)          # remember a line that entered the bridge
    reason = g.check(target, text, dm_ok=False)   # None if the send is fine

Model: a channel (or nick) is *reserved* when it appears in `bot.reserved`.
Everything said in a reserved channel is private context: it may be read and
reasoned about, but it must not be paraphrased back onto the open network.
The gate therefore stores the recent reserved lines and, on the way out,
refuses an outbound line to a NON-reserved target when it shares a verbatim
run (>= MIN_RUN characters) with a line that was recorded in a reserved
channel. A DM to a reserved target itself (dm_ok) is always allowed, and a
line addressed *to* a reserved channel is never a leak.

The original's exact heuristic is not published; this keeps the same shape
(files, names, API, on/off via an empty file) and is deliberately boring:
stdlib only, bounded memory, no framework.
"""
import fnmatch
import os
import threading
import time

# A shared run of this many characters between an outgoing line and a stored
# reserved line is treated as a leak. Configurable; keep it high enough that
# ordinary IRC chatter (greetings, single words) can never trip it.
MIN_RUN = int(os.environ.get("BOT_RESERVED_MINRUN", "40"))
# Bounded memory: keep at most this many reserved lines, each truncated.
MAX_LINES = int(os.environ.get("BOT_RESERVED_MAX", "200"))
LINE_CAP = 512
# Drop stored lines older than this many seconds (0 = never expire).
MAX_AGE = float(os.environ.get("BOT_RESERVED_TTL", "86400"))


class LeakGuard:
    def __init__(self, host, reserved_file, store_file):
        self.host = host
        self.reserved_file = reserved_file
        self.store_file = store_file
        self.lock = threading.Lock()
        self.globs = []       # fnmatch patterns from bot.reserved
        self.lines = []       # list of (ts, target_lower, body)
        self._load_reserved()

    def _load_reserved(self):
        try:
            with open(self.reserved_file) as f:
                for line in f:
                    line = line.strip()
                    # A comment is a bare '#', or '#' followed by whitespace.
                    # A channel glob like '#brain-private' has no space, so it
                    # is NOT mistaken for a comment (nicks never start with #).
                    if not line or line == "#" or line.startswith("# "):
                        continue
                    self.globs.append(line.lower())
        except FileNotFoundError:
            pass

    def _is_reserved(self, target):
        t = (target or "").lower()
        if not t:
            return False
        for g in self.globs:
            if t == g or fnmatch.fnmatch(t, g):
                return True
        return False

    def record(self, target, body):
        """Remember a line seen in a reserved channel (no-op otherwise)."""
        if not body or not self._is_reserved(target):
            return
        now = time.monotonic()
        with self.lock:
            self.lines.append((now, (target or "").lower(), body[:LINE_CAP]))
            if len(self.lines) > MAX_LINES:
                del self.lines[:-MAX_LINES]
            self._expire_locked(now)
            self._store_locked()

    def _expire_locked(self, now):
        if MAX_AGE <= 0:
            return
        self.lines = [ln for ln in self.lines if now - ln[0] <= MAX_AGE]

    def _store_locked(self):
        # Best-effort private state next to the bot; never fatal.
        try:
            tmp = self.store_file + ".tmp"
            with open(tmp, "w") as f:
                for _ts, target, body in self.lines:
                    f.write(f"{target}\t{body}\n")
            os.replace(tmp, self.store_file)
        except OSError:
            pass

    def check(self, target, text, dm_ok=False):
        """Return a reason string when `text` must not go to `target`, else None.

        A send to a reserved target, or a DM to a reserved nick, is fine.
        """
        if not text:
            return None
        if self._is_reserved(target) or dm_ok:
            return None
        needle = text
        with self.lock:
            self._expire_locked(time.monotonic())
            for _ts, src, body in self.lines:
                if self._shared_run(needle, body):
                    return f"reserved-content:{src}"
        return None

    @staticmethod
    def _shared_run(a, b):
        """True if a and b share a verbatim run of >= MIN_RUN characters."""
        if len(a) < MIN_RUN or len(b) < MIN_RUN:
            return False
        # Bounded: a and b are capped at LINE_CAP, so the scan stays cheap.
        for i in range(len(a) - MIN_RUN + 1):
            if a[i:i + MIN_RUN] in b:
                return True
        return False
