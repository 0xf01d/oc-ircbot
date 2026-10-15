#!/usr/bin/env python3
"""brain.py — the OpenCode substitution for Claude Code's Monitor.

The original claude-ircbot let a Claude Code session attach to bot.py's
stdout with the Monitor tool: every structured event line became a
notification mid-conversation, and the agent wrote `SAY #chan text` back into
the bot.send FIFO.

This is the same loop, minus Claude Code:

    tail BOT_EVENTS -> batch + per-channel ring buffer -> prompt
        -> `opencode run --auto --model <model> <prompt>` -> parse `SAY ...`
        -> write into BOT_FIFO

Design constraints it keeps from the original: stdlib only, one small file,
bounded memory, no framework. New here: a timeout on every model call, a
heartbeat file the watchdog sidecar reads, structured logs on stdout, and a
`--dry-run` mode that prints the prompt it *would* send instead of invoking
the model.

Contract (documented in README.md): the model must answer with zero or more
lines of the form

    SAY <target> <text>

one per PRIVMSG. Anything the model says that is not such a line is ignored.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections import OrderedDict, deque
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))


def _cfg(key, default):
    return os.environ.get(key, default)


def _bool(key, default=False):
    v = os.environ.get(key)
    if v is None:
        return default
    return v not in ("0", "no", "off", "false", "")


# --- config -----------------------------------------------------------------
EVENTS_FILE = _cfg("BOT_EVENTS", "/data/events.log")
FIFO = _cfg("BOT_FIFO", "/data/bot.send")
HEARTBEAT = _cfg("BRAIN_HEARTBEAT", "/data/brain.heartbeat")
NICK = _cfg("IRC_NICK", "vjt-claude")          # our own nick; never answer it
MODEL = _cfg("BRAIN_MODEL", "opencode/deepseek-v4.1-flash-free")
OPENCODE_BIN = _cfg("BRAIN_OPENCODE_BIN", "opencode")
# Stub seam: when set, this command is run (prompt on stdin) instead of
# opencode. tests/ and the compose smoke test use it to stay offline.
OC_BRAIN_CMD = _cfg("OC_BRAIN_CMD", "")
POLL = float(_cfg("BRAIN_POLL", "1.0"))
BATCH_MAX = int(_cfg("BRAIN_BATCH_MAX", "5"))
BATCH_WINDOW = float(_cfg("BRAIN_BATCH_WINDOW", "3.0"))
RING = int(_cfg("BRAIN_RING", "8"))
MAX_CHANS = int(_cfg("BRAIN_MAX_CHANS", "32"))
TIMEOUT = float(_cfg("BRAIN_TIMEOUT", "120"))
RATE = float(_cfg("BRAIN_RATE", "3.0"))          # min seconds between sends/channel
GLOBAL_RATE = float(_cfg("BRAIN_GLOBAL_RATE", "1.0"))
FROM_START = _bool("BRAIN_FROM_START", False)
DRY_RUN = _bool("BRAIN_DRY_RUN", False)
API_KEY = _cfg("OPENCODE_API_KEY", "")

SYSTEM = _cfg(
    "BRAIN_SYSTEM",
    "You are an IRC participant. You see recent channel context and new "
    "events. Reply with zero or more lines of the form `SAY <target> <text>` "
    "(one PRIVMSG each). Use the channel from the event as <target>. Keep it "
    "short and human. If nothing warrants a reply, output nothing.",
)

_TZ = ZoneInfo("Europe/Rome")
# Kinds that are worth waking the model for. Everything else only feeds the
# per-channel ring buffer as context.
ACTIONABLE = {"MSG", "INVITE", "IDLE", "CTCP", "KICK", "NOTICE"}
# Strip ANSI colour codes opencode emits in its formatted output.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SAY = re.compile(r"^SAY\s+(\S+)\s+(.+)$")
_EVENT = re.compile(r"^\[(\d{2}:\d{2}(?::\d{2})?)\]\s+([A-Z_]+)\s*(.*)$")


def emit(kind, *parts):
    line = f"[{datetime.now(_TZ).strftime('%H:%M:%S')}] BRAIN {kind} " + \
        " ".join(str(p) for p in parts)
    print(line, flush=True)


def install_api_key():
    """Write the OpenCode auth file from OPENCODE_API_KEY, if given.

    Path matches the CLI's own store (~/.local/share/opencode/auth.json), the
    format documented in the README: {"opencode":{"type":"api","key":"..."}}.
    """
    if not API_KEY:
        return
    d = os.path.join(os.path.expanduser("~"), ".local", "share", "opencode")
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "auth.json")
        with open(path, "w") as f:
            json.dump({"opencode": {"type": "api", "key": API_KEY}}, f)
        os.chmod(path, 0o600)
        emit("AUTH_WRITTEN", path)
    except OSError as e:
        emit("AUTH_ERROR", repr(e))


# --- event parsing ----------------------------------------------------------
def parse_event(line):
    """Parse one bot.py event line into a dict, or None if unrecognised."""
    m = _EVENT.match(line.strip())
    if not m:
        return None
    ts, kind, rest = m.groups()
    ev = {"ts": ts, "kind": kind, "raw": line.strip()}
    for key in ("FROM", "HOST", "TO"):
        mm = re.search(rf"(?:^|\s){key}=([^\s]*)", rest)
        if mm:
            ev[key.lower()] = mm.group(1)
    mm = re.search(r"BODY=(.*)$", rest)
    if mm:
        ev["body"] = mm.group(1)
    if kind == "MSG":
        mt = re.match(r"(TRUSTED|UNTRUSTED)\b", rest)
        if mt:
            ev["trust"] = mt.group(1)
    if "to" not in ev and kind in ("IDLE", "KICK", "PART", "JOIN", "INVITE"):
        mm = re.search(r"[#&][^\s]+", rest)
        if mm:
            ev["to"] = mm.group(0)
    return ev


def fmt_event(ev):
    """One human/model-readable line for the prompt."""
    ts = ev.get("ts", "??:??")
    kind = ev.get("kind", "?")
    if kind == "MSG":
        return (f"[{ts}] <{ev.get('from','?')}> to {ev.get('to','?')} "
                f"({ev.get('trust','?').lower()}): {ev.get('body','')}")
    if kind == "NOTICE":
        return f"[{ts}] -{ev.get('from','?')}- notice: {ev.get('body','')}"
    if kind == "IDLE":
        return f"[{ts}] (idle) {ev.get('to','?')}"
    if kind == "CTCP":
        return f"[{ts}] ctcp from {ev.get('from','?')}: {ev.get('body','')}"
    if kind == "JOIN":
        return f"[{ts}] * {ev.get('from','')} {ev.get('raw','')}"
    return f"[{ts}] {ev.get('raw','')}"


def prompts_of(ev):
    """The channel an event belongs to, for ring-buffer bucketing."""
    tgt = ev.get("to") or ""
    if tgt.startswith(("#", "&")):
        return tgt.lower()
    src = ev.get("from") or ""
    if src.startswith(("#", "&")):
        return src.lower()
    # IDLE/KICK and friends carry the channel as a bare token in the event.
    mm = re.search(r"[#&][^\s]+", ev.get("raw", ""))
    if mm:
        return mm.group(0).lower()
    return "#dm"


def build_prompt(batch, ring, nick=NICK, system=SYSTEM):
    """Compose the model prompt: per-channel context then the new events.

    `ring` maps channel -> deque of recent event dicts (oldest first).
    `batch` is the list of new events to act on. Deterministic ordering:
    channels in first-seen order, events oldest-first.
    """
    chan_order = []
    grouped = OrderedDict()
    for ev in batch:
        ch = prompts_of(ev)
        if ch not in grouped:
            grouped[ch] = []
            chan_order.append(ch)
        grouped[ch].append(ev)

    batch_ids = {id(ev) for ev in batch}
    out = [system, ""]
    for ch in chan_order:
        # Context excludes the new events themselves (they are listed below).
        ctx = [ev for ev in ring.get(ch, ()) if id(ev) not in batch_ids]
        if ctx:
            out.append(f"Recent context in {ch}:")
            for ev in ctx:
                out.append("  " + fmt_event(ev))
        out.append(f"New events in {ch}:")
        for ev in grouped[ch]:
            out.append("  " + fmt_event(ev))
        out.append("")
    out.append("Your reply (SAY lines only):")
    return "\n".join(out)


def parse_answer(text):
    """Extract [(target, body)] from the model's `SAY <target> <text>` lines."""
    out = []
    for raw in _ANSI.sub("", text).splitlines():
        line = raw.strip()
        m = _SAY.match(line)
        if not m:
            continue
        target, body = m.group(1), m.group(2).strip()
        if body:
            out.append((target, body))
    return out


