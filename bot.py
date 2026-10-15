#!/usr/bin/env python3
"""bot.py — minimal IRC bridge between an AI brain and IRC.

A close port of vjt/claude-ircbot's bot.py: TLS IRC transport, trust model,
and a FIFO command grammar (SAY ACT NOTICE JOIN PART WHOIS QUIT RAW). It is
brain-agnostic: selected events go to stdout and BOT_EVENTS for a sidecar
brain (brain.py) to tail, and commands arrive over the FIFO.
"""
import fnmatch
import os
import random
import re
import socket
import ssl
import sys
import threading
import time

from leak_guard import LeakGuard
from util import env, env_bool, now

# Env-overridable paths so a second instance (e.g. Libera.Chat) runs the SAME
# code with its own nick / FIFO / logs / trust / startup.
HERE = os.path.dirname(os.path.abspath(__file__))
HOST = env("IRC_HOST", "irc.azzurra.chat")
PORT = int(env("IRC_PORT", "6697"))
NICK = env("IRC_NICK", "vjt-claude")
IDENT = env("IRC_IDENT", "claude")
REAL = env("IRC_REAL", "github.com/vjt/claude-ircbot")
# Source address to bind: two bots sharing a jail need two vhosts, and on
# Azzurra the vhost is the rDNS of the source address (the address IS the
# identity). Port stays 0 — we want the address, not a fixed port.
BIND = env("IRC_BIND", "")
# TLS verify is on by default (a password rides the wire). IRC_TLS_VERIFY=0
# exists for IRCnet: no services, nothing to protect, and its certs do not
# validate (vjt's order, 2026-08-25).
TLS_VERIFY = env_bool("IRC_TLS_VERIFY", True)
TLS = env_bool("IRC_TLS", True)          # plaintext escape hatch for smoke tests

LOG = env("BOT_LOG", os.path.join(HERE, "bot.log"))
FIFO = env("BOT_FIFO", os.path.join(HERE, "bot.send"))
# Event stream for sidecar brains: a second container tails this instead of stdout.
EVENTS_FILE = env("BOT_EVENTS", "/data/events.log")
TRUST_FILE = env("BOT_TRUST", os.path.join(HERE, "bot.trust"))
ENV_FILE = env("BOT_ENV", os.path.join(HERE, ".env"))
STARTUP_FILE = env("BOT_STARTUP", os.path.join(HERE, "bot.startup"))
# Shared by ALL instances on purpose (not per-network): see leak_guard.py.
RESERVED_FILE = env("BOT_RESERVED", os.path.join(HERE, "bot.reserved"))
RESERVED_STORE = env("BOT_RESERVED_STORE", os.path.join(HERE, "reserved_recent.tsv"))


def load_env():
    out = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


ENV = load_env()
NICKSERV_PASS = ENV.get("NICKSERV_PASS") or os.environ.get("NICKSERV_PASS") or ""
startup_fired = False

# Ghost-on-connect: a line drop leaves our old connection holding NICK as a
# ghost for ~7min, and every reconnect hits 433 pre-001. On a pre-001 433 take
# a throwaway nick to reach 001, GHOST the stale nick from services, reclaim
# it, then IDENTIFY — otherwise registration never completes and systemd
# restarts us into a flap loop.
registered = False      # True once we've received 001 (registration complete)
ghost_needed = False    # True while we hold a temp nick and still want NICK back
tmp_nick = None

# Trust = listed in trust_rules AND host matches the nick's glob AND 307/330
# confirmed the nick is registered to services. Cache reset on PART/QUIT/NICK.
trust_rules = []        # list of (nick_lower, host_glob)
verified = set()        # lowercase nicks confirmed via 307/330
whois_pending = set()   # lowercase nicks we WHOIS'd, awaiting 307/318

# IRC line limit is 512 incl CRLF. Body safe ~400.
MAX_BODY = 400

sock = None
send_lock = threading.Lock()
leak_guard = LeakGuard(HOST, RESERVED_FILE, RESERVED_STORE)

# Per-channel random cooldown after the last HUMAN PRIVMSG; when elapsed, emit
# `IDLE <chan>` once, disarm, and re-arm on the next human message. Our own
# outgoing messages do NOT reset the timer.
IDLE_RANGES = {
    "#sniffo": (5 * 60, 20 * 60),
    "#olografix": (10 * 60, 40 * 60),
    "#it-opers": (30 * 60, 90 * 60),
}
idle_state = {c.lower(): None for c in IDLE_RANGES}   # chan -> fire_at or None
idle_lock = threading.Lock()

