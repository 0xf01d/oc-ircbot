#!/usr/bin/env python3
"""handlers.py — extra FIFO verbs, hot-reloadable.

Bot loads this file (BOT_HANDLERS, default /data/handlers.py) and calls
dispatch() for verbs the built-in set does not claim. Edit the file on the
PVC and it is reloaded on the next command: no restart, the IRC connection
is never touched. A broken file is reported and ignored (previous handlers
keep working) — it can never take the bot down.

Each handler: def h_<verb>(ctx, rest, origin) -> bool | str | None
  ctx      — Handlers context (send_raw, split_say, emit, log, nick, host,
             trust_check, is_trust_listed, env, now; see bot.HandlerCtx)
  rest     — everything after the verb, verbatim (may be "")
  origin   — the optional :origin tag or None
Return True when handled; a string is sent as a PRIVMSG to its origin target
if one is known (convenience), None/False means "not handled". The verb in
`h_<verb>` is matched case-insensitively: `h_ECHO` and `h_echo` both serve
the FIFO verb ECHO.

This shipped copy doubles as the reference example (see the bottom of the
file); the copy that runs in the pod lives on the PVC and is the only one
`kubectl cp` ever touches.
"""
import importlib.util
import os
import sys
import threading

# Loader state. _path is the last path handed to load() (dispatch stats it
# for hot reload), _module the last GOOD module, _mtime its st_mtime_ns.
_path = None
_module = None
_mtime = None
_name = None        # the loaded module's sys.modules key
_seq = 0            # fresh module name per load: no stale sys.modules entry
_lock = threading.RLock()   # reentrant: a handler may import this loader


def _emit_err(emit, *parts):
    if emit is not None:
        emit(*parts)
    else:
        print(" ".join(str(p) for p in parts), flush=True)


def load(path, emit=None):
    """(Re)load the handlers file. Returns the effective module, or None
    when no file has ever loaded.

    Absence is not an error: a missing file keeps whatever is loaded. A file
    that fails to import is reported (HANDLER_IMPORT_ERROR) and IGNORED —
    the previously loaded module keeps serving, so a broken edit can never
    take the bot down. Each load gets a fresh module object built with
    spec_from_file_location (never importlib.reload on a stale module): a
    file that grew a new import must load cleanly.
    """
    global _path, _module, _mtime, _name, _seq
    with _lock:
        _path = path
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return _module
        if _module is not None and mtime == _mtime:
            return _module
        name = f"ocbot_handlers_{_seq}"
        _seq += 1
        sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            sys.modules.pop(name, None)
            _emit_err(emit, "HANDLER_IMPORT_ERROR", path, repr(e))
            return _module
        if _name is not None:
            sys.modules.pop(_name, None)
        _name = name
        _module = mod
        _mtime = mtime
        return mod


def verbs():
    """Sorted FIFO verbs the current module exposes (its h_<verb> callables);
    [] when nothing is loaded."""
    with _lock:
        mod = _module
    if mod is None:
        return []
    out = set()
    for name in dir(mod):
        if name.startswith("h_") and callable(getattr(mod, name)):
            out.add(name[2:].upper())
    return sorted(out)


def _maybe_reload(ctx):
    """Hot reload: one stat per dispatched command, re-exec only when the
    file's mtime moved. Reloading on EVERY command would re-run the module
    top level each time; mtime-gating gives the same "drop a new file in and
    it just works" behaviour without the cost."""
    with _lock:
        path = _path
        mtime = _mtime
    if path is None:
        return
    try:
        now_mtime = os.stat(path).st_mtime_ns
    except OSError:
        return                      # gone mid-flight: keep what we have
    if now_mtime == mtime:
        return
    if load(path, ctx.emit) is not None:
        ctx.emit("HANDLERS_HOT_RELOAD", f"verbs={len(verbs())}")


def dispatch(ctx, verb, rest, origin):
    """Run h_<verb> from the loaded module. Returns (handled, result).

    Any exception a handler raises is reported (HANDLER_ERROR) and swallowed:
    the bot falls through to its unknown-verb error and the FIFO reader
    thread lives on — a broken handler can never kill the bot. A non-empty
    string return is sent as a PRIVMSG to the origin tag when one is present
    (the tag is the sidecar's IRC-visible name; the bot validated its shape).
    """
    _maybe_reload(ctx)
    with _lock:
        mod = _module
    if mod is None:
        return (False, None)
    h = getattr(mod, "h_" + verb, None)
    if h is None:
        h = getattr(mod, "h_" + verb.lower(), None)
    if not callable(h):
        return (False, None)
    try:
        result = h(ctx, rest, origin)
    except Exception as e:
        ctx.emit("HANDLER_ERROR", verb, repr(e))
        return (False, None)
    if isinstance(result, str) and result and origin:
        ctx.split_say(origin, result, origin)
    return (bool(result), result if result else None)


# --- shipped examples: useful, inert, safe to delete ------------------------
# Every handler here guards on the origin tag: the FIFO itself is trusted
# (root/writer-sidecars only), but a handler that turns text into IRC lines
# should still know WHO it is talking to before it talks.

