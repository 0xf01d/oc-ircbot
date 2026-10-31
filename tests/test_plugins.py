"""Plugin hook tests — plug.py loader, the brain's five hooks, the bot's
outbound hook, and the API_KEY/MODELS_FILE regression (the live /app patch
used to delete those two module-level defs while install_api_key() and the
models ladder still read them; the next restart died with NameError)."""
import json
import os
import threading
import time
from unittest import mock

import pytest

import bot
import brain
import memory_log
import networks
import plug
import policy


def _bump(path):
    """Force a new mtime, relative to the file's own: one second into the
    PAST, so it stays distinct under any timestamp granularity (a future
    value can truncate onto the current second on some filesystems)."""
    t = os.stat(path).st_mtime_ns - 1_000_000_000
    os.utime(path, ns=(t, t))


def _plugin(path, hooks):
    """Write a plugin file. `hooks` maps hook name -> (params, ret) where
    ret is the literal return expression (None = no return statement).
    Every hook first appends a JSON line [name, str(arg)...] to <path>.log,
    so tests assert what actually ran without importing the module."""
    rec = str(path) + ".log"
    out = [
        "import json",
        f"RECORD = {rec!r}",
        "def _rec(name, args):",
        "    with open(RECORD, 'a') as f:",
        "        f.write(json.dumps([name] + [str(a)[:120] for a in args])"
        " + '\\n')",
    ]
    for name, (params, ret) in hooks.items():
        out.append(f"def {name}({', '.join(params)}):")
        out.append(f"    _rec({name!r}, ({', '.join(params)},))")
        if ret is not None:
            out.append(f"    return {ret}")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    return rec


def _records(rec):
    if not os.path.exists(rec):
        return []                            # no hook ever fired
    with open(rec) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


@pytest.fixture()
def brain_plugin(tmp_path):
    """The brain's module-level plugin swapped for a per-test file."""
    old = brain.plugin
    p = plug.Plugin(str(tmp_path / "brain_plugin.py"), brain.emit)
    brain.plugin = p
    try:
        yield p
    finally:
        brain.plugin = old


@pytest.fixture()
def bot_plugin(tmp_path):
    old = bot.bot_plugin
    p = plug.Plugin(str(tmp_path / "bot_plugin.py"), bot.emit)
    bot.bot_plugin = p
    try:
        yield p
    finally:
        bot.bot_plugin = old


def _bare_brain(tmp_path, label="irc"):
    """A Brain with no threads; feed/write_fifo are called direct."""
    mem = memory_log.Memory(str(tmp_path / "mem"))
    mem.ensure()
    os.mkfifo(str(tmp_path / f"bot.send.{label}"))
    net = networks.Net(label, str(tmp_path / f"events.{label}.log"),
                       str(tmp_path / f"bot.send.{label}"),
                       str(tmp_path / f"bot.{label}.log"),
                       {"IRC_NICK": "ocbot"})
    return brain.Brain(nets=[net], mem=mem), net


def _feed(brain_obj, body="hello there", target="#test"):
    ev = policy.parse_event(
        f"[12:00] MSG TRUSTED FROM=alice HOST=a@h TO={target} BODY={body}")
    brain_obj.feed("irc", ev)
    return ev


class FifoReader:
    """FIFO reader armed BEFORE the write: write_fifo opens O_NONBLOCK and a
    pipe with no reader rejects writers, so the reader must already exist
    (the test_brain.Env.open_fifo pattern). Reopens in a loop since the
    brain closes the pipe after every send."""

    def __init__(self, fifo):
        self.fifo = fifo
        self.lines = []
        self.opened = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.opened:
            try:
                with open(self.fifo) as f:
                    for line in f:
                        self.lines.append(line.rstrip("\n"))
            except OSError:
                time.sleep(0.02)

    def line(self, timeout=5):
        deadline = time.monotonic() + timeout
        while not self.lines and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.lines[0] if self.lines else None


