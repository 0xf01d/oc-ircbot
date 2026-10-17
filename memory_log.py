#!/usr/bin/env python3
"""memory_log.py — the disk-backed memory the persistent session boots from:
MEMORY.md (the index, auto-injected into every session), the rolling 14-day
activity log and its archive. The promotion rule (>14 days of relevance =>
a typed file user_* / feedback_* / project_* / reference_*, one index line)
lives in the seed index; the 14-day archive trim runs in code at brain start
so it never depends on the model remembering to do it."""
import os
import re
from datetime import date, datetime, timedelta

MEMORY_INDEX = "MEMORY.md"
ACTIVITY_LOG = "project_activity_log.md"
ACTIVITY_ARCHIVE = "project_activity_log_archive.md"

DEFAULT_INDEX_BODY = """\
# Memory index

One line per memory file; details live in the files, not here.

<!-- agent: add one line here whenever you promote something to a memory
     file. Anything that matters longer than 14 days does NOT stay in the
     activity log — promote it to a typed file (user_*, feedback_*,
     project_*, reference_*) and index it. -->
"""

_HEAD = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


def today_heading(day=None):
    d = day or date.today()
    return f"## {d.isoformat()}"


class Memory:
    """The memory root. All methods are filesystem-plain and testable."""

    def __init__(self, root):
        self.root = root

    def path(self, name):
        return os.path.join(self.root, name)

    def ensure(self, today=None):
        """Create the root, seed the index and today's heading. Idempotent."""
        os.makedirs(self.root, exist_ok=True)
        idx = self.path(MEMORY_INDEX)
        if not os.path.exists(idx):
            with open(idx, "w") as f:
                f.write(DEFAULT_INDEX_BODY)
        log = self.path(ACTIVITY_LOG)
        body = ""
        try:
            with open(log) as f:
                body = f.read()
        except FileNotFoundError:
            pass
        head = today_heading(today)
        if head not in body:
            with open(log, "a") as f:
                if body and not body.endswith("\n"):
                    f.write("\n")
                f.write(f"{head}\n\n")

    def read(self, name, default=""):
        try:
            with open(self.path(name)) as f:
                return f.read()
        except FileNotFoundError:
            return default

    def index_text(self):
        return self.read(MEMORY_INDEX)

    def append_activity(self, line):
        """One line into today's heading of the activity log."""
        self.ensure()
        with open(self.path(ACTIVITY_LOG), "a") as f:
            f.write(f"- {line}\n")

    def archive_older_than(self, days=14, today=None):
        """Move day-headings older than `days` into the archive file.
        Returns (archived_days, kept_days). Content before the first heading
        is kept as-is at the top of the log."""
        log_path = self.path(ACTIVITY_LOG)
        try:
            with open(log_path) as f:
                body = f.read()
        except FileNotFoundError:
            return (0, 0)
        if not body.strip():
            return (0, 0)
        matches = list(_HEAD.finditer(body))
        if not matches:
            return (0, 0)

        # Split into (heading_date_or_None, block_text), preserving order.
        blocks = []
        pre = body[:matches[0].start()]
        if pre.strip():
            blocks.append((None, pre))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            blocks.append((m.group(1), body[m.start():end]))

        cutoff = (today or date.today()) - timedelta(days=days)
        kept, gone = [], []
        for head_day, text in blocks:
            if head_day is not None and date.fromisoformat(head_day) < cutoff:
                gone.append(text.rstrip("\n"))
            else:
                kept.append(text if text.endswith("\n") else text + "\n")

        head = today_heading(today)
        if not any(b.startswith(head) for b in kept):
            kept.append(f"{head}\n\n")

        with open(log_path, "w") as f:
            f.writelines(kept)
        if gone:
            with open(self.path(ACTIVITY_ARCHIVE), "a") as f:
                f.write("\n".join(gone).rstrip("\n") + "\n")
        return (len(gone), len(kept))

    def today_body(self, today=None):
        """Today's activity-log block (without the heading), for rehydrate."""
        body = self.read(ACTIVITY_LOG)
        head = today_heading(today)
        i = body.find(head)
        if i < 0:
            return ""
        rest = body[i + len(head):]
        m = _HEAD.search(rest)
        return rest[:m.start()] if m else rest

    def log_compaction(self, when=None):
        """Record a compaction so the activity log always explains gaps."""
        ts = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
        self.append_activity(f"session compacted (auto) at {ts}")