def h_echo(ctx, rest, origin):
    # Trusted-only guard: ECHO relays arbitrary text to IRC, so it answers
    # only origin tags on the bot's trust list (BOT_TRUST). Anything else —
    # or no origin tag at all, since then there is no target — is left
    # "not handled" and surfaces as the bot's unknown-verb error.
    if not origin or not ctx.is_trust_listed(origin):
        return None
    return rest            # dispatch PRIVMSGs it back to the origin


def h_pingme(ctx, rest, origin):
    # Liveness probe for the hot handlers, no trust gate needed (a NOTICE
    # carries nothing to leak):  PINGME:admin  ->  NOTICE admin :pong
    if not origin:
        return None
    ctx.send_raw(f"NOTICE {origin} :pong", origin)
    return True


# Template: a handler that fetches a URL and says the first line back to the
# origin with ctx.send_raw. Left disabled on purpose — outbound HTTP from a
# hot handler must be a deliberate act by whoever curates the PVC copy, not
# something the repo ships switched on.
#
# def h_fetch(ctx, rest, origin):
#     if not origin or not ctx.is_trust_listed(origin):
#         return None
#     url = rest.strip()
#     if not url.startswith(("http://", "https://")):
#         return None
#     import urllib.request
#     with urllib.request.urlopen(url, timeout=10) as r:
#         first = r.read(400).decode("utf-8", "replace").splitlines()[0]
#     ctx.send_raw(f"PRIVMSG {origin} :{first[:380]}", origin)
#     return True


def h_marco(ctx, rest, origin):
    if not origin:
        return None
    ctx.send_raw(f"NOTICE {origin} :marco-polo {rest}".strip(), origin)
    return True


# --- PROBE: one-shot health report for the operator -------------------------
# PROBE:katia  ->  NOTICE katia :brain=NNs model=... nets=k fifos=n verbs="..."
# Answers from what is on disk (heartbeat, models.json, FIFOs), never blocks:
# every read is a stat or a small file, guarded, so a missing file degrades to
# "?" instead of raising.
def h_probe(ctx, rest, origin):
    import json
    import os
    import time

    if not origin:
        return None

    def age(path):
        try:
            return int(time.time() - os.path.getmtime(path))
        except OSError:
            return None

    hb = age("/data/brain.heartbeat")
    st = age("/data/agent.status")

    model = "?"
    try:
        with open("/data/models.json") as f:
            model = (json.load(f).get("model") or "?").split("/")[-1]
    except (OSError, ValueError):
        pass

    try:
        nets = len([n for n in os.listdir("/data") if n.startswith("bot.send.")])
        evs = sum(os.path.getsize(f"/data/{n}") for n in os.listdir("/data")
                  if n.startswith("events.") and n.endswith(".log"))
    except OSError:
        nets, evs = 0, 0

    verbs = ",".join(sorted(
        n[2:] for n in dir(__import__("sys").modules[__name__])
        if n.startswith("h_"))) or "-"

    ctx.send_raw(
        f"NOTICE {origin} :brain={'-' if hb is None else str(hb) + 's'} "
        f"status={'-' if st is None else str(st) + 's'} model={model} "
        f"nets={nets} events={evs // 1024}k verbs={verbs}", origin)
    return True


# Trust is deliberately NOT a FIFO verb. A TRUST command here used to append
# a rule to the trust file and flip the live `verified` set, which made an IRC
# message (or anything that could write the FIFO) into a trust grant. Trust
# now comes only from the read-only config mount (/conf), reviewed and
# redeployed with the ConfigMap. There is no runtime path to a new entry.


# --- BANLIST: read-only ban-list query -------------------------------------
# `BANLIST #channel` -> `MODE #channel +b`. Strictly read-only: the only mode
# ever sent is "+b" with no mask, so this can never add/remove a ban or touch
# any other mode. The channel argument is validated hard (single token,
# starts with #) so nothing else can be spliced onto the wire.
def h_banlist(ctx, rest, origin):
    chan = rest.strip()
    if (not chan.startswith("#") or len(chan) > 64
            or any(c.isspace() or c in "\r\n" for c in chan)):
        return "usage: BANLIST #channel"
    ctx.send_raw(f"MODE {chan} +b", origin)
    ctx.emit("BANLIST", chan)
    return True


# --- VOICE: give +v to a nick, narrowly -------------------------------------
# `VOICE #channel nick` -> `MODE #channel +v nick`. Both tokens are validated
# hard (single token, channel starts with #, nick has no whitespace/CRLF/comma)
# so nothing else can be spliced onto the wire. The bot must already be op in
# the channel; otherwise the server just ignores the mode.
def h_voice(ctx, rest, origin):
    parts = rest.split()
    if len(parts) < 2:
        return "usage: VOICE #channel nick"
    chan, nick = parts[0], parts[1]
    if (not chan.startswith("#") or len(chan) > 64
            or any(c.isspace() or c in "\r\n" for c in chan)):
        return "usage: VOICE #channel nick"
    if any(c.isspace() or c in "\r\n," for c in nick) or len(nick) > 64:
        return "usage: VOICE #channel nick"
    ctx.send_raw(f"MODE {chan} +v {nick}", origin)
    ctx.emit("VOICE", f"{chan} {nick} by {origin or 'fifo'}")
    return True