# --- plug.py loader ------------------------------------------------------------
def test_missing_file_is_a_silent_noop(tmp_path):
    logs = []
    p = plug.Plugin(str(tmp_path / "nope.py"), lambda *a: logs.append(a))
    assert p.load() is None
    assert p.call("drop", object()) is plug.MISSING
    assert logs == []                        # absence is not an error


def test_load_and_call(tmp_path):
    path = str(tmp_path / "p.py")
    with open(path, "w") as f:
        f.write("VAL = 41\ndef val():\n    return VAL\n")
    logs = []
    p = plug.Plugin(path, lambda *a: logs.append(a))
    assert p.load() is not None
    assert logs[-1][:2] == ("PLUGIN_LOADED", path)
    assert p.call("val") == 41
    assert p.call("nothing_here") is plug.MISSING
    assert p.call("VAL") is plug.MISSING     # call() runs hooks, not data


def test_hot_reload_when_mtime_moves(tmp_path):
    path = str(tmp_path / "p.py")
    with open(path, "w") as f:
        f.write("def val():\n    return 1\n")
    p = plug.Plugin(path, lambda *a: None)
    assert p.call("val") == 1
    with open(path, "w") as f:
        f.write("def val():\n    return 2\n")
    _bump(path)
    assert p.call("val") == 2


def test_broken_file_keeps_previous_module(tmp_path):
    path = str(tmp_path / "p.py")
    with open(path, "w") as f:
        f.write("def val():\n    return 1\n")
    logs = []
    p = plug.Plugin(path, lambda *a: logs.append(a))
    p.call("val")
    with open(path, "w") as f:
        f.write("this is not python (")
    _bump(path)
    assert p.call("val") == 1                # previous keeps serving
    assert any(a[0] == "PLUGIN_IMPORT_ERROR" for a in logs)


def test_raising_hook_is_logged_and_reads_noop(tmp_path):
    path = str(tmp_path / "p.py")
    with open(path, "w") as f:
        f.write("def boom(x):\n    raise ValueError('nope')\n")
    logs = []
    p = plug.Plugin(path, lambda *a: logs.append(a))
    assert p.call("boom", 1) is plug.MISSING
    assert logs[-1][:2] == ("PLUGIN_ERROR", "boom")
    assert "ValueError" in logs[-1][2]


# --- brain: drop ----------------------------------------------------------------
def test_drop_never_batches(tmp_path, brain_plugin):
    _plugin(brain_plugin.path, {"drop": (["ev", "net"], "True")})
    b, _net = _bare_brain(tmp_path)
    _feed(b)
    assert b.sess is None or not b.sess.pending


def test_drop_sees_event_and_net(tmp_path, brain_plugin):
    rec = _plugin(brain_plugin.path, {"drop": (["ev", "net"], "True")})
    b, _net = _bare_brain(tmp_path)
    _feed(b)
    rows = _records(rec)
    assert rows[0][0] == "drop"
    assert "BODY=hello there" in rows[0][1]
    assert rows[0][2] == "irc"


def test_drop_false_falls_through(tmp_path, brain_plugin):
    _plugin(brain_plugin.path, {"drop": (["ev", "net"], "False")})
    b, _net = _bare_brain(tmp_path)
    _feed(b)
    assert len(b.sess.pending) == 1


def test_drop_returning_nothing_keeps_the_event(tmp_path, brain_plugin):
    _plugin(brain_plugin.path, {"drop": (["ev", "net"], None)})
    b, _net = _bare_brain(tmp_path)
    _feed(b)
    assert len(b.sess.pending) == 1


# --- brain: rewrite ---------------------------------------------------------------
def test_rewrite_replaces_the_event(tmp_path, brain_plugin):
    _plugin(brain_plugin.path, {
        "rewrite": (["ev", "net"],
                    "({'kind': ev['kind'], 'raw': ev['raw'], "
                    "'to': ev['to'], 'from': ev['from'], "
                    "'body': ev['body'].upper(), 'trust': ev['trust']})")})
    b, _net = _bare_brain(tmp_path)
    _feed(b, body="shout this")
    assert b.sess.pending[0][1]["body"] == "SHOUT THIS"


