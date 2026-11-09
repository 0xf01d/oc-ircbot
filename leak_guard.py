#!/usr/bin/env python3
"""leak_guard.py — reserved-content leak gate for bot.py.

A channel (or nick) in `bot.reserved` is *reserved*: what is said there may
be read and reasoned about, but never paraphrased back onto the open network.
The gate records recent reserved lines and refuses an outbound line to a
NON-reserved target when it shares a verbatim run (>= MIN_RUN chars) with a
recorded reserved line. Sends to a reserved target, or a DM to one, are fine.
"""
import fnmatch
import os
import threading
import time

MIN_RUN = 40       # shared-run length that counts as a leak
MAX_LINES = 200    # bounded memory: reserved lines kept, each truncated
MAX_AGE = 86400.0  # drop stored lines older than this (0 = never)
LINE_CAP = 512


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
                    # '#' followed by whitespace is a comment; '#brain-p'
                    # is a channel glob (nicks never start with #).
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
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.store_file)
        except OSError:
            pass

    def check(self, target, text, dm_ok=False):
        """Reason string when `text` must not go to `target`, else None."""
        if not text:
            return None
        if self._is_reserved(target) or dm_ok:
            return None
        with self.lock:
            self._expire_locked(time.monotonic())
            for _ts, src, body in self.lines:
                if self._shared_run(text, body):
                    return f"reserved-content:{src}"
        return None

    @staticmethod
    def _shared_run(a, b):
        """True if a and b share a verbatim run of >= MIN_RUN characters."""
        if len(a) < MIN_RUN or len(b) < MIN_RUN:
            return False
        for i in range(len(a) - MIN_RUN + 1):
            if a[i:i + MIN_RUN] in b:
                return True
        return False
