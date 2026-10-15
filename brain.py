#!/usr/bin/env python3
"""brain.py — persistent OpenCode sessions, one per (network, channel).

    opencode serve runs ONCE (or BRAIN_SERVE_URL attaches to one); every
    (network, channel) gets ONE warm session; events are tailed per network
    and submitted with POST /session/{id}/prompt_async (HTTP 204, never
    blocking); replies stream back over SSE GET /event and are parsed into
    FIFO verb lines. A wedged turn is cancelled with /abort; refusals, turn
    caps and repeated wedges reset the session, which rehydrates from disk
    memory (memory_log.py). Permission requests are rejected on sight and
    announced to the admin over the bot FIFO — an interactive prompt would
    dead-lock a headless agent, so deny fast and surface on IRC.
"""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from functools import partial

import memory_log
import networks
import policy
from util import emit as _emit
from util import env, env_bool

emit = partial(_emit, "BRAIN")

# --- config -----------------------------------------------------------------
HEARTBEAT = env("BRAIN_HEARTBEAT", "/data/brain.heartbeat")
AGENT_STATUS = env("BRAIN_AGENT_STATUS", "/data/agent.status")
WORKDIR = env("BRAIN_WORKDIR", "/data/workspace")
MEMORY_DIR = env("BRAIN_MEMORY_DIR", os.path.join(WORKDIR, "memory"))
NICK = env("IRC_NICK", "vjt-claude")           # fallback; per-net nick wins
ADMIN = env("BRAIN_ADMIN", "admin")            # NOTICE target for the gate
MODEL = env("BRAIN_MODEL", "opencode/deepseek-v4.1-flash-free")
SERVE_URL = env("BRAIN_SERVE_URL", "")         # attach mode (tests/CI)
SERVE_PORT = int(env("BRAIN_SERVE_PORT", "46099"))
POLL = float(env("BRAIN_POLL", "1.0"))
BATCH_WINDOW = float(env("BRAIN_BATCH_WINDOW", "3.0"))
RATE = float(env("BRAIN_RATE", "3.0"))         # min seconds between sends/target
GLOBAL_RATE = float(env("BRAIN_GLOBAL_RATE", "1.0"))
FROM_START = env_bool("BRAIN_FROM_START")
MUTE = tuple(c.strip().lower() for c in env("BRAIN_MUTE", "").split(",")
             if c.strip())                     # chan_filter.awk port
ADDRESS_ONLY = env_bool("BRAIN_ADDRESS_ONLY")
API_KEY = env("OPENCODE_API_KEY", "")

OPENCODE_BIN = "opencode"
RING = 8               # events of context carried in every prompt
BATCH_MAX = 5          # submit a batch once it grows this big
MAX_CHANS = 64
PENDING_MAX = 32       # unsubmitted events kept per session, newest wins
# A free-tier model can legitimately turn for minutes: these stay env-tunable
# so a slow deploy can tune around guaranteed abort->wedge->reset loops.
MAX_TURNS = int(env("BRAIN_MAX_TURNS", "150"))          # turn cap per session
TURN_TIMEOUT = float(env("BRAIN_TURN_TIMEOUT", "180"))  # wedged-turn abort
DRY_RUN = env_bool("BRAIN_DRY_RUN")   # lets compose/k8s dry-run without
                                      # editing the container command


def install_api_key():
    """Write the OpenCode auth file from OPENCODE_API_KEY; must run BEFORE
    `opencode serve` spawns (the server reads auth once)."""
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


def setup_workdir():
    """Materialise the agent workspace: opencode.json + memory root.

    The directory the server runs in is the session's project root: its
    opencode.json carries the permission gate, the auto-injected MEMORY.md
    and compaction; `memory/` is what the agent survives compactions with.
    """
    os.makedirs(WORKDIR, exist_ok=True)
    cfg_path = os.path.join(WORKDIR, "opencode.json")
    if not os.path.exists(cfg_path):
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(here, "opencode.json")) as f:
                data = f.read()
            json.loads(data)                  # validate before installing
        except (OSError, ValueError) as e:
            raise RuntimeError(f"opencode.json missing or invalid: {e}") from e
        with open(cfg_path, "w") as f:
            f.write(data)
        emit("WORKDIR_CONFIG", cfg_path)
    mem = memory_log.Memory(MEMORY_DIR)
    mem.ensure()
    mem.archive_older_than()   # the 14-day rule, in code, at brain start
    return mem


