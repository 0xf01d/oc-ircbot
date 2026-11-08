#!/usr/bin/env python3
"""Apply the IRC-command dispatch hook to /app/bot.py (idempotent).

Run inside the bot container:
    python3 /data/apply_ab2filter_patch.py
It backs up the original to /app/bot.py.bak-ab2filter and only inserts the
hook once (marker: IRC_COMMANDS_FILE).
"""
import os
import shutil
import sys

BOT = "/app/bot.py"
MARK = "IRC_COMMANDS_FILE"

FUNC = '''

# --- persistent IRC commands (!ab2filter et al.) -----------------------------
# The command bodies live in /data/irc_commands.py on the PVC, so they survive
# bot restarts; this hook is the only part that lives in the image. Hot reload
# is mtime-gated like handlers.py.
IRC_COMMANDS_FILE = env("BOT_IRC_COMMANDS", "/data/irc_commands.py")
_irc_mod = None
_irc_mtime = None
_irc_lock = threading.Lock()


def _irc_command_dispatch(target, nick, body, trusted):
    global _irc_mod, _irc_mtime
    try:
        mtime = os.stat(IRC_COMMANDS_FILE).st_mtime_ns
    except OSError:
        return
    with _irc_lock:
        if _irc_mod is None or mtime != _irc_mtime:
            import importlib.util
            name = "ocbot_irc_commands_%d" % int(time.time() * 1000)
            spec = importlib.util.spec_from_file_location(name, IRC_COMMANDS_FILE)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception as e:
                sys.modules.pop(name, None)
                emit("IRC_CMD_IMPORT_ERROR", repr(e))
                return
            _irc_mod = mod
            _irc_mtime = mtime
            emit("IRC_COMMANDS_LOADED", IRC_COMMANDS_FILE)
        mod = _irc_mod
    mod.handle(target, nick, body, trusted, send_raw, emit, split_say)

'''

HOOK = '''        if target.startswith("#") and body.startswith("!"):
            try:
                _irc_command_dispatch(target, nick, body, trusted)
            except Exception as e:
                emit("IRC_CMD_ERROR", repr(e))
'''


def main():
    with open(BOT) as f:
        src = f.read()
    if MARK in src:
        print("already patched")
        return 0
    anchor = "\ndef process_cmd(line):"
    if anchor not in src:
        print("ERROR: process_cmd anchor not found", file=sys.stderr)
        return 1
    src = src.replace(anchor, FUNC + anchor, 1)
    hook_anchor = '''        if target.startswith("#") and nick.lower() != NICK.lower():
            _arm_idle(target)
        return
'''
    if hook_anchor not in src:
        print("ERROR: PRIVMSG hook anchor not found", file=sys.stderr)
        return 1
    src = src.replace(hook_anchor, HOOK + hook_anchor, 1)
    shutil.copy2(BOT, BOT + ".bak-ab2filter")
    with open(BOT, "w") as f:
        f.write(src)
    print("patched", BOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