def test_rewrite_may_mutate_in_place(tmp_path, brain_plugin):
    _plugin(brain_plugin.path, {"rewrite": (["ev", "net"], "ev")})
    b, _net = _bare_brain(tmp_path)
    _feed(b)
    assert len(b.sess.pending) == 1          # ev returned as-is


def test_rewrite_none_drops(tmp_path, brain_plugin):
    _plugin(brain_plugin.path, {"rewrite": (["ev", "net"], "None")})
    b, _net = _bare_brain(tmp_path)
    _feed(b)
    assert b.sess is None or not b.sess.pending


def test_later_checks_run_on_the_rewritten_event(tmp_path, brain_plugin):
    # Mute runs after rewrite on the REPLACED event: a plugin cannot route
    # around the built-in gates by rewriting.
    _plugin(brain_plugin.path, {
        "rewrite": (["ev", "net"],
                    "dict(ev, raw=ev['raw'].replace('#test', '#sniffo'), "
                    "to='#sniffo')")})
    b, _net = _bare_brain(tmp_path)
    with mock.patch.object(brain, "MUTE", ("#sniffo",)):
        _feed(b)
    assert b.sess is None or not b.sess.pending


# --- brain: handle ----------------------------------------------------------------
def test_handle_serves_without_a_model_turn(tmp_path, brain_plugin):
    rec = _plugin(brain_plugin.path, {
        "handle": (["ev", "net", "ctx"],
                   "ctx.say(ev['to'], 'pong') or True")})
    b, net = _bare_brain(tmp_path)
    r = FifoReader(net.fifo)
    _feed(b, body="!ping")
    assert r.line() == "SAY #test pong"
    assert b.sess is None or not b.sess.pending   # nothing batched
    assert _records(rec)[0][0] == "handle"


def test_handle_false_falls_through_to_the_batch(tmp_path, brain_plugin):
    _plugin(brain_plugin.path, {"handle": (["ev", "net", "ctx"], "False")})
    b, _net = _bare_brain(tmp_path)
    _feed(b)
    assert len(b.sess.pending) == 1


def test_handle_ctx_carries_net_nick_and_senders(tmp_path, brain_plugin):
    # ctx is a namespace built per call: net, nick, say/notice/send bound to
    # this net's FIFO. The plugin uses send() with a raw verb line.
    rec = _plugin(brain_plugin.path, {
        "handle": (["ev", "net", "ctx"],
                   "ctx.notice(ctx.nick, f\"{ctx.net}|{ctx.nick}\") or True")})
    b, net = _bare_brain(tmp_path)
    r = FifoReader(net.fifo)
    _feed(b)
    assert r.line() == "NOTICE ocbot irc|ocbot"
    assert _records(rec)[0][0] == "handle"


def test_stats_builtin_wins_over_handle(tmp_path, brain_plugin):
    # The brief pins the order: !stats is checked BEFORE handle, so the
    # built-in answers even when the plugin's handle would claim everything.
    rec = _plugin(brain_plugin.path, {"handle": (["ev", "net", "ctx"], "True")})
    b, _net = _bare_brain(tmp_path)
    with mock.patch.object(brain, "write_fifo", return_value=1) as wf:
        ev = policy.parse_event(
            "[12:00] MSG TRUSTED FROM=alice HOST=a@h TO=#test BODY=!stats")
        b.feed("irc", ev)
    lines = [c.args[0][0] for c in wf.call_args_list]
    assert any(l.startswith("NOTICE #test [stats]") for l in lines)
    assert _records(rec) == []               # the plugin never saw it


def test_stats_does_not_deadlock_on_the_session_lock(tmp_path):
    # Regression (found by this lane): the !stats branch used to wrap
    # ensure_session() — which takes self.lock itself — in `with self.lock:`,
    # wedging the whole brain on every !stats a trusted user typed.
    b, _net = _bare_brain(tmp_path)
    b.sess = brain.Session("s1", "irc:global", "sys")
    ev = policy.parse_event(
        "[12:00] MSG TRUSTED FROM=alice HOST=a@h TO=#test BODY=!stats")
    with mock.patch.object(brain, "write_fifo", return_value=1) as wf:
        b.feed("irc", ev)                     # returns: no deadlock
    assert wf.called


