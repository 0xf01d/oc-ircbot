"""Hot-reloadable external command handlers (handlers.py + bot wiring).

Each test starts from a pristine loader state: the autouse fixture saves and
restores handlers' module globals, so no test can leak a loaded module into
another (the loader state is process-global by design — it IS the bot's).
"""
import os
from unittest import mock

import pytest

import bot
import handlers


@pytest.fixture(autouse=True)
def _pristine_loader():
    saved = (handlers._path, handlers._module, handlers._mtime,
             handlers._name, handlers._seq, bot.trust_rules)
    handlers._path = handlers._module = handlers._mtime = None
    handlers._name = None
    handlers._seq = 0
    yield
    (handlers._path, handlers._module, handlers._mtime,
     handlers._name, handlers._seq, bot.trust_rules) = saved


def touch(path):
    """Bump the mtime by a guaranteed-visible ns step (a same-ns rewrite on a
    coarse filesystem would look like "unchanged" to the hot-reload gate)."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


ECHO_SRC = "def h_echo(ctx, rest, origin):\n    return rest\n"
PINGME_SRC = ("def h_pingme(ctx, rest, origin):\n"
              "    ctx.send_raw(f'NOTICE {origin} :pong', origin)\n"
              "    return True\n")


def test_dispatch_with_nothing_loaded_is_not_handled():
    class Ctx:
        pass

    assert handlers.dispatch(Ctx(), "WHATEVER", "rest", None) == (False, None)
    assert handlers.verbs() == []


def test_handler_is_loaded_and_dispatched_from_a_fifo_line(tmp_path):
    hf = tmp_path / "handlers.py"
    hf.write_text(ECHO_SRC)
    assert handlers.load(str(hf)) is not None
    assert handlers.verbs() == ["ECHO"]
    bot.trust_rules = [("admin", "*")]      # ECHO's trusted-only guard
    with mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit"):
        bot.process_cmd("ECHO:admin hi there")
    assert sr.call_args.args == ("PRIVMSG admin :hi there", "admin")


def test_uppercase_handler_names_serve_too(tmp_path):
    hf = tmp_path / "handlers.py"
    hf.write_text("def h_ECHO(ctx, rest, origin):\n    return rest\n")
    handlers.load(str(hf))
    assert handlers.verbs() == ["ECHO"]
    bot.trust_rules = [("admin", "*")]
    with mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit"):
        bot.process_cmd("ECHO:admin upper")
    assert sr.call_args.args == ("PRIVMSG admin :upper", "admin")


def test_load_is_mtime_gated(tmp_path):
    hf = tmp_path / "handlers.py"
    hf.write_text(ECHO_SRC)
    first = handlers.load(str(hf))
    assert handlers.load(str(hf)) is first      # unchanged: no re-exec


def test_new_verb_works_without_reconnect(tmp_path):
    hf = tmp_path / "handlers.py"
    hf.write_text(ECHO_SRC)
    handlers.load(str(hf))
    sentinel = mock.Mock()      # the live IRC socket; a reconnect would swap it
    with mock.patch.object(bot, "sock", sentinel), \
            mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit") as em:
        bot.process_cmd("PINGME:admin")         # not a verb yet -> unknown
        hf.write_text(ECHO_SRC + PINGME_SRC)
        touch(hf)                               # kubectl cp equivalent
        bot.process_cmd("PINGME:admin")         # picked up, same process
        assert bot.sock is sentinel             # nothing rebuilt the socket
    assert any(c.args[0] == "NOTICE admin :pong" for c in sr.call_args_list)
    assert any(c.args[:2] == ("HANDLERS_HOT_RELOAD", "verbs=2")
               for c in em.call_args_list)
    assert not any(c.args[0] == "CONNECTED" for c in em.call_args_list)


def test_raising_handler_is_reported_and_bot_keeps_going(tmp_path):
    hf = tmp_path / "handlers.py"
    hf.write_text("def h_boom(ctx, rest, origin):\n"
                  "    raise RuntimeError('kaboom')\n")
    handlers.load(str(hf))
    with mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit") as em:
        bot.process_cmd("BOOM:x")
        assert any(c.args[:2] == ("HANDLER_ERROR", "BOOM")
                   for c in em.call_args_list)
        # falls through: a broken handler is just an unknown verb...
        assert any(c.args[:2] == ("CMD_ERROR", "unknown-verb")
                   for c in em.call_args_list)
        # ...and the FIFO reader lives on.
        bot.process_cmd("SAY #c still alive")
    assert sr.call_args.args == ("PRIVMSG #c :still alive", None)


def test_broken_file_is_reported_and_previous_verbs_survive(tmp_path):
    hf = tmp_path / "handlers.py"
    hf.write_text(ECHO_SRC)
    handlers.load(str(hf))
    bot.trust_rules = [("admin", "*")]
    with mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit") as em:
        hf.write_text("def h_echo(:\n    this is not python\n")
        touch(hf)
        bot.process_cmd("ECHO:admin still works")
    assert any(c.args[0] == "HANDLER_IMPORT_ERROR" for c in em.call_args_list)
    assert sr.call_args.args == ("PRIVMSG admin :still works", "admin")


def test_load_handlers_startups(tmp_path):
    # absent: not an error
    with mock.patch.object(bot, "HANDLERS_FILE", str(tmp_path / "nope.py")), \
            mock.patch.object(bot, "emit") as em:
        bot.load_handlers()
    assert any(c.args[0] == "HANDLERS_MISSING" for c in em.call_args_list)
    # present but broken: import error, and never claimed as loaded
    broken = tmp_path / "broken.py"
    broken.write_text("def h_echo(:\n")
    with mock.patch.object(bot, "HANDLERS_FILE", str(broken)), \
            mock.patch.object(bot, "emit") as em:
        bot.load_handlers()
    assert any(c.args[0] == "HANDLER_IMPORT_ERROR" for c in em.call_args_list)
    assert not any(c.args[0] == "HANDLERS_LOADED" for c in em.call_args_list)
    # good file: loaded, with the verb count in the event
    good = tmp_path / "good.py"
    good.write_text(ECHO_SRC)
    with mock.patch.object(bot, "HANDLERS_FILE", str(good)), \
            mock.patch.object(bot, "emit") as em:
        bot.load_handlers()
    assert any(c.args[:1] == ("HANDLERS_LOADED",) and c.args[2] == "verbs=1"
               for c in em.call_args_list)


def test_file_appearing_after_missing_startup_is_picked_up(tmp_path):
    hf = tmp_path / "handlers.py"
    assert handlers.load(str(hf)) is None     # absent at startup
    assert handlers.verbs() == []
    hf.write_text(ECHO_SRC)                   # dropped in later
    bot.trust_rules = [("admin", "*")]
    with mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit"):
        bot.process_cmd("ECHO:admin late arrival")
    assert sr.call_args.args == ("PRIVMSG admin :late arrival", "admin")


def test_reload_verb_rereads_the_file(tmp_path):
    hf = tmp_path / "handlers.py"
    hf.write_text(ECHO_SRC)
    with mock.patch.object(bot, "HANDLERS_FILE", str(hf)), \
            mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit") as em:
        bot.process_cmd("RELOAD:admin")
        assert any(c.args[:2] == ("HANDLERS_RELOADED", "verbs=1")
                   for c in em.call_args_list)
        assert any(c.args[0] == "NOTICE admin :handlers reloaded, verbs=1"
                   for c in sr.call_args_list)
        hf.write_text(ECHO_SRC + PINGME_SRC)
        touch(hf)
        bot.process_cmd("RELOAD:admin")
        assert any(c.args[:2] == ("HANDLERS_RELOADED", "verbs=2")
                   for c in em.call_args_list)
        bot.process_cmd("PINGME:admin")
    assert any(c.args[0] == "NOTICE admin :pong" for c in sr.call_args_list)


def test_unknown_verb_detail_lists_the_hot_verbs(tmp_path):
    hf = tmp_path / "handlers.py"
    hf.write_text(ECHO_SRC)
    handlers.load(str(hf))
    with mock.patch.object(bot, "emit") as em, \
            mock.patch.object(bot, "send_raw"):
        bot.process_cmd("WAT")
    err = next(c for c in em.call_args_list if c.args[0] == "CMD_ERROR")
    assert err.args[1] == "unknown-verb"
    assert "ECHO" in err.args[-1]        # the log says what exists


def test_shipped_handlers_have_no_trust_grant_path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "handlers.py")) as f:
        src = f.read()
    # The live /data copy had h_trust/h_untrust; they are deliberately gone.
    # Trust comes from /conf only. A message handler must never write the set.
    assert "def h_trust" not in src
    assert "def h_untrust" not in src
    assert "TRUST_FORCED" not in src
    assert "UNTRUST_FORCED" not in src
    assert "TRUST_FILE" not in src


def test_trust_is_not_a_fifo_verb():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert handlers.load(os.path.join(root, "handlers.py")) is not None
    verbs = handlers.verbs()
    assert "TRUST" not in verbs and "UNTRUST" not in verbs
