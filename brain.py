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
    /abort; refusals, turn caps and repeated wedges reset the session, which
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

OPENCODE_BIN = "opencode"
BATCH_MAX = 5          # submit a batch once it grows this big
PROBE_SECS = float(env("BRAIN_PROBE_SECS", "300"))  # best-model probe cadence
# Nothing is ever dropped for being late (vjt's model: the writer queues, the
# agent catches up) — a large backlog is only announced, loudly, here and in
# agent.status.
PENDING_BACKLOG = 128
# A free-tier model can legitimately turn for minutes: these stay env-tunable
# so a slow deploy can tune around guaranteed abort->wedge->reset loops.
MAX_TURNS = int(env("BRAIN_MAX_TURNS", "100"))         # turn cap per session
RESET_SECS = int(env("BRAIN_RESET_SECS", "900"))       # /clear cadence (15 min)
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


# --- the one session ----------------------------------------------------------
class Session:
    __slots__ = ("sid", "title", "system", "turns", "busy_since",
                 "wedges", "refusals", "pending", "last_count",
                 "lat_ms", "out_tokens", "tps", "model_id", "prompt_ts",
                 "born")

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
        self.sess = None                   # THE session (created lazily)
        self.loop_count = 0
        self.total_turns = 0
        self.total_refusals = 0
        self.total_wedges = 0
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
        self.save_session_store()
        emit("SESSION_NEW", title, sid)
        self.rehydrate(sess)
        return sess

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
        disk. Aborts first so we never reset mid-turn."""
        try:
            self.api.abort(sess.sid)
        except OSError:
            pass
        with self.lock:
            if self.sess is sess:
                self.sess = None
            leftover = sess.pending
        self.save_session_store()
        emit("SESSION_RESET", sess.title, reason, f"turns={sess.turns}")
        self.mem.append_activity(
            f"session reset ({reason}) after {sess.turns} turns: {sess.title}")
        self.registers = registers()      # registers live like the session
        fresh = self.ensure_session()
        if fresh is not None and leftover:
            with self.lock:
                fresh.pending = leftover + fresh.pending

    # -- event intake -------------------------------------------------------------
    def feed(self, net_name, ev):
        if ev is None:
            return
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
        # model turn, no batch, the answer rides the FIFO right now.
        body = (ev.get("body") or "").strip().lower()
        if (ev.get("kind") == "MSG" and body == STATS_CMD
                and ev.get("trust") == "TRUSTED"):
            with self.lock:
                sess2 = self.ensure_session()
                if sess2 is not None:
                    self.stats_reply(sess2, ev)
            return
        with self.lock:
            sess.pending.append((time.monotonic(), ev))
            depth = len(sess.pending)
        if depth >= PENDING_BACKLOG and depth % PENDING_BACKLOG == 0:
            # Visible, never dropped: a slow model must not strand events
            # silently (vjt never discards; neither do we).
            emit("PENDING_BACKLOG", sess.title, f"events={depth}")

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
                     if policy.is_addressed(
                         ev, self._net_nick(ev.get("net")))]
        prompt = policy.build_prompt([ev for _t, ev in batch], addressed)
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
            sess.prompt_ts = sess.busy_since
        emit("PROMPT_SENT", sess.title, f"events={len(batch)}")

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
        lines = [
            f"model: {sess.model_id or (self.model and self.model[1]) or '?'}"
            f"{q} | turns: {self.total_turns}",
            f"latency: {sess.lat_ms}ms | out: {sess.out_tokens} tok"
            f" | tps: {sess.tps or '?'}",
            f"refusals: {self.total_refusals} | wedges: {self.total_wedges}"
            f" | pending: {len(sess.pending)}",
            f"nets: {','.join(n.name for n in self.nets)}",
        ]
        target = policy.reply_target(ev)
        net_name = ev.get("net")
        net = self.net_by_name.get(net_name)
        if net is None:
            emit("STATS_ERROR", "unknown net", repr(net_name))
            return
        write_fifo([f"NOTICE {target} [stats] {l}" for l in lines], net.fifo)
        emit("STATS_SENT", net_name, target)

    def on_idle(self, sid):
        sess = self.sess
        if sess is None or sess.sid != sid:
            return
        try:
            msgs = self.api.messages(sid)
        except OSError as e:
            emit("COLLECT_ERROR", sess.title, repr(e))
            return
        sess.last_count = len(msgs)
        self._note_metrics(sess, msgs)
        text = last_assistant_text(msgs)
        with self.lock:
            sess.busy_since = None
        self.record_turn(sess, text)
        self._note_clean_turn(sess)
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
            n = write_fifo(lines, self.net_by_name[net_name].fifo)
            emit("SENT", n, sess.title, net_name, " ".join(lines)[:200])

    # -- agent guard (wedged turns) ---------------------------------------------------
    def guard_tick(self):
        with self.lock:
            sess = self.sess
            busy = sess is not None and sess.busy_since is not None
        if not busy:
            return
        nowm = time.monotonic()
        if nowm - sess.busy_since < TURN_TIMEOUT:
            return
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
        sess = self.sess
        if sess is not None and sess.sid != props.get("sessionID"):
            sess = None
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
            line = (f"NOTICE {ADMIN} [gate] denied {what} ({patterns})")
            for net in self.nets:
                write_fifo([line], net.fifo)

    # -- SSE loop ------------------------------------------------------------------------
    def catch_up_missed_idles(self):
        """Run on every (re)connect: collect turns that finished while the
        stream was down. A session.idle lost to a connection blip would
        otherwise leave the finished reply unfetched — channel silence, then a
        wedged-reset 180 s later for good measure (review should-fix). A turn
        still running shows a non-assistant last message and is left alone."""
        sess = self.sess
        if sess is None or sess.busy_since is None:
            return
        try:
            msgs = self.api.messages(sess.sid)
        except OSError:
            return
        if len(msgs) <= sess.last_count:
            return
        if msgs[-1].get("info", {}).get("role") != "assistant":
            return
        emit("SSE_CATCHUP", sess.title)
        self.on_idle(sess.sid)

    def sse_loop(self):
        backoff = 1.0
        if self.dry_run:
            return
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
            sess = self.sess
            if sess is not None and sess.sid == props.get("sessionID"):
                status = props.get("status") or {}
                if status.get("type") == "retry":
                    msg = str(status.get("message", ""))
                    emit("SESSION_RETRY", sess.title, msg[:120])
                    self._demote_model(sess, msg)
                elif status.get("type") == "busy":
                    sess.busy_since = sess.busy_since or time.monotonic()
        elif etype == "session.error":
            emit("SESSION_ERROR", str(props)[:200])
        elif etype == "permission.asked":
            self.gate(props)
        elif etype in ("session.next.compaction.ended", "session.compacted"):
            sess = self.sess
            self.mem.log_compaction()
            emit("COMPACTED", sess.title if sess and
                 sess.sid == props.get("sessionID")
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
            sess = self.sess
            snap = ({sess.title: {"turns": sess.turns,
                                  "refusals": sess.refusals,
                                  "wedges": sess.wedges,
                                  "busy": sess.busy_since is not None,
                                  "pending": len(sess.pending)}}
                    if sess else {})
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
             "address-only" if ADDRESS_ONLY else "all-events")
        self.mem = self.mem or setup_workdir()
        self.load_session_store()
        if not self.dry_run:
            self.serve = Serve.attached() if SERVE_URL else Serve.spawned()
            emit("SERVE_ATTACH" if SERVE_URL else "SERVE_UP", self.serve.url)
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
            # Periodic /clear: a session that never idles (wedged turn) never
            # reaches the turn-cap check in record_turn, so turns grow without
            # bound across watchdog restarts (seen: 636). Clear on age too.
            if (not self.dry_run and self.sess is not None
                    and (self.sess.turns >= MAX_TURNS
                         or (self.sess.born
                             and time.monotonic() - self.sess.born >= RESET_SECS
                             and self.sess.busy_since is None))):
                self.reset_session(self.sess,
                                   f"periodic (turns={self.sess.turns}, "
                                   f"age>{RESET_SECS}s)")
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