# Kick-flood backoff: > KICK_MAX kicks in KICK_WINDOW => give up rejoin.
kick_history = {}
kick_lock = threading.Lock()
KICK_WINDOW = 60.0
KICK_MAX = 3


def _arm_idle(chan):
    rng = IDLE_RANGES.get(chan.lower())
    if rng:
        with idle_lock:
            idle_state[chan.lower()] = time.monotonic() + random.uniform(*rng)


def idle_ticker_loop():
    while True:
        time.sleep(10)
        now_mono = time.monotonic()
        fired = []
        with idle_lock:
            for chan, fire_at in list(idle_state.items()):
                if fire_at is not None and now_mono >= fire_at:
                    idle_state[chan] = None
                    fired.append(chan)
        for chan in fired:
            emit("IDLE", chan)
            log("*", f"IDLE {chan}")


def emit(kind, *parts):
    # [HH:MM] anchor (no seconds, by request) — start-monitor.sh's grep and
    # policy._EVENT both tolerate it. The EVENTS_FILE append is best-effort: a
    # sidecar container tails it instead of our stdout.
    line = f"[{now('%H:%M')}] " + kind + " " + " ".join(str(p) for p in parts)
    print(line, flush=True)
    try:
        with open(EVENTS_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_trust():
    global trust_rules
    out = []
    try:
        with open(TRUST_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    out.append((parts[0].lower(), parts[1]))
    except FileNotFoundError:
        pass
    trust_rules = out
    emit("TRUST_LOADED", len(trust_rules))


def is_trust_listed(nick):
    n = nick.lower()
    return any(tn == n for tn, _ in trust_rules)


def host_matches(nick, host):
    n = nick.lower()
    for tn, hg in trust_rules:
        if tn == n and fnmatch.fnmatch(host, hg):
            return True
    return False


def trust_check(nick, host):
    """(trusted, reason). Fires WHOIS on first sighting of a listed nick."""
    n = nick.lower()
    if not is_trust_listed(n):
        return (False, "not-listed")
    if not host_matches(n, host):
        return (False, "host-mismatch")
    if n not in verified:
        if n not in whois_pending:
            whois_pending.add(n)
            send_raw(f"WHOIS {nick}")
            emit("WHOIS_FIRED", nick)
        return (False, "pending-whois")
    return (True, "ok")


# Hard DM gate: by the time a model could apply a written policy, the payload
# is already in its context — drop at the bot. Unlisted nicks never become
# events; listed-but-unverified nicks are HELD until the WHOIS resolves
# (releasing the first DM of a genuinely trusted nick after a reconnect).
SERVICE_NICKS = {
    "nickserv", "chanserv", "memoserv", "operserv", "hostserv",
    "botserv", "helpserv", "statserv", "global", "services",
}
DM_REJECT_MSG = env(
    "BOT_DM_REJECT",
    "Con me si parla in canale, non in privato. / I only talk in channels, not in private.",
)
DM_REJECT_COOLDOWN = 6 * 3600.0
dm_rejected = {}
dm_lock = threading.Lock()
DM_HOLD_MAX = 5  # per nick, newest wins — a flood can't grow the queue
dm_held = {}


def dm_blocked(nick):
    n = nick.lower()
    if not n or n == NICK.lower() or n in SERVICE_NICKS:
        return False
    return not is_trust_listed(n)


def dm_reject_once(nick):
    """The 'talk in channel' NOTICE at most once per cooldown per nick; an
    empty BOT_DM_REJECT disables the rebound (the DM is still dropped)."""
    if not DM_REJECT_MSG:
        return False
    n = nick.lower()
    nowm = time.monotonic()
    with dm_lock:
        last = dm_rejected.get(n)
        if last is not None and nowm - last < DM_REJECT_COOLDOWN:
            return False
        dm_rejected[n] = nowm
        if len(dm_rejected) > 500:
            for k, ts in sorted(dm_rejected.items(), key=lambda kv: kv[1])[:100]:
                dm_rejected.pop(k, None)
    send_raw(f"NOTICE {nick} :{DM_REJECT_MSG}")
    return True


def dm_hold(nick, host, body):
    with dm_lock:
        q = dm_held.setdefault(nick.lower(), [])
        q.append((host, body))
        del q[:-DM_HOLD_MAX]
        return len(q)


def dm_flush(nick, reason):
    """Release held DMs, re-checking each (host glob + verified) at release."""
    n = nick.lower()
    with dm_lock:
        queued = dm_held.pop(n, [])
    for host, body in queued:
        if n in verified and host_matches(n, host):
            emit("MSG", "TRUSTED", f"FROM={nick}", f"HOST={host}",
                 f"TO={NICK}", "HELD=1", f"BODY={body}")
        else:
            emit("DM_DROPPED", f"FROM={nick}", f"HOST={host}",
                 f"LEN={len(body)}", reason)


def trust_reset(nick, reason):
    n = nick.lower()
    dm_flush(nick, f"reset:{reason}")
    if n in verified or n in whois_pending:
        verified.discard(n)
        whois_pending.discard(n)
        emit("TRUST_RESET", nick, reason)


def _mask_secrets(line):
    if NICKSERV_PASS and NICKSERV_PASS in line:
        return line.replace(NICKSERV_PASS, "********")
    return line


def log(direction, line):
    try:
        with open(LOG, "a") as f:
            f.write(f"{now('%H:%M:%S')} {direction} {_mask_secrets(line)}\n")
    except Exception:
        pass


def send_raw(s):
    log(">", s)
    with send_lock:
        try:
            sock.sendall((s + "\r\n").encode("utf-8", errors="replace"))
        except Exception as e:
            emit("ERROR", "send-fail", repr(e))


def split_say(target, text):
    """PRIVMSG(s) preserving whitespace verbatim; wrap at the last space
    before MAX_BODY bytes, hard-splitting an oversized word on a utf-8 boundary."""
    b = text.encode("utf-8")
    if len(b) <= MAX_BODY:
        send_raw(f"PRIVMSG {target} :{text}")
        return
    while b:
        if len(b) <= MAX_BODY:
            send_raw(f"PRIVMSG {target} :{b.decode('utf-8', errors='ignore')}")
            return
        cut = b.rfind(b" ", 1, MAX_BODY + 1)
        if cut <= 0:
            cut = MAX_BODY
            while cut > 1 and (b[cut] & 0xC0) == 0x80:
                cut -= 1
        else:
            cut += 1  # keep the breaking space on this chunk
        send_raw(f"PRIVMSG {target} :{b[:cut].decode('utf-8', errors='ignore')}")
        b = b[cut:]


def run_startup():
    try:
        with open(STARTUP_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        emit("STARTUP_DONE", "no-file")
        return
    count = 0
    for raw in lines:
        line = raw.rstrip("\r\n").strip()
        if not line or line.startswith("#"):
            continue
        try:
            process_cmd(line)
            count += 1
        except Exception as e:
            emit("ERROR", "startup-fail", repr(e), line[:80])
        time.sleep(0.5)
    emit("STARTUP_DONE", count)


def fire_startup(reason):
    global startup_fired
    if startup_fired:
        return
    startup_fired = True
    emit("STARTUP_FIRED", reason)
    threading.Thread(target=run_startup, daemon=True).start()


def _reclaim_after_ghost():
    # GHOST is async; give services a beat, then reclaim. The echo of our own
    # NICK change fires IDENTIFY — wording-agnostic across services.
    time.sleep(4)
    if ghost_needed:
        send_raw(f"NICK {NICK}")
        emit("NS_GHOST_RECLAIM", NICK)


def handle_server_line(line):
    global registered, ghost_needed, tmp_nick
    log("<", line)
    if line.startswith("PING "):
        send_raw("PONG " + line[5:])
        return

    m = re.match(r"^(?::(\S+)\s+)?(\S+)(?:\s+(.*))?$", line)
    if not m:
        return
    prefix, cmd, rest = m.groups()
    nick = prefix.split("!")[0] if prefix else ""
    # Host in every event: a flooder spoofing a trusted nick is only visible
    # against the trust host glob, in the stream itself.
    host = prefix.split("@", 1)[1] if prefix and "@" in prefix else ""
    rest = rest or ""
    if cmd in ("PRIVMSG", "NOTICE", "TOPIC") and " :" in rest:
        tgt, _, body = rest.partition(" :")
        leak_guard.record(tgt.strip(), body)

    if cmd == "001":
        registered = True
        emit("CONNECTED", NICK)
        for tn, _ in trust_rules:
            whois_pending.add(tn)
            send_raw(f"WHOIS {tn}")
            emit("WHOIS_FIRED", tn)
        if NICKSERV_PASS:
            if ghost_needed:
                send_raw(f"PRIVMSG NickServ :GHOST {NICK} {NICKSERV_PASS}")
                emit("NS_GHOST_SENT", NICK)
                threading.Thread(target=_reclaim_after_ghost, daemon=True).start()
            else:
                send_raw(f"PRIVMSG NickServ :IDENTIFY {NICKSERV_PASS}")
                emit("NS_IDENTIFY_SENT")
        else:
            fire_startup("no-nickserv-pass")
        return
    if cmd in ("433", "432", "437"):
        emit("NICK_ERROR", cmd, rest)
        if not registered:
            tmp_nick = f"{NICK}-{random.randint(100, 999)}"
            send_raw(f"NICK {tmp_nick}")
            ghost_needed = True
            emit("NICK_FALLBACK", tmp_nick)
        return
    if cmd in ("464", "465"):
        emit("AUTH_ERROR", cmd, rest)
        return
    if cmd == "401":
        # ERR_NOSUCHNICK: surface silent PRIVMSG/NOTICE failures as events.
        parts = rest.split(" :", 1)
        head = parts[0].split()
        tgt = head[1] if len(head) > 1 else ""
        reason = parts[1] if len(parts) > 1 else "No such nick/channel"
        emit("IRC_ERROR", "401", tgt, reason)
        return
    if cmd == "307":    # RPL_WHOISREGNICK (Azzurra/bahamut)
        parts = rest.split()
        if len(parts) >= 2:
            target = parts[1].lower()
            verified.add(target)
            whois_pending.discard(target)
            emit("VERIFIED", parts[1])
            dm_flush(parts[1], "307")
        return
    if cmd == "330":
        # RPL_WHOISACCOUNT (Libera/solanum — Libera has no 307).
        parts = rest.split()
        if len(parts) >= 2:
            target = parts[1].lower()
            verified.add(target)
            whois_pending.discard(target)
            emit("VERIFIED", parts[1])
            dm_flush(parts[1], "330")
        return
    if cmd == "318":
        # RPL_ENDOFWHOIS: pending and not verified => not registered.
        parts = rest.split()
        if len(parts) >= 2:
            target = parts[1].lower()
            if target in whois_pending:
                whois_pending.discard(target)
                if target not in verified:
                    emit("NOT_REGISTERED", parts[1])
            dm_flush(parts[1], "318")   # nothing stays parked past ENDOFWHOIS
        return
    if cmd == "NOTICE":
        parts = rest.split(" :", 1)
        tgt = parts[0]
        msg = parts[1] if len(parts) > 1 else ""
        if nick.lower() == "nickserv" and NICKSERV_PASS:
            low = msg.lower()
            if any(k in low for k in ("identified", "accepted", "recognized", "riconosciuto", "autenticato", "accettata", "identificato")):
                fire_startup("nickserv-ok")
            elif any(k in low for k in ("incorrect", "invalid", "failed", "errata", "errato")):
                emit("NS_IDENTIFY_FAIL", msg)
        if nick and nick.lower() != NICK.lower():
            emit("NOTICE", f"FROM={nick}", f"HOST={host}", f"TO={tgt}", f"BODY={msg}")
        return
    if cmd == "INVITE":
        parts = rest.split(" :", 1)
        target_chan = parts[1] if len(parts) == 2 else rest.split()[-1]
        trusted, reason = trust_check(nick, host)
        emit("INVITE", nick, f"HOST={host}", target_chan, "trusted" if trusted else f"untrusted:{reason}")
        if trusted:
            send_raw(f"JOIN {target_chan}")
        return
    if cmd == "KICK":
        kick_parts = rest.split(" :", 1)
        head = kick_parts[0].split()
        kick_chan = head[0] if head else ""
        kick_target = head[1] if len(head) > 1 else ""
        kick_reason = kick_parts[1] if len(kick_parts) > 1 else ""
        emit("KICK", nick, f"HOST={host}", kick_chan, kick_target, kick_reason)
        if kick_chan and kick_target.lower() == NICK.lower():
            # Repeated kicks in a short window = a hostile op: walk away
            # rather than rejoin into Excess Flood.
            nowm = time.monotonic()
            key = kick_chan.lower()
            with kick_lock:
                history = [t for t in kick_history.get(key, []) if nowm - t < KICK_WINDOW]
                history.append(nowm)
                kick_history[key] = history
                count = len(history)
            if count > KICK_MAX:
                emit("KICK_GIVEUP", kick_chan, nick, f"{count} kicks in {KICK_WINDOW}s")
            else:
                def _rejoin(ch=kick_chan):
                    send_raw(f"PRIVMSG ChanServ :INVITE {ch}")
                    threading.Timer(0.5, lambda: send_raw(f"JOIN {ch}")).start()
                threading.Timer(2.0, _rejoin).start()
                emit("KICK_REJOIN", kick_chan, nick, f"{count}/{KICK_MAX}")
        return
    if cmd == "PRIVMSG":
        parts = rest.split(" :", 1)
        target = parts[0].strip()
        body = parts[1] if len(parts) > 1 else ""
        if body.startswith("\x01") and body.endswith("\x01"):
            ctcp = body.strip("\x01")
            emit("CTCP", f"FROM={nick}", f"HOST={host}", f"TO={target}", f"BODY={ctcp}")
            if ctcp.upper() == "VERSION":
                send_raw(f"NOTICE {nick} :\x01VERSION claude-code PoC\x01")
            elif ctcp.upper().startswith("PING"):
                send_raw(f"NOTICE {nick} :\x01{ctcp}\x01")
            return
        if not target.startswith(("#", "&")) and dm_blocked(nick):
            replied = dm_reject_once(nick)
            emit("DM_BLOCKED", f"FROM={nick}", f"HOST={host}",
                 f"LEN={len(body)}", "replied" if replied else "silent")
            return
        trusted, reason = trust_check(nick, host)
        trust = "TRUSTED" if trusted else "UNTRUSTED"
        if not target.startswith(("#", "&")) and not trusted:
            # Listed but pending WHOIS: park it. Anything else (host-mismatch
            # squatter) will never become trusted: drop and rebound.
            if reason == "pending-whois":
                depth = dm_hold(nick, host, body)
                emit("DM_HELD", f"FROM={nick}", f"HOST={host}",
                     f"LEN={len(body)}", f"QUEUED={depth}")
            else:
                replied = dm_reject_once(nick)
                emit("DM_BLOCKED", f"FROM={nick}", f"HOST={host}",
                     f"LEN={len(body)}", reason,
                     "replied" if replied else "silent")
            return
        emit("MSG", trust, f"FROM={nick}", f"HOST={host}", f"TO={target}", f"BODY={body}")
        if is_trust_listed(nick) and not trusted:
            emit("TRUST_DENIED", nick, host, reason)
        if target.startswith("#") and nick.lower() != NICK.lower():
            _arm_idle(target)
        return
    if cmd == "JOIN":
        emit("JOIN", nick, f"HOST={host}", rest.lstrip(":"))
        return
    if cmd == "PART":
        emit("PART", nick, f"HOST={host}", rest)
        trust_reset(nick, "part")
        return
    if cmd == "QUIT":
        emit("QUIT", nick, f"HOST={host}", rest)
        trust_reset(nick, "quit")
        return
    if cmd == "NICK":
        new_nick = rest.lstrip(":")
        emit("NICK_CHANGE", nick, f"HOST={host}", new_nick)
        trust_reset(nick, "nick-change")
        if ghost_needed and new_nick.lower() == NICK.lower():
            ghost_needed = False
            if NICKSERV_PASS:
                send_raw(f"PRIVMSG NickServ :IDENTIFY {NICKSERV_PASS}")
                emit("NS_IDENTIFY_SENT")
        return
    if cmd == "ERROR":
        emit("SERVER_ERROR", rest)
        return


# EAGAIN on a TLS socket means "record not complete yet", not "link dead" —
# retry, but a capped retry: a process that hears nothing forever is worse
# than a dead one. A real recv resets the count.
RECV_RETRY_SLEEP = 0.05
RECV_RETRY_MAX = 200            # ~10s of solid EAGAIN, then die for real


def reader_loop():
    buf = b""
    retries = 0
    while True:
        try:
            data = sock.recv(8192)
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError) as e:
            # One BlockingIOError(11) once killed the process and cost a full
            # reconnect; genuine timeouts share neither handler.
            retries += 1
            if retries > RECV_RETRY_MAX:
                emit("ERROR", "recv-fail", f"{retries} consecutive {e!r}")
                return
            time.sleep(RECV_RETRY_SLEEP)
            continue
        except Exception as e:
            emit("ERROR", "recv-fail", repr(e))
            return
        retries = 0
        if not data:
            emit("DISCONNECTED")
            return
        buf += data
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
            if line:
                try:
                    handle_server_line(line)
                except Exception as e:
                    emit("ERROR", "handler-fail", repr(e), line[:80])


def writer_loop():
    if os.path.exists(FIFO):
        try:
            os.remove(FIFO)
        except Exception:
            pass
    os.mkfifo(FIFO, 0o600)
    emit("FIFO_READY", FIFO)
    while True:
        with open(FIFO, "r") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                try:
                    process_cmd(line)
                except Exception as e:
                    emit("CMD_ERROR", "cmd-fail", repr(e), line[:80])


def outbound_ok(verb, rest):
    """Leak gate for every chat line; RAW is parsed too, or `RAW PRIVMSG #x
    :...` would be the way around it."""
    if verb == "RAW":
        m = re.match(r"(?i)(PRIVMSG|NOTICE|TOPIC)\s+(\S+)\s+:(.*)", rest)
        if not m:
            return True
        target, text = m.group(2), m.group(3)
    elif verb in ("SAY", "ACT", "NOTICE") and " " in rest:
        target, text = rest.split(" ", 1)
    else:
        return True
    # Services commands name the reserved channel by design.
    if target.lower() in ("chanserv", "nickserv"):
        return True
    reason = leak_guard.check(target, text, dm_ok=is_trust_listed(target))
    if reason:
        log("!", f"LEAK_BLOCKED {verb} {target} {reason}")
        emit("LEAK_BLOCKED", verb, f"TO={target}", reason, f"LEN={len(text)}")
        return False
    leak_guard.record(target, text)
    return True


def process_cmd(line):
    verb, _, rest = line.partition(" ")
    verb = verb.upper()
    if not outbound_ok(verb, rest):
        return
    if verb == "SAY":
        if " " not in rest:
            emit("CMD_ERROR", "SAY needs <target> <text>", repr(rest))
            return
        target, text = rest.split(" ", 1)
        split_say(target, text)
    elif verb == "ACT":
        if " " not in rest:
            emit("CMD_ERROR", "ACT needs <target> <text>", repr(rest))
            return
        target, text = rest.split(" ", 1)
        send_raw(f"PRIVMSG {target} :\x01ACTION {text}\x01")
    elif verb == "NOTICE":
        if " " not in rest:
            emit("CMD_ERROR", "NOTICE needs <target> <text>", repr(rest))
            return
        target, text = rest.split(" ", 1)
        send_raw(f"NOTICE {target} :{text}")
    elif verb == "JOIN":
        send_raw(f"JOIN {rest}")
    elif verb == "PART":
        send_raw(f"PART {rest}")
    elif verb == "WHOIS":
        send_raw(f"WHOIS {rest}")
    elif verb == "QUIT":
        send_raw(f"QUIT :{rest or 'bye'}")
    elif verb == "RAW":
        send_raw(rest)
    else:
        emit("CMD_ERROR", "unknown-verb", repr(verb),
             "valid: SAY ACT NOTICE JOIN PART WHOIS QUIT RAW")


def main():
    global sock
    load_trust()
    ctx = ssl.create_default_context()
    if not TLS_VERIFY:
        # check_hostname must go first: CERT_NONE with hostname checking raises.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(
        (HOST, PORT), timeout=30, source_address=((BIND, 0) if BIND else None)
    )
    # Kernel keepalive + recv timeout: a silently dead TCP link surfaces as a
    # timeout -> reader_loop returns -> the platform restart policy acts.
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
    if TLS:
        sock = ctx.wrap_socket(raw, server_hostname=HOST)
        sock.settimeout(420)
        emit("TLS_OK", HOST, PORT, "verify" if TLS_VERIFY else "NOVERIFY")
    else:
        sock = raw
        sock.settimeout(420)
        emit("TCP_OK", HOST, PORT, "plaintext")
    send_raw(f"NICK {NICK}")
    send_raw(f"USER {IDENT} 0 * :{REAL}")

    threading.Thread(target=writer_loop, daemon=True).start()
    threading.Thread(target=idle_ticker_loop, daemon=True).start()
    reader_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        emit("FATAL", repr(e))
        sys.exit(1)