# --- brain: outbound ---------------------------------------------------------------
def test_outbound_rewrites_the_fifo_line(tmp_path, brain_plugin):
    _plugin(brain_plugin.path, {
        "outbound": (["net", "verb", "target", "text"],
                     "text.replace('hunter2', '[redacted]')")})
    b, net = _bare_brain(tmp_path)
    r = FifoReader(net.fifo)
    n = brain.write_fifo(["SAY #test pass is hunter2"], net.fifo, net="irc")
    assert n == 1
    assert r.line() == "SAY #test pass is [redacted]"


def test_outbound_none_blocks_and_logs(tmp_path, brain_plugin, capsys):
    _plugin(brain_plugin.path, {"outbound": (["net", "verb", "target", "text"],
                                             "None")})
    b, net = _bare_brain(tmp_path)
    n = brain.write_fifo(["SAY #test blocked"], net.fifo, net="irc")
    assert n == 0
    assert "OUTBOUND_BLOCKED irc SAY #test" in capsys.readouterr().out


def test_outbound_only_sees_say_act_notice(tmp_path, brain_plugin):
    rec = _plugin(brain_plugin.path, {"outbound": (["net", "verb", "target",
                                                    "text"], "None")})
    b, net = _bare_brain(tmp_path)
    FifoReader(net.fifo)                      # writes must not time out
    brain.write_fifo(["QUIT bye", "JOIN #x", "NOTICE #c hi"], net.fifo,
                     net="irc")
    rows = _records(rec)
    assert rows == [["outbound", "irc", "NOTICE", "#c", "hi"]]  # QUIT/JOIN untouched


def test_outbound_without_net_label_is_not_hooked(tmp_path, brain_plugin):
    # Bare write_fifo callers (plumbing, tests) keep the exact old behaviour.
    rec = _plugin(brain_plugin.path, {"outbound": (["net", "verb", "target",
                                                    "text"], "None")})
    b, net = _bare_brain(tmp_path)
    r = FifoReader(net.fifo)
    assert brain.write_fifo(["SAY #test direct"], net.fifo) == 1
    assert r.line() == "SAY #test direct"
    assert _records(rec) == []


# --- brain: event -------------------------------------------------------------------
def test_event_hook_receives_lifecycle_events(tmp_path, brain_plugin):
    rec = _plugin(brain_plugin.path, {"event": (["name", "payload"], None)})
    b, _net = _bare_brain(tmp_path)
    brain.plugin_event("start")
    brain.plugin_event("turn_end", turns=3, seconds=1.25)
    rows = _records(rec)
    assert rows == [["event", "start", "{}"],
                    ["event", "turn_end", "{'turns': 3, 'seconds': 1.25}"]]


def test_event_hook_never_breaks_the_caller(tmp_path, brain_plugin):
    path = str(tmp_path / "p.py")
    with open(path, "w") as f:
        f.write("def event(name, payload):\n    raise RuntimeError('x')\n")
    brain_plugin.path = path
    brain.plugin_event("reset", reason="r")   # must not raise


# --- resilience ------------------------------------------------------------------------
def test_raising_plugin_behaves_as_noop(tmp_path, brain_plugin, capsys):
    _plugin(brain_plugin.path, {
        "drop": (["ev", "net"], "1/0"),
        "rewrite": (["ev", "net"], "1/0"),
        "handle": (["ev", "net", "ctx"], "1/0"),
        "outbound": (["net", "verb", "target", "text"], "1/0"),
        "event": (["name", "payload"], "1/0")})
    b, net = _bare_brain(tmp_path)
    _feed(b)                                  # no exception, event batched
    assert len(b.sess.pending) == 1
    brain.plugin_event("start")               # raising event hook: no-op
    r = FifoReader(net.fifo)
    n = brain.write_fifo(["SAY #test x"], net.fifo, net="irc")
    assert n == 1                             # raising outbound != a block
    assert r.line() == "SAY #test x"
    out = capsys.readouterr().out
    for hook in ("drop", "rewrite", "handle", "outbound", "event"):
        assert f"PLUGIN_ERROR {hook}" in out


