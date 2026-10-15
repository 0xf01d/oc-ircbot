"""brain v2 tests — every async path exercised against the stub SSE server.

The stub (tests/stub_opencode_serve.py) speaks the same v1 API as
`opencode serve`: POST /session, prompt_async -> 204, SSE /event with
session.status / session.idle, GET /session/{id}/message, abort, and the
permission reply endpoint. That makes these tests honest about the pipeline:
tail -> prompt_async -> SSE -> idle -> messages -> FIFO.
"""
import json
import os
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest

import brain
import memory_log
import networks
from stub_opencode_serve import StubServe


class Env:
    """One isolated brain + stub-serve world."""

    def __init__(self, tmp_path, answer=None, turn_delay=0.05, fresh=True):
        self.data = tmp_path / "data"
        self.work = tmp_path / "workspace"
        if fresh:
            self.data.mkdir()
            self.work.mkdir(parents=True)
        self.fifo = str(self.data / "bot.send")
        if fresh:
            os.mkfifo(self.fifo)
        self.events = str(self.data / "events.log")
        self.serve = StubServe(turn_delay=turn_delay, answer=answer).start()
        self.mem = memory_log.Memory(str(self.work / "memory"))
        self.mem.ensure()
        net = networks.Net("irc", self.events, self.fifo,
                           str(self.data / "bot.log"),
                           {"IRC_NICK": "ocbot"})
        self.brain = brain.Brain(nets=[net], mem=self.mem)

    def start(self):
        self._patches = [
            mock.patch.object(brain, "SERVE_URL", self.serve.url),
            mock.patch.object(brain, "POLL", 0.05),
            mock.patch.object(brain, "BATCH_WINDOW", 0.3),
            mock.patch.object(brain, "RATE", 0.0),
            mock.patch.object(brain, "GLOBAL_RATE", 0.0),
            mock.patch.object(brain, "FROM_START", True),
            mock.patch.object(brain, "WORKDIR", str(self.work)),
            mock.patch.object(brain, "HEARTBEAT",
                              str(self.data / "brain.heartbeat")),
            mock.patch.object(brain, "AGENT_STATUS",
                              str(self.data / "agent.status")),
        ]
        for p in self._patches:
            p.start()
        self.thread = threading.Thread(target=self.brain.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while self.brain.api is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert self.brain.api is not None, "brain never attached to the stub"

    def stop(self):
        self.brain.stop_flag = True
        self.thread.join(timeout=5)
        for p in reversed(self._patches):
            p.stop()
        self.serve.stop()

    def say(self, line):
        with open(self.events, "a") as f:
            f.write(line + "\n")

    def open_fifo(self):
        """Open the FIFO reader BEFORE the brain writes: a named pipe with no
        reader blocks writers, and write_fifo gives up after its deadline."""
        self._fifo_got = []

        def rdr():
            with open(self.fifo) as f:
                for line in f:
                    self._fifo_got.append(line.rstrip("\n"))

        threading.Thread(target=rdr, daemon=True).start()

    def fifo_lines(self, want=1, timeout=10):
        deadline = time.monotonic() + timeout
        while len(self._fifo_got) < want and time.monotonic() < deadline:
            time.sleep(0.05)
        return list(self._fifo_got)

    def read_fifo(self, want=1, timeout=10):
        self.open_fifo()
        return self.fifo_lines(want, timeout)


@pytest.fixture()
def world(tmp_path):
    w = Env(tmp_path)
    w.start()
    try:
        yield w
    finally:
        w.stop()


# --- pipeline: event -> prompt_async -> SSE -> FIFO ----------------------------
def test_full_roundtrip_answers_through_the_fifo(world):
    world.open_fifo()
    world.say("[12:00] MSG TRUSTED FROM=alice HOST=h TO=#test BODY=hi bot")
    lines = world.fifo_lines()
    assert lines == ["SAY #test stub-ok"]
    assert world.brain.total_turns >= 2      # rehydrate + the real turn
    # The live session pointer persists on disk for the next restart.
    store = json.load(open(str(world.work / "sessions.json")))
    assert store and next(iter(store.values())).startswith("ses_")
    # The watchdog input file is fresh and structured.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snap = json.load(open(str(world.data / "agent.status")))
        if snap["sessions"].get("irc:irc:#test", {}).get("turns", 0) >= 2:
            break
        time.sleep(0.05)
    assert snap["sessions"]["irc:irc:#test"]["turns"] >= 2


def test_muted_channel_never_creates_a_session(world):
    with mock.patch.object(brain, "MUTE", ("#sniffo",)):
        world.say("[12:00] MSG TRUSTED FROM=a HOST=h TO=#sniffo BODY=hi")
        time.sleep(0.6)
        assert not world.brain.sessions
        assert not world.serve.state.sessions


def test_own_nick_events_are_invisible(world):
    world.say("[12:00] MSG TRUSTED FROM=OCBOT HOST=h TO=#test BODY=echo")
    time.sleep(0.6)
    sess = world.brain.sessions.get(("irc", "#test"))
    assert sess is not None
    # Not in the ring, not in any prompt: our own lines do not exist.
    assert len(sess.ring) == 0
    prompts = [m for m in world.serve.state.sessions[sess.sid]["messages"]
               if m["info"]["role"] == "user"
               and "New events:" in m["parts"][0]["text"]]
    assert not prompts


def test_batching_waits_for_the_window(world):
    world.say("[12:00] MSG TRUSTED FROM=a HOST=h TO=#test BODY=one")
    time.sleep(0.1)
    world.say("[12:00] MSG TRUSTED FROM=b HOST=h TO=#test BODY=two")
    deadline = time.monotonic() + 8
    sess_key = ("irc", "#test")
    while sess_key not in world.brain.sessions and \
            time.monotonic() < deadline:
        time.sleep(0.05)
    sess = world.brain.sessions[sess_key]
    while sess.turns < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    user_prompts = [m for m in
                    world.serve.state.sessions[sess.sid]["messages"]
                    if m["info"]["role"] == "user"
                    and "New events:" in m["parts"][0]["text"]]
    assert len(user_prompts) == 1
    body = user_prompts[0]["parts"][0]["text"]
    assert "to #test (trusted): one" in body
    assert "to #test (trusted): two" in body


def _drain(fifo, timeout=10):
    got = []

    def rdr():
        with open(fifo) as f:
            while not got:
                line = f.readline()
                if line:
                    got.append(line.strip())

    th = threading.Thread(target=rdr, daemon=True)
    th.start()
    end = time.monotonic() + timeout
    while not got and time.monotonic() < end:
        time.sleep(0.05)
    return got


def test_replies_route_to_the_right_networks_fifo(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for d in (a, b):
        os.mkfifo(str(d / "bot.send"))
    serve = StubServe(turn_delay=0.05).start()
    mem = memory_log.Memory(str(tmp_path / "mem"))
    mem.ensure()
    nets = [
        networks.Net("net1", str(a / "events.log"), str(a / "bot.send"),
                     str(a / "bot.log"), {"IRC_NICK": "n1"}),
        networks.Net("net2", str(b / "events.log"), str(b / "bot.send"),
                     str(b / "bot.log"), {"IRC_NICK": "n2"}),
    ]
    br = brain.Brain(nets=nets, mem=mem)
    patches = [
        mock.patch.object(brain, "SERVE_URL", serve.url),
        mock.patch.object(brain, "POLL", 0.05),
        mock.patch.object(brain, "BATCH_WINDOW", 0.2),
        mock.patch.object(brain, "RATE", 0.0),
        mock.patch.object(brain, "GLOBAL_RATE", 0.0),
        mock.patch.object(brain, "FROM_START", True),
        mock.patch.object(brain, "WORKDIR", str(tmp_path / "work")),
        mock.patch.object(brain, "HEARTBEAT", str(tmp_path / "hb")),
        mock.patch.object(brain, "AGENT_STATUS", str(tmp_path / "as")),
    ]
    for p in patches:
        p.start()
    t = threading.Thread(target=br.run, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 10
        while br.api is None and time.monotonic() < deadline:
            time.sleep(0.05)
        got1 = _drain(str(a / "bot.send"))
        got2 = _drain(str(b / "bot.send"))
        with open(str(a / "events.log"), "a") as f:
            f.write("[12:00] MSG TRUSTED FROM=x HOST=h TO=#c BODY=hello\n")
        with open(str(b / "events.log"), "a") as f:
            f.write("[12:00] MSG TRUSTED FROM=y HOST=h TO=#c BODY=hello\n")
        deadline = time.monotonic() + 10
        while (not got1 or not got2) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert got1 == ["SAY #c stub-ok"]
        assert got2 == ["SAY #c stub-ok"]
        # Both networks got their own session, titled per (net, channel).
        titles = sorted(s["title"] for s in serve.state.sessions.values())
        assert "irc:net1:#c" in titles and "irc:net2:#c" in titles
    finally:
        br.stop_flag = True
        t.join(timeout=5)
        for p in reversed(patches):
            p.stop()
        serve.stop()


# --- agent guard: refusal, turn cap, wedged turns ------------------------------
def _refusal_answer(prompt, state):
    if "Do not send anything to IRC right now" in prompt:
        return ""
    return "SAY #test hi\nI can't help with that request."


def test_refusal_resets_the_session(tmp_path):
    w = Env(tmp_path, answer=_refusal_answer)
    w.start()
    try:
        w.say("[12:00] MSG TRUSTED FROM=a HOST=h TO=#test BODY=hi")
        deadline = time.monotonic() + 10
        while w.brain.total_refusals < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert w.brain.total_refusals == 1
        # The old session was abandoned: a fresh one owns the channel now.
        sess = w.brain.sessions[("irc", "#test")]
        assert sess.turns <= 1
        assert "refusal" in w.mem.read("project_activity_log.md")
    finally:
        w.stop()


def test_turn_cap_resets_the_session(tmp_path):
    w = Env(tmp_path)
    w.start()
    try:
        sess = w.brain.get_session("irc", "#test")
        old_sid = sess.sid
        sess.turns = brain.MAX_TURNS - 1
        w.brain.record_turn(sess, "no refusal here")
        assert w.brain.sessions[("irc", "#test")].sid != old_sid
    finally:
        w.stop()


def test_wedged_turn_aborts_then_resets(tmp_path):
    w = Env(tmp_path, turn_delay=30.0)   # a turn that never returns
    w.start()
    try:
        sess = w.brain.get_session("irc", "#test")
        with mock.patch.object(brain, "TURN_TIMEOUT", 0.1):
            w.brain.guard_tick()             # first wedge: abort, keep
            time.sleep(0.2)
            assert w.brain.total_wedges == 1
            assert sess.wedges == 1
            assert sess.sid in w.serve.state.aborted
            sess.busy_since = time.monotonic() - 1   # wedged again
            w.brain.guard_tick()             # second wedge: reset
            time.sleep(0.3)
            assert w.brain.sessions[("irc", "#test")].sid != sess.sid
    finally:
        w.stop()


# --- permission gate -------------------------------------------------------------
def test_permission_asked_is_rejected_and_announced(world):
    sess = world.brain.get_session("irc", "#test")
    world.open_fifo()
    world.brain.gate({"id": "per_1", "sessionID": sess.sid,
                      "permission": "bash", "patterns": ["rm -rf"]})
    assert world.serve.permission_denied_count() == 1
    req_id, body = world.serve.state.permission_replies[0]
    assert req_id == "per_1" and body["reply"] == "reject"
    lines = world.fifo_lines()
    assert lines and lines[0].startswith("NOTICE admin [gate] denied bash")


# --- session persistence across restarts -------------------------------------------
def test_restart_reattaches_stored_session_and_counts_turns(tmp_path):
    w = Env(tmp_path)
    w.start()
    try:
        sess = w.brain.get_session("irc", "#test")
        sess.turns = 7
        w.brain.save_session_store()
        sid = sess.sid
    finally:
        w.stop()

    w2 = Env(tmp_path, fresh=False)          # same paths, new server world
    w2.serve.state.sessions[sid] = {         # the server still knows it
        "id": sid, "title": "irc:irc:#test",
        "messages": [{"info": {"role": "assistant"},
                      "parts": [{"type": "text", "text": "old"}]}],
        "busy": False}
    w2.start()
    try:
        sess2 = w2.brain.get_session("irc", "#test")
        assert sess2.sid == sid
        assert sess2.turns == 1     # counted from the server's history
    finally:
        w2.stop()


# --- unit-level helpers --------------------------------------------------------------
def test_last_assistant_text_and_turn_count():
    msgs = [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "q"}]},
        {"info": {"role": "assistant"},
         "parts": [{"type": "text", "text": "SAY #c hi"},
                   {"type": "step-start"}]},
    ]
    assert brain.last_assistant_text(msgs) == "SAY #c hi"
    assert brain.assistant_turn_count(msgs) == 1
    assert brain.last_assistant_text([]) == ""
    assert brain.last_assistant_text(
        [{"info": {"role": "assistant"}, "parts": []}]) == ""