# --- FIFO -------------------------------------------------------------------
def write_fifo(lines, path=FIFO, deadline=10.0):
    """Write `SAY target body` command lines into the bot FIFO.

    Non-blocking open + retry: a bot that has not yet created the FIFO (or is
    down) must not wedge the brain. Returns the number of lines written.
    """
    if not lines:
        return 0
    payload = "".join(f"SAY {t} {b}\n" for t, b in lines).encode("utf-8")
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(0.25)
            continue
        try:
            os.write(fd, payload)
            return len(lines)
        except OSError as e:
            emit("FIFO_WRITE_ERROR", repr(e))
            return 0
        finally:
            os.close(fd)
    emit("FIFO_WRITE_TIMEOUT", path)
    return 0


# --- model invocation -------------------------------------------------------
def invoke(prompt, timeout=TIMEOUT):
    """Run the brain command non-interactively; return stdout text.

    OC_BRAIN_CMD (stub) receives the prompt on stdin. Otherwise the OpenCode
    CLI is called exactly as the README documents:
    `opencode run --auto --model <model> <prompt>`. A timeout kills the child
    and returns ""; the loop never blocks on a wedged model.
    """
    if OC_BRAIN_CMD:
        argv = shlex.split(OC_BRAIN_CMD)
        stdin_text = prompt
    else:
        argv = [OPENCODE_BIN, "run", "--auto", "--model", MODEL, prompt]
        stdin_text = None
    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        emit("MODEL_TIMEOUT", f"{timeout:.0f}s")
        return ""
    except FileNotFoundError as e:
        emit("MODEL_NOT_FOUND", repr(e))
        return ""
    except OSError as e:
        emit("MODEL_ERROR", repr(e))
        return ""
    if proc.returncode != 0:
        emit("MODEL_EXIT", proc.returncode)
    if proc.stderr:
        emit("MODEL_STDERR", proc.stderr.strip()[:200])
    return proc.stdout or ""