def test_broken_plugin_file_is_reported_not_fatal(tmp_path, brain_plugin,
                                                  capsys):
    with open(brain_plugin.path, "w") as f:
        f.write("def drop(ev, net)\n    return True\n")   # syntax error
    b, _net = _bare_brain(tmp_path)
    _feed(b)
    assert len(b.sess.pending) == 1
    assert "PLUGIN_IMPORT_ERROR" in capsys.readouterr().out


def test_missing_plugin_file_changes_nothing(tmp_path, brain_plugin):
    brain_plugin.path = str(tmp_path / "never-written.py")
    b, _net = _bare_brain(tmp_path)
    _feed(b)
    assert len(b.sess.pending) == 1


# --- regression: the /app patch's NameError ------------------------------------------------
def test_module_level_defs_survive():
    # The live brain.py patch deleted API_KEY and MODELS_FILE at module level
    # while install_api_key() and the models ladder still read them. Real
    # code must expose all of them, always.
    for name in ("API_KEY", "MODELS_FILE", "STATS_CMD", "MODEL",
                 "BATCH_MAX", "PREEMPT_ON_ADDRESS"):
        assert hasattr(brain, name), name
    assert brain.BATCH_MAX == 25


def test_install_api_key_with_empty_key_no_nameerror(capsys):
    with mock.patch.object(brain, "API_KEY", ""):
        brain.install_api_key()               # must not raise NameError
    assert "AUTH_WRITTEN" not in capsys.readouterr().out


# --- bot: outbound ------------------------------------------------------------------------
def test_bot_outbound_replaces_text(bot_plugin):
    _plugin(bot_plugin.path, {
        "outbound": (["target", "text", "verb"],
                     "text.replace('secret', '[censored]')")})
    with mock.patch.object(bot, "send_raw") as sr:
        bot.process_cmd("SAY #c a secret here")
    assert sr.call_args.args[0] == "PRIVMSG #c :a [censored] here"


def test_bot_outbound_none_blocks(bot_plugin):
    _plugin(bot_plugin.path, {"outbound": (["target", "text", "verb"],
                                           "None")})
    with mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit") as em:
        bot.process_cmd("SAY #c drop it")
    sr.assert_not_called()
    assert any(c.args[:3] == ("BOT_PLUGIN_BLOCKED", "SAY", "#c")
               for c in em.call_args_list)


def test_bot_outbound_runs_after_the_leak_guard(bot_plugin):
    rec = _plugin(bot_plugin.path, {"outbound": (["target", "text", "verb"],
                                                 "None")})
    with mock.patch.object(bot, "send_raw"), \
            mock.patch.object(bot, "outbound_ok", return_value=False):
        bot.process_cmd("SAY #c leaky")       # the leak gate blocks first...
    assert _records(rec) == []                # ...the plugin never runs


def test_bot_outbound_covers_act_and_notice(bot_plugin):
    _plugin(bot_plugin.path, {
        "outbound": (["target", "text", "verb"],
                     "text.upper() if verb == 'ACT' else None")})
    with mock.patch.object(bot, "send_raw") as sr:
        bot.process_cmd("ACT #c waves")
        bot.process_cmd("NOTICE nick hello")
    assert sr.call_args_list[0].args[0].endswith("WAVES\x01")
    assert sr.call_count == 1                 # NOTICE blocked by the None


def test_bot_outbound_skips_non_chat_verbs(bot_plugin):
    rec = _plugin(bot_plugin.path, {"outbound": (["target", "text", "verb"],
                                                 "None")})
    with mock.patch.object(bot, "send_raw"):
        bot.process_cmd("QUIT bye")
    assert _records(rec) == []


def test_bot_missing_plugin_is_a_noop(bot_plugin):
    bot_plugin.path = "/nonexistent/bot_plugin.py"
    with mock.patch.object(bot, "send_raw") as sr:
        bot.process_cmd("SAY #c hi")
    sr.assert_called_once_with("PRIVMSG #c :hi", None)