def test_split_model():
    assert brain.split_model("opencode/deepseek") == ("opencode", "deepseek")
    assert brain.split_model("a/b/c") == ("a", "b/c")
    assert brain.split_model("nomodel") is None
    assert brain.split_model("") is None


def test_write_fifo_roundtrip(tmp_path):
    fifo = str(tmp_path / "bot.send")
    os.mkfifo(fifo)
    got = []
    t = threading.Thread(target=lambda: got.append(open(fifo).readline()))
    t.start()
    n = brain.write_fifo(["SAY #test hi", "ACT #test waves"], path=fifo,
                         deadline=5)
    t.join(timeout=5)
    assert n == 2
    assert got[0].strip() == "SAY #test hi"


def test_write_fifo_gives_up_without_reader(tmp_path, capsys):
    fifo = str(tmp_path / "nobody.send")
    os.mkfifo(fifo)
    n = brain.write_fifo(["SAY #c hi"], path=fifo, deadline=0.5)
    assert n == 0
    assert "FIFO_WRITE_TIMEOUT" in capsys.readouterr().out


def test_registers_config_from_file(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "WORKDIR", str(tmp_path))
    assert brain.registers() == {}
    (tmp_path / "registers.json").write_text('{"#d": "calm"}')
    assert brain.registers() == {"#d": "calm"}
    (tmp_path / "registers.json").write_text("not json")
    assert brain.registers() == {}