def registers():
    """Per-channel register map from <WORKDIR>/registers.json."""
    try:
        with open(os.path.join(WORKDIR, "registers.json")) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# --- serve ------------------------------------------------------------------
class Serve:
    """The `opencode serve` process handle, managed or attached."""

    def __init__(self, url, managed=None, port=None):
        self.url = url.rstrip("/")
        self.managed = managed
        self.port = port

    @classmethod
    def spawned(cls):
        """Start `opencode serve` on loopback and wait for /global/health.

        Serve is the agent's whole control plane (sessions, prompts, aborts,
        history) and has no auth of its own: the loopback bind IS the boundary.
        The brain talks to 127.0.0.1 here, and so does the watchdog — compose
        gives it `network_mode: service: brain`, k8s puts both containers in
        one pod — so the API is never reachable beyond the host (review
        blocker 1). There is deliberately no env knob to change this.
        """
        install_api_key()
        host = "127.0.0.1"
        last_err = "unknown"
        for attempt in range(5):
            port = SERVE_PORT + attempt
            proc = subprocess.Popen(
                [OPENCODE_BIN, "serve", "--hostname", host,
                 "--port", str(port), "--log-level", "WARN"],
                cwd=WORKDIR, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            serve = cls(f"http://127.0.0.1:{port}", managed=proc, port=port)
            if serve._wait_healthy():
                return serve
            last_err = f"port {port} (exit {proc.poll()})"
            serve.stop()
        raise RuntimeError(f"opencode serve failed to start: {last_err}")

    @classmethod
    def attached(cls):
        return cls(SERVE_URL)

    def _wait_healthy(self, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.healthy():
                return True
            if self.managed and self.managed.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def healthy(self):
        try:
            with urllib.request.urlopen(self.url + "/global/health",
                                        timeout=3) as r:
                return json.loads(r.read()).get("healthy") is True
        except (OSError, ValueError):
            return False

    def stop(self):
        if self.managed and self.managed.poll() is None:
            self.managed.terminate()
            try:
                self.managed.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.managed.kill()


# --- HTTP client ------------------------------------------------------------
class Api:
    """Thin sync client for the opencode server's v1 API."""

    def __init__(self, base):
        self.base = base.rstrip("/")

    def _req(self, method, path, body=None, timeout=15):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
        return json.loads(payload) if payload else None

    def create_session(self, title):
        return self._req("POST", "/session", {"title": title})

    def list_sessions(self):
        return self._req("GET", "/session") or []

    def prompt_async(self, sid, text, system=None, model=None):
        body = {"parts": [{"type": "text", "text": text}]}
        if system:
            body["system"] = system
        if model:
            body["model"] = {"providerID": model[0], "modelID": model[1]}
        self._req("POST", f"/session/{sid}/prompt_async", body)

    def messages(self, sid):
        return self._req("GET", f"/session/{sid}/message") or []

    def abort(self, sid):
        return self._req("POST", f"/session/{sid}/abort", {})

    def permission_reply(self, request_id, reply="reject", message=""):
        return self._req("POST", f"/permission/{request_id}/reply",
                         {"reply": reply, "message": message})

    def events(self):
        """Yield parsed SSE events; reconnect is the caller's job."""
        req = urllib.request.Request(self.base + "/event")
        resp = urllib.request.urlopen(req, timeout=None)
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    yield json.loads(line[6:])
                except ValueError:
                    continue
        finally:
            resp.close()


def split_model(model):
    """'provider/model' -> ('provider', 'model'); None = server default."""
    if not model or "/" not in model:
        return None
    p, _, m = model.partition("/")
    return (p, m) if p and m else None


# --- FIFO -------------------------------------------------------------------
def write_fifo(lines, path, deadline=10.0):
    """Write FIFO lines into the bot's named pipe. Non-blocking open + retry:
    a bot that is down must not wedge the brain. Returns lines written."""
    if not lines:
        return 0
    payload = "".join(f"{ln}\n" for ln in lines).encode("utf-8")
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
            emit("FIFO_WRITE_ERROR", path, repr(e))
            return 0
        finally:
            os.close(fd)
    emit("FIFO_WRITE_TIMEOUT", path)
    return 0


# --- tail -------------------------------------------------------------------
class Tailer:
    """Minimal `tail -f`, tolerant of truncation/rotation."""

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


# --- sessions ----------------------------------------------------------------
class Session:
    __slots__ = ("sid", "net", "chan", "nick", "system", "turns", "busy_since",
                 "wedges", "refusals", "pending", "ring", "last_count")

    def __init__(self, sid, net, chan, nick, system):
        self.sid = sid
        self.net = net
        self.chan = chan
        self.nick = nick
        self.system = system
        self.turns = 0
        self.busy_since = None
        self.wedges = 0
        self.refusals = 0
        self.pending = []                  # (fed_monotonic, event)
        self.ring = deque(maxlen=RING)
        self.last_count = 0                # messages already collected

    @property
    def title(self):
        return f"irc:{self.net}:{self.chan}"


def last_assistant_text(messages):
    """The final assistant turn's text parts, joined. [] safe."""
    for msg in reversed(messages):
        if msg.get("info", {}).get("role") != "assistant":
            continue
        texts = [p.get("text", "") for p in msg.get("parts") or []
                 if p.get("type") == "text"]
        return "\n".join(t for t in texts if t)
    return ""


def assistant_turn_count(messages):
    return sum(1 for m in messages
               if m.get("info", {}).get("role") == "assistant")


# --- brain -------------------------------------------------------------------
class Brain:
    def __init__(self, dry_run=False, nets=None, mem=None, refusal_pats=None):
        self.dry_run = dry_run
        self.nets = nets if nets is not None else networks.load()
        self.net_by_name = {n.name: n for n in self.nets}
        self.mem = mem
        self.api = None
        self.serve = None
        self.lock = threading.Lock()
        self.sessions = {}                 # (net, chan_lower) -> Session
        self.by_sid = {}                   # sessionID -> Session
        self.last_sent = {}                # (net, target) -> monotonic
        self.last_global = 0.0
        self.recent = deque(maxlen=64)     # digests of lines just sent
        self.loop_count = 0
        self.total_turns = 0
        self.total_refusals = 0
        self.total_wedges = 0
        self.registers = registers()
        self.model = split_model(MODEL)
        self.refusals = refusal_pats or policy.DEFAULT_REFUSAL_PATTERNS
        self.stored = {}                   # (net, chan) -> sid, from disk
        self.stop_flag = False

    # -- session management -----------------------------------------------------
    def _system_for(self, net, chan, nick):
        return policy.build_system(nick, net, chan, self.registers)

    def _store_path(self):
        return os.path.join(WORKDIR, "sessions.json")

    def load_session_store(self):
        """{(net, chan): sid} across brain restarts: the pointer to the LIVE
        session per channel (dead ones were removed by reset)."""
        try:
            with open(self._store_path()) as f:
                data = json.loads(f.read())
            if isinstance(data, dict):
                self.stored = {tuple(k.split(":", 1)): v
                               for k, v in data.items()}
        except (OSError, ValueError):
            self.stored = {}

    def save_session_store(self):
        try:
            with open(self._store_path(), "w") as f:
                json.dump({f"{net}:{chan}": sess.sid
                           for (net, chan), sess in self.sessions.items()}, f)
        except OSError:
            pass

    def get_session(self, net, chan, nick=None):
        """The (net, chan) session, created or re-attached on first use.
        Main-thread only (feed drives it); the SSE thread never creates."""
        key = (net, chan)
        with self.lock:
            sess = self.sessions.get(key)
        if sess:
            return sess
        if len(self.sessions) >= MAX_CHANS:
            emit("CHAN_OVERFLOW", net, chan)
            return None
        env_ = self.net_by_name[net].env
        nick = nick or env_.get("IRC_NICK", NICK)
        title = f"irc:{net}:{chan}"
        known = {s.get("id") for s in self.api.list_sessions()}
        sid = None
        if key in self.stored and self.stored[key] in known:
            sid = self.stored[key]
        if sid is None:
            try:
                sid = self.api.create_session(title)["id"]
            except (OSError, KeyError, ValueError) as e:
                emit("SESSION_CREATE_ERROR", title, repr(e))
                return None
            sess = Session(sid, net, chan, nick, self._system_for(net, chan, nick))
            with self.lock:
                self.sessions[key] = sess
                self.by_sid[sid] = sess
            self.save_session_store()
            emit("SESSION_NEW", title, sid)
            self.rehydrate(sess)
            return sess
        # Re-attached: count existing turns so the cap survives restarts.
        sess = Session(sid, net, chan, nick, self._system_for(net, chan, nick))
        try:
            sess.turns = assistant_turn_count(self.api.messages(sid))
        except OSError:
            sess.turns = 0
        with self.lock:
            self.sessions[key] = sess
            self.by_sid[sid] = sess
        emit("SESSION_ATTACHED", title, sid, f"turns={sess.turns}")
        return sess

    def rehydrate(self, sess):
        """Wake a fresh session from disk memory (the /resume ritual)."""
        prompt = policy.rehydrate_prompt(sess.nick, self.mem.index_text(),
                                         self.mem.today_body())
        try:
            self.api.prompt_async(sess.sid, prompt, system=sess.system,
                                  model=self.model)
            sess.busy_since = time.monotonic()
            emit("REHYDRATE", sess.title, sess.sid)
        except OSError as e:
            emit("REHYDRATE_ERROR", sess.title, repr(e))

    def reset_session(self, sess, reason):
        """The `/clear`: abandon the session, start a new one, rehydrate from
        disk. Aborts first so we never reset mid-turn."""
        try:
            self.api.abort(sess.sid)
        except OSError:
            pass
        with self.lock:
            self.sessions.pop((sess.net, sess.chan), None)
            self.by_sid.pop(sess.sid, None)
            leftover = sess.pending
        self.save_session_store()
        emit("SESSION_RESET", sess.title, reason, f"turns={sess.turns}")
        self.mem.append_activity(
            f"session reset ({reason}) after {sess.turns} turns: {sess.title}")
        fresh = self.get_session(sess.net, sess.chan, sess.nick)
        if fresh is not None and leftover:
            with self.lock:
                fresh.pending = leftover + fresh.pending

    # -- event intake -------------------------------------------------------------
    def feed(self, net, ev):
        if ev is None:
            return
        if policy.is_muted(ev, MUTE):
            return
        sess = self.get_session(net, policy.channel_of(ev))
        if sess is None:
            return
        src = (ev.get("from") or "").lower()
        if src and src == sess.nick.lower():
            return
        sess.ring.append(ev)
        if not policy.wakes_model(ev, sess.nick, ADDRESS_ONLY):
            return
        with self.lock:
            sess.pending.append((time.monotonic(), ev))
            del sess.pending[:-PENDING_MAX]   # a slow model must not grow
                                              # the batch past the context

    def due(self, sess):
        with self.lock:
            if not sess.pending:
                return False
            oldest = sess.pending[0][0]
            return (len(sess.pending) >= BATCH_MAX or
                    time.monotonic() - oldest >= BATCH_WINDOW)

    # -- submit ---------------------------------------------------------------------
    def submit(self, sess):
        with self.lock:
            if not sess.pending or sess.busy_since is not None:
                return
            batch = sess.pending
            sess.pending = []
        addressed = [id(ev) for _t, ev in batch
                     if policy.is_addressed(ev, sess.nick)]
        prompt = policy.build_prompt([ev for _t, ev in batch],
                                     list(sess.ring), sess.net,
                                     sess.chan, addressed)
        if self.dry_run:
            emit("DRY_RUN_PROMPT", sess.title)
            print(prompt, flush=True)
            return
        try:
            self.api.prompt_async(sess.sid, prompt, system=sess.system,
                                  model=self.model)
        except OSError as e:
            emit("PROMPT_ERROR", sess.title, repr(e))
            with self.lock:
                sess.pending = batch + sess.pending
            return
        with self.lock:
            sess.busy_since = time.monotonic()
        emit("PROMPT_SENT", sess.title, f"events={len(batch)}")

    # -- collect ----------------------------------------------------------------------
    def on_idle(self, sid):
        sess = self.by_sid.get(sid)
        if sess is None:
            return
        try:
            msgs = self.api.messages(sid)
        except OSError as e:
            emit("COLLECT_ERROR", sess.title, repr(e))
            return
        sess.last_count = len(msgs)
        text = last_assistant_text(msgs)
        with self.lock:
            sess.busy_since = None
        self.record_turn(sess, text)
        self.dispatch(sess, text)
        # Monitor queueing: the next due batch goes out now.
        if self.due(sess):
            self.submit(sess)

    def record_turn(self, sess, text):
        sess.turns += 1
        self.total_turns += 1
        hits = policy.refusal_hits(text, self.refusals)
        if hits:
            sess.refusals += 1
            self.total_refusals += 1
            emit("REFUSAL", sess.title, hits[0][:60])
            self.mem.append_activity(
                f"refusal detected in {sess.title}: {hits[0][:60]}")
            self.reset_session(sess, "refusal")
            return
        if sess.turns >= MAX_TURNS:
            self.reset_session(sess, "turn-cap")
        elif text:
            self.mem.append_activity(
                f"{sess.title}: turn {sess.turns}, {len(text)} chars")

    def dispatch(self, sess, text):
        answer = policy.filter_answer(policy.parse_answer(text), sess.nick)
        if not answer:
            emit("NO_VERBS", sess.title)
            return
        nowm = time.monotonic()
        # The rate gates apply per ANSWER, not per line: lines 2..N of one
        # reply ride the same FIFO write, and a per-line check ate every line
        # after the first (review blocker 2: `SAY #t one\nSAY #t two\nSAY #t
        # three` delivered only `one`). An answer failing a gate is dropped
        # whole — never partially.
        targets = sorted({t.lower() for v, t, _b in answer
                          if v in policy.CHAT_VERBS and t})
        if targets:
            for t in targets:
                if nowm - self.last_sent.get((sess.net, t), -1e9) < RATE:
                    emit("RATE_LIMITED", sess.title, t)
                    return
            if nowm - self.last_global < GLOBAL_RATE:
                emit("RATE_LIMITED", sess.title, "global")
                return
        # A prompt-injected answer must not be able to storm the connection
        # either: non-chat verbs are capped per answer (review should-fix).
        non_chat = [a for a in answer if a[0] not in policy.CHAT_VERBS]
        if len(non_chat) > policy.NON_CHAT_CAP:
            emit("VERB_STORM", sess.title, f"{len(non_chat)} non-chat lines, "
                 f"keeping {policy.NON_CHAT_CAP}")
            kept, room = [], policy.NON_CHAT_CAP
            for a in answer:
                if a[0] in policy.CHAT_VERBS:
                    kept.append(a)
                elif room > 0:
                    kept.append(a)
                    room -= 1
            answer = kept
        out, digests = [], []
        for verb, target, body in answer:
            digest = hashlib.sha1(
                f"{sess.net}\0{target}\0{body}".encode()).hexdigest()
            if digest in self.recent:
                continue                  # a model echoing itself must not loop
            digests.append(digest)
            if target:
                out.append(f"{verb} {target} {body}")
            elif body:
                out.append(f"{verb} {body}")
            else:
                out.append(verb)
        if not out:
            return
        for t in targets:                 # commit only on a real send
            self.last_sent[(sess.net, t)] = nowm
        self.last_global = nowm
        n = write_fifo(out, self.net_by_name[sess.net].fifo)
        self.recent.extend(digests)
        emit("SENT", n, sess.title, " ".join(out)[:200])

    # -- agent guard (wedged turns) ---------------------------------------------------
    def guard_tick(self):
        nowm = time.monotonic()
        with self.lock:
            busy = [s for s in self.sessions.values()
                    if s.busy_since is not None]
        for sess in busy:
            if nowm - sess.busy_since < TURN_TIMEOUT:
                continue
            emit("WEDGED", sess.title, f"{nowm - sess.busy_since:.0f}s")
            self.total_wedges += 1
            sess.wedges += 1
            try:
                self.api.abort(sess.sid)
            except OSError as e:
                emit("ABORT_ERROR", sess.title, repr(e))
            with self.lock:
                sess.busy_since = None
            if sess.wedges >= 2:
                self.reset_session(sess, "wedged-twice")

    # -- permission gate -----------------------------------------------------------------
    def gate(self, props):
        """permission.asked: reject on sight, announce over the FIFO."""
        req_id = props.get("id") or ""
        sess = self.by_sid.get(props.get("sessionID"))
        what = props.get("permission") or "unknown"
        patterns = ",".join(props.get("patterns") or [])[:120]
        emit("PERMISSION_DENIED", what, patterns)
        self.mem.append_activity(f"permission denied: {what} {patterns}")
        try:
            self.api.permission_reply(
                req_id, "reject",
                "headless IRC agent: tool use outside policy is denied")
        except OSError as e:
            emit("PERMISSION_REPLY_ERROR", repr(e))
        if sess is not None:
            write_fifo([f"NOTICE {ADMIN} [gate] denied {what} ({patterns}) "
                        f"on {sess.net}{sess.chan}"],
                       self.net_by_name[sess.net].fifo)

    # -- SSE loop ------------------------------------------------------------------------
    def catch_up_missed_idles(self):
        """Run on every (re)connect: collect turns that finished while the
        stream was down. A session.idle lost to a connection blip would
        otherwise leave the finished reply unfetched — channel silence, then a
        wedged-reset 180 s later for good measure (review should-fix). A turn
        still running shows a non-assistant last message and is left alone."""
        for sess in list(self.sessions.values()):
            if sess.busy_since is None:
                continue
            try:
                msgs = self.api.messages(sess.sid)
            except OSError:
                continue
            if len(msgs) <= sess.last_count:
                continue
            if msgs[-1].get("info", {}).get("role") != "assistant":
                continue
            emit("SSE_CATCHUP", sess.title)
            self.on_idle(sess.sid)

    def sse_loop(self):
        backoff = 1.0
        while not self.stop_flag:
            try:
                self.catch_up_missed_idles()
                for ev in self.api.events():
                    self.on_event(ev)
                    backoff = 1.0
                    if self.stop_flag:
                        return
            except OSError as e:
                if self.stop_flag:
                    return
                emit("SSE_ERROR", repr(e))
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def on_event(self, ev):
        etype = ev.get("type", "")
        props = ev.get("properties") or {}
        if etype == "session.idle":
            self.on_idle(props.get("sessionID"))
        elif etype == "session.status":
            sess = self.by_sid.get(props.get("sessionID"))
            status = props.get("status") or {}
            if sess and status.get("type") == "retry":
                emit("SESSION_RETRY", sess.title,
                     str(status.get("message", ""))[:120])
            elif sess and status.get("type") == "busy":
                sess.busy_since = sess.busy_since or time.monotonic()
        elif etype == "session.error":
            emit("SESSION_ERROR", str(props)[:200])
        elif etype == "permission.asked":
            self.gate(props)
        elif etype in ("session.next.compaction.ended", "session.compacted"):
            sess = self.by_sid.get(props.get("sessionID"))
            self.mem.log_compaction()
            emit("COMPACTED", sess.title if sess
                 else str(props.get("sessionID", "")))

    # -- watchdog plumbing --------------------------------------------------------------
    def heartbeat(self):
        self.loop_count += 1
        try:
            with open(HEARTBEAT, "w") as f:
                f.write(f"{time.time():.3f} {self.loop_count}\n")
        except OSError:
            pass
        with self.lock:
            snap = {s.title: {"turns": s.turns, "refusals": s.refusals,
                              "wedges": s.wedges,
                              "busy": s.busy_since is not None}
                    for s in self.sessions.values()}
        try:
            with open(AGENT_STATUS, "w") as f:
                json.dump({"ts": time.time(), "turns": self.total_turns,
                           "refusals": self.total_refusals,
                           "wedges": self.total_wedges,
                           "sessions": snap}, f)
        except OSError:
            pass

    # -- main loop --------------------------------------------------------------------
    def run(self):
        emit("START", f"nets={','.join(n.name for n in self.nets)}",
             f"model={MODEL}", "dry-run" if self.dry_run else "live",
             f"mute={','.join(MUTE) or '-'}",
             "address-only" if ADDRESS_ONLY else "policy-gated")
        self.mem = self.mem or setup_workdir()
        self.load_session_store()
        self.serve = Serve.attached() if SERVE_URL else Serve.spawned()
        emit("SERVE_ATTACH" if SERVE_URL else "SERVE_UP", self.serve.url)
        self.api = Api(self.serve.url)
        tailers = [(n.name, Tailer(n.events, from_start=FROM_START))
                   for n in self.nets]
        threading.Thread(target=self.sse_loop, daemon=True).start()
        while not self.stop_flag:
            self.heartbeat()
            for net_name, tail in tailers:
                for line in tail.read_new():
                    self.feed(net_name, policy.parse_event(line))
            for sess in list(self.sessions.values()):
                if self.due(sess):
                    self.submit(sess)
            self.guard_tick()
            # Managed serve died: respawn, then drop session state so every
            # channel re-attaches against the new server.
            if self.serve.managed and self.serve.managed.poll() is not None:
                emit("SERVE_DOWN", f"exit={self.serve.managed.returncode}")
                try:
                    fresh = Serve.spawned()
                except RuntimeError as e:
                    emit("SERVE_RESPAWN_FAIL", repr(e))
                    time.sleep(POLL * 5)
                else:
                    self.serve = fresh
                    self.api = Api(fresh.url)
                    with self.lock:
                        self.sessions.clear()
                        self.by_sid.clear()
                    emit("SERVE_UP", fresh.url)
            time.sleep(POLL)
        self.serve.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenCode IRC brain (persistent)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and print prompts, never submit them")
    args = ap.parse_args(argv)
    brain = Brain(dry_run=args.dry_run or DRY_RUN)

    def _term(*_):
        brain.stop_flag = True
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    brain.run()


if __name__ == "__main__":
    main()