# --- tail -------------------------------------------------------------------
class Tailer:
    """Minimal `tail -f` over a file, tolerant of truncation/rotation."""

    def __init__(self, path, from_start=False):
        self.path = path
        self.from_start = from_start
        self.f = None
        self.fd = None
        self.buf = ""

    def _open(self):
        f = open(self.path, "r", errors="replace")
        self.fd = os.fstat(f.fileno())
        if not self.from_start:
            f.seek(0, os.SEEK_END)
        else:
            self.from_start = False
        self.f = f

    def read_new(self):
        if self.f is None:
            try:
                self._open()
            except FileNotFoundError:
                return []
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return []
        # Rotation / replacement: reopen from the top of the new inode.
        if (st.st_ino, st.st_dev) != (self.fd.st_ino, self.fd.st_dev):
            try:
                self.f.close()
            except Exception:
                pass
            self.f = None
            self.buf = ""
            return []
        data = self.f.read()
        if not data:
            return []
        self.buf += data
        if "\n" not in self.buf:
            return []
        *whole, self.buf = self.buf.split("\n")
        return [ln for ln in whole if ln.strip()]


# --- main loop --------------------------------------------------------------
class Brain:
    def __init__(self, dry_run=DRY_RUN):
        self.dry_run = dry_run
        self.ring = OrderedDict()          # chan -> deque[event]
        self.pending = []                  # (monotonic_ts, event)
        self.last_sent = {}                # chan -> monotonic
        self.last_global = 0.0
        self.loop_count = 0
        self.recent = deque(maxlen=64)     # sha1 of lines we just sent

    def _remember(self, ev):
        ch = prompts_of(ev)
        if ch not in self.ring:
            self.ring[ch] = deque(maxlen=RING)
            if len(self.ring) > MAX_CHANS:
                self.ring.popitem(last=False)
        self.ring[ch].append(ev)

    def feed(self, ev):
        if ev is None:
            return
        # Ignore our own nick and non-actionable events for the batch, but
        # still keep them as context.
        src = (ev.get("from") or "").lower()
        if src and src == NICK.lower():
            return
        self._remember(ev)
        if ev.get("kind") in ACTIONABLE:
            if ev.get("kind") == "MSG" and not (ev.get("body") or "").strip():
                return
            self.pending.append((time.monotonic(), ev))

    def due(self):
        if not self.pending:
            return False
        return (len(self.pending) >= BATCH_MAX or
                time.monotonic() - self.pending[0][0] >= BATCH_WINDOW)

    def flush(self):
        batch = [ev for _ts, ev in self.pending]
        self.pending = []
        prompt = build_prompt(batch, self.ring, nick=NICK, system=SYSTEM)
        if self.dry_run:
            emit("DRY_RUN_PROMPT", "")
            print(prompt, flush=True)
            return
        answer = invoke(prompt)
        if not answer:
            return
        says = parse_answer(answer)
        if not says:
            emit("NO_SAY", f"from {len(batch)} event(s)")
            return
        out = []
        now = time.monotonic()
        for target, body in says:
            if body.startswith(f"{NICK}:"):        # never answer ourselves
                continue
            ch = target.lower()
            if now - self.last_sent.get(ch, -1e9) < RATE:
                emit("RATE_LIMITED", target)
                continue
            if now - self.last_global < GLOBAL_RATE:
                emit("RATE_LIMITED", "global")
                continue
            # Dedupe: a model that echoes the last line must not loop.
            digest = hashlib.sha1((target + "\0" + body).encode()).hexdigest()
            if digest in self.recent:
                continue
            out.append((target, body, digest))
            self.last_sent[ch] = now
            self.last_global = now
        if not out:
            return
        n = write_fifo([(t, b) for t, b, _ in out])
        for _t, _b, digest in out:
            self.recent.append(digest)
        emit("SENT", n, " ".join(t for t, _, _ in out))

    def heartbeat(self):
        self.loop_count += 1
        try:
            with open(HEARTBEAT, "w") as f:
                f.write(f"{time.time():.3f} {self.loop_count}\n")
        except OSError:
            pass

    def run(self):
        emit("START", f"events={EVENTS_FILE}", f"fifo={FIFO}", f"model={MODEL}",
             "dry-run" if self.dry_run else "live")
        install_api_key()
        tail = Tailer(EVENTS_FILE, from_start=FROM_START)
        while True:
            self.heartbeat()
            for line in tail.read_new():
                self.feed(parse_event(line))
            if self.due():
                self.flush()
            time.sleep(POLL)


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenCode IRC brain")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt instead of invoking the model")
    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Brain(dry_run=args.dry_run or DRY_RUN).run()


if __name__ == "__main__":
    main()