def test_turn_knobs_and_dry_run_env_tunable(tmp_path):
    # A free-tier model can turn for minutes: MAX_TURNS / TURN_TIMEOUT must
    # stay env-tunable (hardcoding them made a slow turn an untouchable
    # abort->wedge->reset loop), and BRAIN_DRY_RUN lets compose/k8s dry-run
    # without editing the container command.
    code = "import brain; print(brain.MAX_TURNS, brain.TURN_TIMEOUT, brain.DRY_RUN)"
    env = dict(os.environ, BRAIN_MAX_TURNS="7", BRAIN_TURN_TIMEOUT="240.5",
               BRAIN_DRY_RUN="1", BRAIN_WORKDIR=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", code], env=env, check=True,
                         capture_output=True, text=True, timeout=30).stdout
    assert out.split() == ["7", "240.5", "True"]


def test_setup_workdir_installs_config_and_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(brain, "MEMORY_DIR", str(tmp_path / "memory"))
    mem = brain.setup_workdir()
    cfg = json.load(open(tmp_path / "opencode.json"))
    assert cfg["permission"]["bash"] == "deny"
    assert cfg["instructions"] == ["memory/MEMORY.md"]
    assert cfg == json.load(open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "opencode.json")))
    assert (tmp_path / "memory" / "MEMORY.md").exists()
    # A missing repo config is a hard error: the gate must never silently vanish.
    monkeypatch.setattr(brain, "__file__",
                        str(tmp_path / "nonexistent" / "brain.py"))
    (tmp_path / "opencode.json").unlink()
    with pytest.raises(RuntimeError):
        brain.setup_workdir()


def test_archive_trim_runs_at_brain_start(tmp_path, monkeypatch):
    import datetime
    monkeypatch.setattr(brain, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(brain, "MEMORY_DIR", str(tmp_path / "memory"))
    mem = brain.setup_workdir()
    old = (datetime.date(2020, 1, 1)).isoformat()
    with open(mem.path(memory_log.ACTIVITY_LOG), "a") as f:
        f.write(f"## {old}\n\n- ancient\n")
    brain.setup_workdir()
    assert "ancient" not in mem.read(memory_log.ACTIVITY_LOG)
    assert "ancient" in mem.read(memory_log.ACTIVITY_ARCHIVE)
