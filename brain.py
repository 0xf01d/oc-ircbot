#!/usr/bin/env python3
"""brain.py — ONE persistent OpenCode session, vjt's live-conversation model.

    opencode serve runs ONCE (or BRAIN_SERVE_URL attaches to one); the brain
    holds ONE session — the same continuous conversation vjt's Monitor fed,
    with every network and channel interleaved in it. Events are tailed per
    network and submitted as `[HH:MM] net|target <nick>: text` lines with
    POST /session/{id}/prompt_async (HTTP 204, never blocking); while the
    model turns, batches queue and drain on idle — one conversation,
    serialized, no per-channel parallelism. Routing comes from the ANSWER:
    every FIFO verb line carries its network (`SAY azzurra|#chan text`) and
    is written to that network's bot FIFO. A wedged turn is cancelled with
    /abort; refusals and repeated wedges reset the session, which
    rehydrates from disk memory (memory_log.py). Permission requests are
    rejected on sight and announced to the admin over the bot FIFO — an
    interactive prompt would dead-lock a headless agent, so deny fast and
    surface on IRC.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from functools import partial

import memory_log
import networks
import plug
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
# Quiescence window: a batch fires when the queue has been still this long.
# This is the knob that coalesces a human burst; BATCH_WINDOW alone measured
# from the OLDEST event, so a burst of five messages became five thin turns.
BATCH_QUIET = float(env("BRAIN_BATCH_QUIET", "2.5"))
# Starvation cap: never hold the oldest event longer than this even if the
# channel never goes quiet (someone typing in another net, a bot loop).
BATCH_MAX_WAIT = float(env("BRAIN_BATCH_MAX_WAIT", "12.0"))
FROM_START = env_bool("BRAIN_FROM_START")
MUTE = tuple(c.strip().lower() for c in env("BRAIN_MUTE", "").split(",")
             if c.strip())                     # chan_filter.awk port
ADDRESS_ONLY = env_bool("BRAIN_ADDRESS_ONLY")
API_KEY = env("OPENCODE_API_KEY", "")
# Model ladder lives on the PVC like networks.json: /data/models.json defines
# the order (benchmark-first), the brain hot-reloads it every poll loop —
# model switches and ladder edits persist across pod restarts with no
# redeploy. Structure: {"ladder": ["provider/model", ...], "model": "provider/model"}
# ("model" optional: the ladder head when absent).
MODELS_FILE = env("BRAIN_MODELS_FILE", "/data/models.json")
# !stats: instant telemetry reply to TRUSTED users, no model turn. The brain
# answers itself over the net's FIFO — latency, tokens, tps, model, quota
# state — data straight from the opencode API's own per-message records.
STATS_CMD = env("BRAIN_STATS_CMD", "!stats")
# The brain's own plugin (plug.py): optional drop/rewrite/handle/outbound/
# event hooks from one file on the PVC. Replaces the old self-patch of /app —
# plugins live on the PVC; /app is read-only and never patched.
PLUGIN_FILE = env("BRAIN_PLUGIN", "/data/brain_plugin.py")
plugin = plug.Plugin(PLUGIN_FILE, emit)

OPENCODE_BIN = "opencode"
BATCH_MAX = int(env("BRAIN_BATCH_MAX", "25"))  # submit a batch once it grows
# this big: a busy channel settles into a handful of fat turns instead of a
# stream of thin ones. BATCH_WINDOW below still caps how long the oldest
# event may wait, so a quiet channel is never held hostage by the number.
PROBE_SECS = float(env("BRAIN_PROBE_SECS", "300"))  # best-model probe cadence
# Nothing is ever dropped for being late (vjt's model: the writer queues, the
# agent catches up) — a large backlog is only announced, loudly, here and in
# agent.status.
PENDING_BACKLOG = 128
# A free-tier model can legitimately turn for minutes: these stay env-tunable
# so a slow deploy can tune around guaranteed abort->wedge->reset loops.
RESET_SECS = int(env("BRAIN_RESET_SECS", "21600"))     # age guard (6 h)
TURN_TIMEOUT = float(env("BRAIN_TURN_TIMEOUT", "180"))  # wedged-turn abort
PREEMPT_ON_ADDRESS = env_bool("BRAIN_PREEMPT_ON_ADDRESS", True)  # an
# addressed message aborts the running turn: the operator outranks the
# agent's own background work.
PREEMPT_COOLDOWN = float(env("BRAIN_PREEMPT_COOLDOWN", "10"))
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
    if True:  # always reinstall: the image copy may be stale (permission drift)
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


def batch_trust(events):
    """The trust of the batch that triggered the latest turn. Synthetic
    (model self-initiated) events never count; a mixed batch is untrusted."""
    trusts = [ev.get("trust") for ev in events
              if not ev.get("synthetic") and ev.get("trust")]
    if not trusts:
        return None
    return "TRUSTED" if all(t == "TRUSTED" for t in trusts) else "UNTRUSTED"


# --- FIFO -------------------------------------------------------------------
_CHAT_VERBS = ("SAY", "ACT", "NOTICE")


class _Workers(dict):
    """Worker sessions keyed `net|channel`, with tolerant lookups (tuple,
    `net|channel`, `net:channel`, or a bare channel on a single net)."""

    def __init__(self, brain):
        super().__init__()
        self._brain = brain

    def _norm(self, key):
        if isinstance(key, tuple) and len(key) == 2:
            return f"{key[0]}|{str(key[1]).lower()}"
        s = str(key)
        if "|" in s:
            net, _, chan = s.partition("|")
            return f"{net}|{chan.lower()}"
        if ":" in s and not s.startswith(("#", "&")):
            net, _, chan = s.partition(":")
            if chan.lower().endswith(":work"):
                chan = chan[:-5]
            return f"{net}|{chan.lower()}"
        chan = s.lower()
        matches = [k for k in self.keys() if k.partition("|")[2] == chan]
        if len(matches) == 1:
            return matches[0]
        nets = getattr(self._brain, "nets", []) or []
        if len(nets) == 1:
            return f"{nets[0].name}|{chan}"
        return f"|{chan}"

    def __getitem__(self, key):
        return super().__getitem__(self._norm(key))

    def __setitem__(self, key, value):
        return super().__setitem__(self._norm(key), value)

    def __contains__(self, key):
        return super().__contains__(self._norm(key))

    def __delitem__(self, key):
        return super().__delitem__(self._norm(key))

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def pop(self, key, *default):
        return super().pop(self._norm(key), *default)

    def setdefault(self, key, default=None):
        return super().setdefault(self._norm(key), default)


def _outbound_hook(net_name, lines):
    """The plugin's outbound gate, applied just before the FIFO write — the
    ONE place every brain chat line passes. SAY/ACT/NOTICE only (QUIT, JOIN,
    WHOIS and the bot's own RAW/DCC escape hatches are never rewritten): a
    str return replaces the line's text, None blocks it (OUTBOUND_BLOCKED)."""
    out = []
    for ln in lines:
        verb, _, rest = ln.partition(" ")
        up = verb.upper()
        if up not in _CHAT_VERBS or " " not in rest:
            out.append(ln)
            continue
        target, _, text = rest.partition(" ")
        res = plugin.call("outbound", net_name, up, target, text)
        if res is plug.MISSING:
            out.append(ln)
        elif res is None:
            emit("OUTBOUND_BLOCKED", net_name, up, target)
        else:
            out.append(f"{verb} {target} {res}")
    return out


def write_fifo(lines, path, deadline=10.0, net=None):
    """Write FIFO lines into the bot's named pipe. Non-blocking open + retry:
    a bot that is down must not wedge the brain. Returns lines written.
    `net` (a network label) switches on the plugin's outbound hook for that
    batch; callers that know their net pass it, bare test callers don't."""
    if not lines:
        return 0
    if net is not None:
        lines = _outbound_hook(net, lines)
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


# --- plugin surface -----------------------------------------------------------
class _HandleCtx:
    """The namespace the `handle` hook gets, built fresh per event: FIFO
    primitives bound to that event's net (the brain's own verb grammar,
    verbatim — no bot.py import), the net's learned nick, and the BRAIN log.
    With no known net the senders no-op: a plugin must never be able to
    wedge the brain on a vanished network."""

    __slots__ = ("net", "nick", "_fifo")

    def __init__(self, net_name, nick, fifo):
        self.net = net_name
        self.nick = nick
        self._fifo = fifo

    def say(self, target, text):
        if self._fifo:
            write_fifo([f"SAY {target} {text}"], self._fifo, net=self.net)

    def notice(self, target, text):
        if self._fifo:
            write_fifo([f"NOTICE {target} {text}"], self._fifo, net=self.net)

    def send(self, raw):
        if self._fifo:
            write_fifo([raw], self._fifo, net=self.net)

    @staticmethod
    def emit(*parts):
        emit(*parts)


def plugin_event(name, **payload):
    """Fire-and-forget lifecycle notification to the plugin's `event` hook.
    Inline by design (a plugin is code, not a service — no threads): the
    loader's try/except is the whole isolation story."""
    plugin.call("event", name, payload)


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


# --- the one session ----------------------------------------------------------
class Session:
    __slots__ = ("sid", "title", "system", "turns", "busy_since",
                 "wedges", "refusals", "pending", "last_count",
                 "lat_ms", "out_tokens", "tps", "model_id", "prompt_ts",
                 "preempted_ts", "born", "last_net", "last_target",
                 "last_trust")

    def __init__(self, sid, title, system):
        self.sid = sid
        self.title = title
        self.system = system
        self.turns = 0
        self.busy_since = None
        self.born = time.monotonic()       # for the periodic /clear cadence
        self.wedges = 0
        self.refusals = 0
        self.pending = []                  # (fed_monotonic, event)
        self.last_count = 0                # messages already collected
        self.lat_ms = None                 # last turn's first-token→end latency
        self.out_tokens = 0                # last turn's output tokens
        self.tps = None                    # last turn's output tokens/sec
        self.model_id = None               # last turn's model
        self.prompt_ts = None              # monotonic when submit() fired
        self.preempted_ts = None           # monotonic when a preempt aborted
                                           # the running turn: the next idle
                                           # belongs to that aborted turn
        self.last_net = None               # last event's network (synthetic turns)
        self.last_target = None            # last event's reply target
        self.last_trust = None             # trust of the batch behind this turn


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
        self._nicks = {}                   # net -> actual nick (fallback-aware)
        self.mem = mem
        self.api = None
        self.serve = None
        self.lock = threading.Lock()
        self.sess = None                   # THE conversation (created lazily)
        self.workers = _Workers(self)      # net|channel -> act worker, lazily
        self.work_sessions = self.workers  # aliases for readability/tests
        self.worker_sessions = self.workers
        self.loop_count = 0
        self.total_turns = 0
        self.total_refusals = 0
        self.total_wedges = 0
        self.total_denies = 0
        self.total_allows = 0
        self.registers = registers()
        self.model = split_model(MODEL)
        # Model ladder: free-tier quotas burn out mid-flight, so when the
        # server reports a quota/rate/disabled retry we walk down the
        # ladder per-request (prompt_async takes model each turn — no
        # redeploy, no serve restart, the session stays warm). The source
        # of truth is MODELS_FILE on the PVC (like networks.json); the env
        # ladder is only the bootstrap default when the file is absent.
        ladder = os.environ.get("BRAIN_MODEL_LADDER", "")
        self._env_ladder = [split_model(m) for m in ladder.split(",")
                            if m.strip()]
        self.model_ladder = list(self._env_ladder)
        self._models_mtime = 0.0
        self._demoted_stack = []           # models climbed down from
        self._clean_turns = 0
        self._probe_sid = None             # side session for best-model probes
        self._probe_model = None           # model the current probe rides
        self._probe_next = 0.0             # monotonic clock gate
        self._last_preempt = float("-inf")  # monotonic of the last preempt
        self.refusals = refusal_pats or policy.DEFAULT_REFUSAL_PATTERNS
        self.stored_sid = None             # sid from disk, for re-attach
        self.stop_flag = False

    # -- session management -----------------------------------------------------
    def _net_nick(self, net_name):
        if net_name in self._nicks:            # learned from NICK_FALLBACK
            return self._nicks[net_name]
        net = self.net_by_name.get(net_name)
        return net.env.get("IRC_NICK", NICK) if net else NICK

    def _roster(self):
        return [(n.name, n.env.get("IRC_NICK", NICK)) for n in self.nets]

    def _store_path(self):
        return os.path.join(WORKDIR, "sessions.json")

    def load_session_store(self):
        """The ONE session's sid across brain restarts."""
        try:
            with open(self._store_path()) as f:
                data = json.loads(f.read())
            self.stored_sid = data.get("session") \
                if isinstance(data, dict) else None
        except (OSError, ValueError):
            self.stored_sid = None

    def save_session_store(self):
        try:
            with open(self._store_path(), "w") as f:
                json.dump({"session": self.sess.sid} if self.sess else {}, f)
        except OSError:
            pass

    def ensure_session(self):
        """THE one session, created or re-attached on first use. Main-thread
        only (feed drives it); the SSE thread never creates."""
        with self.lock:
            sess = self.sess
        if sess:
            return sess
        title = "irc:global"
        system = policy.build_system(self._roster(), self.registers)
        if self.api is None:  # dry-run: a local-only stand-in
            sess = Session("dry", title, system)
            with self.lock:
                self.sess = sess
            return sess
        known = {s.get("id") for s in self.api.list_sessions()}
        if self.stored_sid and self.stored_sid in known:
            # Re-attached: count existing turns so the cap survives restarts.
            sess = Session(self.stored_sid, title, system)
            try:
                sess.turns = assistant_turn_count(self.api.messages(sess.sid))
            except OSError:
                sess.turns = 0
            with self.lock:
                self.sess = sess
            emit("SESSION_ATTACHED", title, sess.sid, f"turns={sess.turns}")
            return sess
        try:
            sid = self.api.create_session(title)["id"]
        except (OSError, KeyError, ValueError) as e:
            emit("SESSION_CREATE_ERROR", title, repr(e))
            return None
        sess = Session(sid, title, system)
        with self.lock:
            self.sess = sess
        self.stored_sid = sid             # memory mirrors the persisted sid
        self.save_session_store()
        emit("SESSION_NEW", title, sid)
        self.rehydrate(sess)
        return sess

    @property
    def worker(self):
        """The sole worker, when there is exactly one (convenience)."""
        with self.lock:
            if len(self.workers) == 1:
                return next(iter(self.workers.values()))
        return None

    def worker_key(self, net_name, ev):
        return f"{net_name}|{policy.channel_of(ev)}"

    def worker_for(self, net_name, ev):
        with self.lock:
            return self.workers.get(self.worker_key(net_name, ev))

    def ensure_worker(self, net_name, ev):
        """The act worker for this network-channel, created on first use.
        A bot that only chats never pays for one."""
        key = self.worker_key(net_name, ev)
        with self.lock:
            worker = self.workers.get(key)
        if worker is not None:
            return worker
        channel = policy.channel_of(ev)
        title = f"{net_name}:{channel}:work"
        system = policy.build_system(self._roster(), self.registers, mode="act")
        if self.api is None:                 # dry-run/local stand-in
            worker = Session(f"dry-{key}", title, system)
            with self.lock:
                return self.workers.setdefault(key, worker)
        try:
            sid = self.api.create_session(title)["id"]
        except (OSError, KeyError, ValueError) as e:
            emit("WORKER_CREATE_ERROR", title, repr(e))
            return None
        worker = Session(sid, title, system)
        with self.lock:
            existing = self.workers.setdefault(key, worker)
        if existing is not worker:
            return existing
        emit("WORKER_NEW", title, sid)
        return worker

    def _session_for_sid(self, sid):
        """Route an SSE idle/status by its sid: chat vs this worker."""
        with self.lock:
            sess = self.sess
            if sess is not None and sess.sid == sid:
                return sess, "chat"
            for worker in self.workers.values():
                if worker.sid == sid:
                    return worker, "work"
        return None, None

    def _session_live(self, sess):
        with self.lock:
            if sess is self.sess:
                return True
            return any(w is sess for w in self.workers.values())

    def rehydrate(self, sess):
        """Wake a fresh session from disk memory (the /resume ritual)."""
        nick = self._net_nick(self.nets[0].name if self.nets else "")
        prompt = policy.rehydrate_prompt(nick, self.mem.index_text(),
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
        disk. Aborts first so we never reset mid-turn. stored_sid is dropped
        before ensure_session: the discarded session still exists on the
        server, so a live stored_sid made the "fresh" session a re-attach
        of the very session being reset — ~750 spins in two minutes (the
        live incident).

        A worker is simply reaped (forgotten); the next act batch for that
        channel creates a fresh one lazily. It must never reset chat."""
        try:
            self.api.abort(sess.sid)
        except OSError:
            pass
        with self.lock:
            is_chat = self.sess is sess
            if is_chat:
                self.sess = None
            else:
                for key, worker in list(self.workers.items()):
                    if worker is sess:
                        del self.workers[key]
            leftover = sess.pending
            sess.pending = []
        emit("SESSION_RESET", sess.title, reason, f"turns={sess.turns}")
        plugin_event("reset", reason=reason, turns=sess.turns)
        self.mem.append_activity(
            f"session reset ({reason}) after {sess.turns} turns: {sess.title}")
        if not is_chat:
            return
        self.stored_sid = None
        self.save_session_store()
        self.registers = registers()      # registers live like the session
        fresh = self.ensure_session()
        if fresh is not None and leftover:
            with self.lock:
                fresh.pending = leftover + fresh.pending

    # -- event intake -------------------------------------------------------------
    def feed(self, net_name, ev):
        if ev is None:
            return
        # Plugin hooks first, before every built-in gate: drop makes the
        # event never have existed (even when addressed); rewrite replaces
        # it wholesale — every later check runs on what the plugin left.
        if plugin.call("drop", ev, net_name) is True:
            return
        rewritten = plugin.call("rewrite", ev, net_name)
        if rewritten is not plug.MISSING:
            if rewritten is None:
                return
            ev = rewritten
        if policy.is_muted(ev, MUTE):
            return
        sess = self.ensure_session()
        if sess is None:
            return
        nick = self._net_nick(net_name)
        if ev.get("kind") == "NICK_FALLBACK":
            self._nicks[net_name] = (ev.get("raw").split()[-1]
                                     if ev.get("raw") else "")
        if (ev.get("from") or "").lower() == nick.lower():
            return                        # our own lines do not exist
        ev["net"] = net_name
        if not policy.wakes_model(ev, nick, ADDRESS_ONLY):
            return
        # !stats: trusted users get telemetry straight from the brain — no
        # model turn, no batch, the answer rides the FIFO right now. It stays
        # ahead of the plugin's handle hook: the built-in wins. (No outer
        # lock here: ensure_session takes self.lock itself — the old
        # `with self.lock:` around it deadlocked the brain on every !stats.)
        body = (ev.get("body") or "").strip().lower()
        if (ev.get("kind") == "MSG" and body == STATS_CMD
                and ev.get("trust") == "TRUSTED"):
            sess2 = self.ensure_session()
            if sess2 is not None:
                self.stats_reply(sess2, ev)
            return
        # handle: the plugin may serve the event itself — say/notice/send
        # over the FIFO, no model turn, nothing batched. False/absent falls
        # through to the normal batch path.
        ctx = self._plugin_ctx(net_name)
        if plugin.call("handle", ev, net_name, ctx) is True:
            return
        with self.lock:
            sess.last_net = net_name
            sess.last_target = policy.reply_target(ev)
            sess.pending.append((time.monotonic(), ev))
            depth = len(sess.pending)
        if depth >= PENDING_BACKLOG and depth % PENDING_BACKLOG == 0:
            # Visible, never dropped: a slow model must not strand events
            # silently (vjt never discards; neither do we).
            emit("PENDING_BACKLOG", sess.title, f"events={depth}")
        if PREEMPT_ON_ADDRESS:
            self._maybe_preempt(sess, ev, nick)

    def _plugin_ctx(self, net_name):
        net = self.net_by_name.get(net_name)
        return _HandleCtx(net_name, self._net_nick(net_name),
                          net.fifo if net else None)

    # -- preemption -------------------------------------------------------------------
    def _maybe_preempt(self, sess, ev, nick):
        """An addressed MSG aborts the running turn: the operator outranks
        the agent's own background work — the bot drops what it had in
        background and takes the message on the fly. The abort is armed
        (preempted_ts) before the API call so an idle that races it still
        reads as the aborted turn's; its partial output is never dispatched
        and it never counts as a wedge. A cooldown keeps a burst of
        addressed messages from abort-looping the model."""
        if ev.get("kind") != "MSG" or not policy.is_addressed(ev, nick):
            return
        with self.lock:
            busy = sess.busy_since is not None
        if not busy:
            return
        now = time.monotonic()
        if now - self._last_preempt < PREEMPT_COOLDOWN:
            emit("PREEMPT_SKIP_COOLDOWN", sess.title, ev.get("from") or "?")
            return
        self._last_preempt = now
        with self.lock:
            sess.preempted_ts = now
        try:
            self.api.abort(sess.sid)
        except OSError as e:
            with self.lock:
                sess.preempted_ts = None
            emit("ABORT_ERROR", sess.title, repr(e))
            return
        with self.lock:
            sess.busy_since = None
        emit("PREEMPT", f"{ev.get('net', '?')}|{policy.reply_target(ev)}",
             ev.get("from") or "?")
        self.submit(sess)

    def due(self, sess):
        """A batch is due when the queue has grown to BATCH_MAX, or when it has
        gone QUIET: nothing new for BATCH_QUIET seconds. Quiescence (not the
        oldest event's age) is what coalesces a burst — measuring from the
        oldest sent event #1 out while events #2-#9 were still arriving, so one
        human burst became five turns. The OLDEST cap still applies as a
        safety net for a channel that never goes quiet."""
        with self.lock:
            if not sess.pending:
                return False
            oldest = sess.pending[0][0]
            newest = sess.pending[-1][0]
            now = time.monotonic()
            if len(sess.pending) >= BATCH_MAX:
                return True
            quiet = now - newest >= BATCH_QUIET
            starved = now - oldest >= BATCH_MAX_WAIT
            return quiet or starved

    # -- submit ---------------------------------------------------------------------
    def _batch_mode(self, batch):
        """A batch is act when any event carries a hands-on directive; that is
        only a routing hint for the richer prompt and the worker session —
        every turn already has the full toolset."""
        for _t, ev in batch:
            if ev.get("synthetic") or policy.needs_hands(
                    ev.get("body") or ev.get("raw") or ""):
                return "act"
        return "chat"

    def submit(self, sess):
        with self.lock:
            if not sess.pending or sess.busy_since is not None:
                return
            batch = sess.pending
            sess.pending = []
        events = [ev for _t, ev in batch]
        addressed = [id(ev) for ev in events
                     if policy.is_addressed(
                         ev, self._net_nick(ev.get("net")))]
        mode = self._batch_mode(batch)
        emit("TURN_MODE", mode)
        route = policy.route_for(mode)
        if route == "work" and self.sess is not None and sess is self.sess:
            self._route_to_worker(batch, events, addressed, mode)
            return
        emit("TURN_ROUTE", mode, sess.title)
        self._send_batch(sess, batch, events, addressed, mode)

    def _route_to_worker(self, batch, events, addressed, mode):
        """Act work leaves the conversation. The intake batch is partitioned
        by owning net|channel FIRST: a worker is only ever handed its own
        channel's events, never a mixed batch whose last event happened to
        pick it. Chat-only groups stay in the global conversation."""
        groups = {}
        for item in batch:
            ev = item[1]
            net_name = ev.get("net") or (self.nets[0].name if self.nets else "")
            groups.setdefault(self.worker_key(net_name, ev), []).append(item)
        order = {id(item): i for i, item in enumerate(batch)}
        chat_batch = []
        for group in groups.values():
            gev = [ev for _t, ev in group]
            gmode = self._batch_mode(group)
            if gmode != "act":
                chat_batch.extend(group)
                continue
            gaddressed = [
                id(ev) for ev in gev
                if policy.is_addressed(ev, self._net_nick(ev.get("net")))]
            self._route_group_to_worker(group, gev, gaddressed, gmode)
        if chat_batch:
            chat_batch.sort(key=lambda item: order[id(item)])
            cev = [ev for _t, ev in chat_batch]
            caddressed = [
                id(ev) for ev in cev
                if policy.is_addressed(ev, self._net_nick(ev.get("net")))]
            chat = self.sess
            if chat is not None:
                emit("TURN_ROUTE", "chat", chat.title)
                self._send_batch(chat, chat_batch, cev, caddressed, "chat")

    def _route_group_to_worker(self, batch, events, addressed, mode):
        """Hand one same-net|channel group to (or queue it on) its worker."""
        trigger = events[-1]
        net_name = trigger.get("net") or (self.nets[0].name if self.nets else "")
        worker = self.ensure_worker(net_name, trigger)
        chat = self.sess
        if worker is None:                   # never drop: fall back to chat
            emit("TURN_ROUTE", mode, chat.title if chat else "?")
            if chat is not None:
                self._send_batch(chat, batch, events, addressed, mode)
            return
        with self.lock:
            worker.last_net = net_name
            worker.last_target = policy.reply_target(trigger)
            worker.pending.extend(batch)
            busy = worker.busy_since is not None
        emit("TURN_ROUTE", mode, worker.title)
        if busy:
            # One in-flight act per channel. The work is already running; a
            # second handoff for every message of a burst would be noise.
            return
        # The worker speaks when it has something to say; there is no
        # provisional acknowledgement.
        self._drain(worker)

    def _drain(self, sess):
        """Send the pending batch on an already-chosen session (the act
        handoff); route/mode were logged by the caller."""
        with self.lock:
            if not sess.pending or sess.busy_since is not None:
                return
            batch = sess.pending
            sess.pending = []
        events = [ev for _t, ev in batch]
        addressed = [id(ev) for ev in events
                     if policy.is_addressed(
                         ev, self._net_nick(ev.get("net")))]
        mode = self._batch_mode(batch)
        self._send_batch(sess, batch, events, addressed, mode)

    def _send_batch(self, sess, batch, events, addressed, mode):
        system = policy.build_system(self._roster(), self.registers, mode=mode)
        prompt = policy.build_prompt(events, addressed)
        trust = batch_trust(events)
        with self.lock:
            sess.last_trust = trust
        if self.dry_run:
            emit("DRY_RUN_PROMPT", sess.title)
            print(prompt, flush=True)
            return
        try:
            self.api.prompt_async(sess.sid, prompt, system=system,
                                  model=self.model)
        except OSError as e:
            emit("PROMPT_ERROR", sess.title, repr(e))
            plugin_event("error", reason=repr(e)[:200])
            with self.lock:
                sess.pending = batch + sess.pending
                sess.last_trust = None
            return
        with self.lock:
            sess.busy_since = time.monotonic()
            sess.prompt_ts = sess.busy_since
            # A freshly submitted turn is not the aborted one: its idle must
            # be dispatched like any other.
            sess.preempted_ts = None
        emit("PROMPT_SENT", sess.title, f"events={len(batch)}")
        plugin_event("turn_start", turns=sess.turns)

    # -- collect ----------------------------------------------------------------------
    def _note_metrics(self, sess, msgs):
        """Pull the finished assistant turn's real numbers from the API's own
        record: wall latency (created→completed), output tokens, tps, model."""
        for msg in reversed(msgs):
            info = msg.get("info", {})
            if info.get("role") != "assistant":
                continue
            t = info.get("time") or {}
            tok = info.get("tokens") or {}
            if not t.get("completed"):
                break                       # still streaming, skip
            created, done = t.get("created"), t["completed"]
            out = tok.get("output") or 0
            sess.lat_ms = max(0, done - created) if created else None
            sess.out_tokens = out
            sess.tps = round(out / (sess.lat_ms / 1000.0), 1) \
                if sess.lat_ms and out else None
            sess.model_id = info.get("modelID")
            break

    def stats_reply(self, sess, ev):
        """`!stats` — trusted-only, instant, no model turn. The brain speaks
        over the FIFO itself with the numbers it already owns."""
        q = (" DEMOTED" if self._demoted_stack
             else " OK" if self.model_ladder else "")
        net_name = ev.get("net")
        worker = self.worker_for(net_name, ev) if net_name else None
        if worker is not None and worker.busy_since is not None:
            wstate = f"busy:{int(time.monotonic() - worker.busy_since)}"
        else:
            wstate = "idle"
        lines = [
            f"model: {sess.model_id or (self.model and self.model[1]) or '?'}"
            f"{q} | turns: {self.total_turns}",
            f"latency: {sess.lat_ms}ms | out: {sess.out_tokens} tok"
            f" | tps: {sess.tps or '?'}",
            f"refusals: {self.total_refusals} | wedges: {self.total_wedges}"
            f" | pending: {len(sess.pending)}",
            f"work={wstate}",
            f"permission: denies={self.total_denies}"
            f" allows={self.total_allows}",
            f"nets: {','.join(n.name for n in self.nets)}",
        ]
        target = policy.reply_target(ev)
        net = self.net_by_name.get(net_name)
        if net is None:
            emit("STATS_ERROR", "unknown net", repr(net_name))
            return
        write_fifo([f"NOTICE {target} [stats] {l}" for l in lines], net.fifo,
                   net=net_name)
        emit("STATS_SENT", net_name, target)

    def on_idle(self, sid):
        sess, _route = self._session_for_sid(sid)
        if sess is None:
            return
        try:
            msgs = self.api.messages(sid)
        except OSError as e:
            emit("COLLECT_ERROR", sess.title, repr(e))
            return
        sess.last_count = len(msgs)
        with self.lock:
            started = sess.busy_since
            preempted = sess.preempted_ts is not None
            sess.preempted_ts = None
            sess.busy_since = None
            sess.last_trust = None
        if preempted:
            # This idle belongs to the turn a preempt aborted: its partial
            # output is never dispatched or counted (last_count is already
            # advanced, so a later catch-up cannot redispatch it either).
            # The batch that caused the preempt flows instead.
            if self.due(sess):
                self.submit(sess)
            return
        self._note_metrics(sess, msgs)
        text = last_assistant_text(msgs)
        self.record_turn(sess, text)
        still_live = self._session_live(sess)  # record_turn may have reset it
        self._note_clean_turn(sess)
        self.dispatch(sess, text)
        plugin_event("turn_end", turns=sess.turns,
                     seconds=(round(time.monotonic() - started, 3)
                              if started else None))
        # Monitor queueing: the next due batch goes out now — on the live
        # session only; a reset moved the queue to the fresh one.
        if still_live and self.due(sess):
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
        if text:
            self.mem.append_activity(
                f"{sess.title}: turn {sess.turns}, {len(text)} chars")

    def dispatch(self, sess, text):
        answer = policy.parse_answer(text)
        if not answer:
            emit("NO_VERBS", sess.title)
            return
        # Routing comes from the ANSWER: `VERB net|rest` — the net selects the
        # bot FIFO, the rest is that bot's own grammar verbatim. A line whose
        # net is missing or unknown is dropped loudly, never guessed. Nothing
        # else gates an answer: no rate limits, no dedup, no per-answer caps
        # (vjt's bot drops nothing; the model owns its own pacing).
        by_net = {}
        for verb, target, body in answer:
            raw = f"{target} {body}".strip() if target else body
            if verb == "QUIT" and not raw:
                if len(self.nets) == 1:
                    by_net.setdefault(self.nets[0].name, []).append("QUIT")
                else:
                    emit("ROUTE_ERROR", sess.title, "bare QUIT, many networks")
                continue
            net_name, sep, arg = raw.partition("|")
            if len(self.nets) == 1:
                # single network: the net label is noise — accept it bare,
                # wrong, or missing, and route home (vjt drops nothing).
                if not sep:
                    arg = raw
                net_name = self.nets[0].name
            elif not sep or net_name not in self.net_by_name:
                emit("ROUTE_ERROR", sess.title, repr(raw[:80]))
                continue
            nick = self._net_nick(net_name)
            head, _, tail = arg.partition(" ")
            if policy.is_own_echo(verb, head, tail, nick):
                continue
            by_net.setdefault(net_name, []).append(f"{verb} {arg}".rstrip())
        for net_name, lines in by_net.items():
            n = write_fifo(lines, self.net_by_name[net_name].fifo,
                           net=net_name)
            emit("SENT", n, sess.title, net_name, " ".join(lines)[:200])

    # -- agent guard (wedged turns) ---------------------------------------------------
    def guard_tick(self):
        with self.lock:
            sessions = ([self.sess] if self.sess is not None else []) + \
                list(self.workers.values())
        nowm = time.monotonic()
        for sess in sessions:
            if sess.busy_since is None:
                continue
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
        """permission.asked: allow only the batch's own TRUSTED turn; every
        other request is rejected fast and announced to the admin."""
        req_id = props.get("id") or ""
        sess, _route = self._session_for_sid(props.get("sessionID"))
        what = props.get("permission") or "unknown"
        raw_patterns = props.get("patterns") or []
        patterns = ",".join(str(p) for p in raw_patterns)[:120]
        trust = sess.last_trust if sess is not None else None
        trust_level = trust if trust in ("TRUSTED", "UNTRUSTED") \
            else "UNTRUSTED"
        decision = policy.permission_decision(trust, what, raw_patterns)
        if decision == "allow":
            self.total_allows += 1
            emit("PERMISSION_ALLOWED", what, patterns,
                 f"trust={trust_level}")
            self.mem.append_activity(
                f"permission allowed: {what} {patterns}")
            try:
                self.api.permission_reply(req_id, "allow")
            except OSError as e:
                emit("PERMISSION_REPLY_ERROR", repr(e))
            return
        self.total_denies += 1
        emit("PERMISSION_DENIED", what, patterns, f"trust={trust_level}")
        self.mem.append_activity(f"permission denied: {what} {patterns}")
        try:
            self.api.permission_reply(req_id, "reject", decision)
        except OSError as e:
            emit("PERMISSION_REPLY_ERROR", repr(e))
        if sess is not None:
            line = (f"NOTICE {ADMIN} [gate] denied {what} ({patterns})")
            for net in self.nets:
                write_fifo([line], net.fifo, net=net.name)

    # -- SSE loop ------------------------------------------------------------------------
    def catch_up_missed_idles(self):
        """Run on every (re)connect: collect turns that finished while the
        stream was down. A session.idle lost to a connection blip would
        otherwise leave the finished reply unfetched — channel silence, then a
        wedged-reset 180 s later for good measure (review should-fix). A turn
        still running shows a non-assistant last message and is left alone."""
        with self.lock:
            sessions = ([self.sess] if self.sess is not None else []) + \
                list(self.workers.values())
        for sess in sessions:
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
        if self.dry_run:
            return
        while not self.stop_flag:
            try:
                self.catch_up_missed_idles()
                plugin_event("connect")
                for ev in self.api.events():
                    self.on_event(ev)
                    backoff = 1.0
                    if self.stop_flag:
                        return
            except OSError as e:
                if self.stop_flag:
                    return
                emit("SSE_ERROR", repr(e))
                plugin_event("error", reason=repr(e)[:200])
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def on_event(self, ev):
        etype = ev.get("type", "")
        props = ev.get("properties") or {}
        if etype == "session.idle":
            self.on_idle(props.get("sessionID"))
        elif etype == "session.status":
            sess, _route = self._session_for_sid(props.get("sessionID"))
            if sess is not None:
                status = props.get("status") or {}
                if status.get("type") == "retry":
                    msg = str(status.get("message", ""))
                    emit("SESSION_RETRY", sess.title, msg[:120])
                    self._demote_model(sess, msg)
                elif status.get("type") == "busy":
                    sess.busy_since = sess.busy_since or time.monotonic()
        elif etype == "session.error":
            emit("SESSION_ERROR", str(props)[:200])
            plugin_event("error", reason=str(props)[:200])
        elif etype == "permission.asked":
            self.gate(props)
        elif etype in ("session.next.compaction.ended", "session.compacted"):
            sess, _route = self._session_for_sid(props.get("sessionID"))
            self.mem.log_compaction()
            emit("COMPACTED", sess.title if sess is not None
                 else str(props.get("sessionID", "")))

    # -- models.json hot reload ----------------------------------------------------
    def _persist_models(self):
        """Write the ladder + current model to MODELS_FILE (best-effort): the
        file is the durable state that reload_models() reads back."""
        m = self.model or ("?", "?")
        try:
            with open(MODELS_FILE, "w") as f:
                json.dump({"model": f"{m[0]}/{m[1]}",
                           "ladder": [f"{p}/{n}" for p, n
                                      in (self.model_ladder or [])]},
                          f, indent=1)
            self._models_mtime = os.path.getmtime(MODELS_FILE)
        except OSError as e:
            emit("MODELS_PERSIST_ERROR", repr(e)[:80])

    def reload_models(self):
        """Hot-reload /data/models.json when it changes on disk (networks.json
        pattern): the file is the persistent source of truth for the ladder.
        Any live demote/probe state resets to the file's order."""
        try:
            mt = os.path.getmtime(MODELS_FILE)
        except OSError:
            return
        if mt == self._models_mtime:
            return
        self._models_mtime = mt
        try:
            with open(MODELS_FILE) as f:
                data = json.load(f)
            ladder = [split_model(m) for m in data.get("ladder", []) if m.strip()]
            model = split_model(data["model"]) if data.get("model") else None
        except (OSError, ValueError, KeyError) as e:
            emit("MODELS_FILE_ERROR", MODELS_FILE, repr(e)[:80])
            return
        if not ladder:
            return
        self.model_ladder = ladder
        self._demoted_stack = []
        self._clean_turns = 0
        if model and model != self.model:
            self.model = model
        m = self.model or ("?", "?")
        emit("MODELS_RELOADED", MODELS_FILE,
             f"model={m[0]}/{m[1]}",
             f"ladder={len(ladder)}")

    # -- model ladder ---------------------------------------------------------------
    _QUOTA_HINTS = ("free usage exceeded", "rate limit", "quota",
                    "access is disabled", "subscription")

    def _demote_model(self, sess, retry_msg):
        """On a quota/rate retry walk one rung down the ladder and swap
        self.model in place — the next turn rides the new model with
        no redeploy and no serve restart. The demoted model is persisted
        to MODELS_FILE so the choice survives restarts."""
        low = retry_msg.lower()
        if not any(h in low for h in self._QUOTA_HINTS):
            return
        if not self.model_ladder:
            return
        while self.model_ladder:
            nxt = self.model_ladder.pop(0)
            if nxt is not None and nxt != self.model:
                self._demoted_stack.append(self.model)
                self.model = nxt
                self._clean_turns = 0          # promo clock restarts
                emit("MODEL_DEMOTED", sess.title,
                     f"model={nxt[0]}/{nxt[1]}")
                self._persist_models()
                # The stuck turn is still riding the dead model's retry
                # loop — kill it now so the next submit goes out on the
                # new model immediately instead of wedging for WEDGED s.
                if self.api is not None:
                    try:
                        self.api.abort(sess.sid)
                        with self.lock:
                            sess.busy_since = None
                        emit("ABORT_SENT", sess.title,
                             "quota-retry, switch models")
                    except OSError as e:
                        emit("ABORT_ERROR", sess.title, repr(e))
                return

    def _note_clean_turn(self, sess):
        """A completed turn with no retry: count it; after PROMOTE_AFTER
        clean turns on a demoted model, probe the previous (better) model
        with one real turn — if it no longer errors, climb back up."""
        target = os.environ.get("BRAIN_PROMOTE_AFTER", "20")
        try:
            need = max(2, int(target))
        except ValueError:
            need = 20
        self._clean_turns = getattr(self, "_clean_turns", 0) + 1
        if self._clean_turns < need or not self._demoted_stack:
            return
        prev = self._demoted_stack[-1]
        emit("MODEL_PROMOTE_TRY", sess.title,
             f"model={prev[0]}/{prev[1]}")
        self.model = prev
        self._demoted_stack.pop()
        self._clean_turns = 0

    # -- best-model probe --------------------------------------------------------------
    def _probe_best(self, sess):
        """Every PROBE_SECS, poke the best model on a throwaway side
        session. Last message is an assistant turn = it's alive: promote
        it and drop everything above the current model. Chat is never
        touched; state is two variables."""
        if not self.model_ladder:
            return
        now = time.monotonic()
        if now < self._probe_next:
            return
        self._probe_next = now + PROBE_SECS
        # verdict on last cycle's probe (if any)
        if self._probe_sid:
            try:
                last = self.api.messages(self._probe_sid)[-1]
            except (OSError, IndexError):
                last = None
            if last and last.get("role") == "assistant":
                winner, self._probe_model = self._probe_model, None
                idx = self.model_ladder.index(winner)
                self._demoted_stack += self.model_ladder[:idx] + [self.model]
                self.model_ladder = self.model_ladder[idx + 1:]
                self.model = winner
                emit("MODEL_PROMOTED", sess.title,
                     f"model={winner[0]}/{winner[1]} (probe ok)")
                self._probe_sid = None
                return
        # fresh poke at the best candidate
        cand = self.model_ladder[0]
        try:
            self._probe_sid = self.api.create_session("irc:probe")["id"]
            self.api.prompt_async(self._probe_sid, "reply: SAY #probe pong",
                                  model=cand)
            self._probe_model = cand
            emit("MODEL_PROBE", sess.title, f"model={cand[0]}/{cand[1]}")
        except OSError as e:
            self._probe_sid = None
            emit("MODEL_PROBE_FAIL", sess.title, repr(e)[:60])

    # -- watchdog plumbing --------------------------------------------------------------
    def heartbeat(self):
        self.loop_count += 1
        try:
            with open(HEARTBEAT, "w") as f:
                f.write(f"{time.time():.3f} {self.loop_count}\n")
        except OSError:
            pass
        with self.lock:
            live = ([self.sess] if self.sess is not None else []) + \
                list(self.workers.values())
            snap = {s.title: {"turns": s.turns,
                              "refusals": s.refusals,
                              "wedges": s.wedges,
                              "busy": s.busy_since is not None,
                              "pending": len(s.pending)}
                    for s in live}
        try:
            with open(AGENT_STATUS, "w") as f:
                json.dump({"ts": time.time(), "turns": self.total_turns,
                           "refusals": self.total_refusals,
                           "wedges": self.total_wedges,
                           "sessions": snap}, f)
        except OSError:
            pass

    def _reap_aged(self):
        """The periodic age guard covers chat AND workers: a kept worker must
        not grow forever. Only idle sessions are reaped."""
        if self.dry_run:
            return
        now = time.monotonic()
        with self.lock:
            chat = self.sess
            workers = list(self.workers.values())
        if (chat is not None and chat.born
                and now - chat.born >= RESET_SECS
                and chat.busy_since is None):
            self.reset_session(chat, f"periodic (age>{RESET_SECS}s)")
        for worker in workers:
            if (worker.born and now - worker.born >= RESET_SECS
                    and worker.busy_since is None):
                self.reset_session(worker, f"periodic (age>{RESET_SECS}s)")

    # -- main loop --------------------------------------------------------------------
    def run(self):
        emit("START", f"nets={','.join(n.name for n in self.nets)}",
             f"model={MODEL}", "dry-run" if self.dry_run else "live",
             f"mute={','.join(MUTE) or '-'}",
             "address-only" if ADDRESS_ONLY else "all-events")
        plugin_event("start")
        self.mem = self.mem or setup_workdir()
        self.load_session_store()
        if not self.dry_run:
            self.serve = Serve.attached() if SERVE_URL else Serve.spawned()
            emit("SERVE_ATTACH" if SERVE_URL else "SERVE_UP", self.serve.url)
            plugin_event("serve_up")
            self.api = Api(self.serve.url)
        else:
            self.serve = None
            self.api = None
        tailers = [(n.name, Tailer(n.events, from_start=FROM_START))
                   for n in self.nets]
        known = {name for name, _ in tailers}
        threading.Thread(target=self.sse_loop, daemon=True).start()
        while not self.stop_flag:
            self.heartbeat()
            self.reload_models()
            # hot reconfig: networks.json on /data may add networks
            for n in networks.load():
                if n.name not in known:
                    known.add(n.name)
                    self.net_by_name[n.name] = n
                    tailers.append((n.name, Tailer(n.events)))
                    emit("NET_ADDED", n.name, f"events={n.events}")
            for net_name, tail in tailers:
                for line in tail.read_new():
                    self.feed(net_name, policy.parse_event(line))
            if self.sess is not None and self.due(self.sess):
                self.submit(self.sess)
            with self.lock:
                workers = list(self.workers.values())
            for worker in workers:
                if self.due(worker):
                    self.submit(worker)
            # Periodic age reset: a session that never idles (wedged turn)
            # would otherwise live forever across watchdog restarts. Age is a
            # hygiene/token guard, not amnesia — six hours by default.
            self._reap_aged()
            if not self.dry_run and self.sess is not None:
                self._probe_best(self.sess)
            self.guard_tick()
            # Managed serve died: respawn, then drop session state so the
            # session re-attaches against the new server.
            if self.serve and self.serve.managed and self.serve.managed.poll() is not None:
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
                        self.sess = None
                    emit("SERVE_UP", fresh.url)
                    plugin_event("serve_up")
            time.sleep(POLL)
        if self.serve:
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
