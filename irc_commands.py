#!/usr/bin/env python3
"""irc_commands.py — persistent IRC commands for oc-ircbot.

Loaded (and hot-reloaded on mtime change) by bot.py from
BOT_IRC_COMMANDS, default /data/irc_commands.py. Lives on the PVC, so it
survives bot-container restarts as long as bot.py has the small dispatch
hook.

Entry point: handle(target, nick, body, trusted, send_raw, emit, split_say)
  returns True when it handled the command.
"""
import hashlib
import ipaddress
import json
import os
import random
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid

X0_URL = "https://x0.at/"
UA = "ocbot/ab2filter"
AB2_HOST = "ab2f.archivingyoursh.it"
_rx_ab2_path = re.compile(r"^/[^/]+\.jsonl$")
_rx_url = re.compile(r"https?://[^\s\"'<>]+")
COOLDOWN = 15.0            # seconds between jobs, globally
MAX_MATCHES = 200000
MAX_OUT_BYTES = 80 * 1024 * 1024
MAX_LINES = 5_000_000

_lock = threading.Lock()
_busy = False
_last_run = 0.0

# --- !steve: feedback loop through the #steve Markov bot --------------------
STEVE_CHANNEL = "#steve"
STEVE_LOG = os.environ.get("BOT_STEVE_LOG", "/data/bot.espernet.log")
STEVE_ROUNDS = 12
STEVE_WAIT = 30.0          # seconds to wait for each Steve reply
STEVE_PREFIXES = ("Steve, ", "steve, ")   # try capital first, then lowercase
STEVE_MAX_CHARS = 300      # cap on the seed / each carried sentence
_steve_lock = threading.Lock()
_steve_busy = False
_rx_privmsg = re.compile(r"^\d\d:\d\d:\d\d < :([^!]+)![^ ]+ PRIVMSG (#[^ ]+) :(.*)$")


def _get_field(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _upload_x0(path):
    boundary = "----ocbot" + uuid.uuid4().hex
    name = path.rsplit("/", 1)[-1]
    with open(path, "rb") as f:
        body = f.read()
    chunks = []
    chunks.append(("--" + boundary + "\r\n").encode())
    chunks.append(('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % name).encode())
    chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
    chunks.append(body)
    chunks.append(b"\r\n")
    chunks.append(("--" + boundary + "--\r\n").encode())
    data = b"".join(chunks)
    req = urllib.request.Request(X0_URL, data=data, headers={
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode("utf-8", "replace").strip()


def _extract_links(text, rx):
    """URLs in text that also match rx; falls back to the rx match if it is
    itself a URL (so a URL-shaped pattern works without a candidate list)."""
    found = []
    for m in _rx_url.finditer(text):
        u = m.group(0)
        if rx.search(u) and u not in found:
            found.append(u)
    if not found:
        for m in rx.finditer(text):
            u = m.group(1) if m.groups() and m.group(1) else m.group(0)
            if u.startswith(("http://", "https://")) and u not in found:
                found.append(u)
    return found


def _stream_filter(url, field, pattern, any_mode, out_path, mode):
    rx = re.compile(pattern)
    total = matched = 0
    written = 0
    truncated = False
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(out_path, "w") as out:
        for raw in r:
            total += 1
            if total > MAX_LINES:
                truncated = True
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if any_mode:
                text = line
                hit = bool(rx.search(text))
            else:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                val = _get_field(obj, field)
                if val is None:
                    continue
                text = str(val)
                hit = bool(rx.search(text))
            if not hit:
                continue
            if mode == "links":
                hits = _extract_links(text, rx)
            elif mode == "values":
                hits = [text]
            else:
                hits = [line]
            if not hits:
                continue
            matched += 1
            for h in hits:
                out.write(h + "\n")
                written += len(h) + 1
            if matched >= MAX_MATCHES or written >= MAX_OUT_BYTES:
                truncated = True
                break
    return total, matched, written, truncated


def _ab2_url_ok(url):
    """Only the archivingyoursh AB2 export host, only *.jsonl paths."""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if p.scheme != "https" or (p.hostname or "").lower() != AB2_HOST:
        return False
    if p.username or p.password or p.port not in (None, 443):
        return False
    return bool(_rx_ab2_path.match(p.path)) and not p.query and not p.fragment


def _parse_args(rest):
    """!ab2filter <url> <field> <pattern>
    [--field F] [--pattern P] [--any] [--links] [--values]"""
    toks = rest.split()
    url = None
    field = None
    pattern = None
    any_mode = False
    links = False
    values = False
    pos = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("--field", "--pattern") and i + 1 < len(toks):
            if t == "--field":
                field = toks[i + 1]
            else:
                pattern = toks[i + 1]
            i += 2
            continue
        if t == "--any":
            any_mode = True
            i += 1
            continue
        if t == "--links":
            links = True
            i += 1
            continue
        if t == "--values":
            values = True
            i += 1
            continue
        pos.append(t)
        i += 1
    if pos:
        url = pos[0]
    if len(pos) > 1:
        field = pos[1]
    if len(pos) > 2:
        pattern = pos[2]
    return url, field, pattern, any_mode, links, values


def _run_job(target, url, field, pattern, any_mode, mode, send_raw, emit):
    global _busy, _last_run
    out_path = "/tmp/ab2filter-%s.jsonl" % uuid.uuid4().hex[:8]
    try:
        total, matched, written, truncated = _stream_filter(
            url, field, pattern, any_mode, out_path, mode)
        if matched == 0:
            send_raw("PRIVMSG %s :ab2filter: 0 matches for %s=%r in %d lines — nothing to upload."
                     % (target, field, pattern, total))
            return
        link = _upload_x0(out_path)
        note = " (truncated)" if truncated else ""
        send_raw("PRIVMSG %s :ab2filter%s [%s]: %d matches / %d lines, %s=%r → %s"
                 % (target, note, mode, matched, total, field, pattern, link))
        emit("AB2FILTER", target, "mode=%s" % mode, "matches=%d" % matched,
             "lines=%d" % total, "bytes=%d" % written)
    except Exception as e:
        emit("AB2FILTER_ERROR", target, repr(e))
        try:
            send_raw("PRIVMSG %s :ab2filter failed: %s" % (target, str(e)[:180]))
        except Exception:
            pass
    finally:
        try:
            import os
            os.unlink(out_path)
        except OSError:
            pass
        with _lock:
            _busy = False


class _LogTail:
    """Read appended lines from the bot's own IRC log, without following
    rotation forever: start at EOF, and reset to zero if the file shrank."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "r", encoding="utf-8", errors="replace")
        self.f.seek(0, os.SEEK_END)
        self.pos = self.f.tell()

    def new_lines(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.pos:
            self.f.seek(0)
            self.pos = 0
        self.f.seek(self.pos)
        while True:
            line = self.f.readline()
            if not line:
                return
            self.pos = self.f.tell()
            yield line.rstrip("\n")

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


def _steve_wait(tail, timeout):
    """Return the next message Steve posts in #steve, or None on timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for line in tail.new_lines():
            m = _rx_privmsg.match(line)
            if not m:
                continue
            who, chan, said = m.group(1), m.group(2), m.group(3)
            if chan.lower() != STEVE_CHANNEL or who.lower() != "steve":
                continue
            said = said.strip()
            if not said or said.startswith("\x01"):
                continue
            return said
        time.sleep(0.4)
    return None


def _steve_run(target, sentence, send_raw, emit):
    global _steve_busy
    tail = None
    try:
        try:
            tail = _LogTail(STEVE_LOG)
        except OSError as e:
            send_raw("PRIVMSG %s :steve: can't read %s (%s)"
                     % (target, STEVE_LOG, str(e)[:80]))
            return
        cur = sentence
        prefix = None
        for i in range(1, STEVE_ROUNDS + 1):
            reply = None
            for p in ((prefix,) if prefix else STEVE_PREFIXES):
                send_raw("PRIVMSG %s :%s%s" % (STEVE_CHANNEL, p, cur))
                reply = _steve_wait(tail, STEVE_WAIT)
                if reply is not None:
                    prefix = p
                    break
            if reply is None:
                send_raw("PRIVMSG %s :steve: no reply after %ds — stopped at round %d/%d."
                         % (target, int(STEVE_WAIT), i, STEVE_ROUNDS))
                emit("STEVE_TIMEOUT", target, "round=%d" % i)
                return
            cur = reply[:STEVE_MAX_CHARS]
        send_raw("PRIVMSG %s :steve: %d rounds done — last: %s"
                 % (target, STEVE_ROUNDS, cur[:300]))
        emit("STEVE_DONE", target, "rounds=%d" % STEVE_ROUNDS)
    except Exception as e:
        emit("STEVE_ERROR", target, repr(e))
        try:
            send_raw("PRIVMSG %s :steve failed: %s" % (target, str(e)[:180]))
        except Exception:
            pass
    finally:
        if tail is not None:
            tail.close()
        with _steve_lock:
            _steve_busy = False


def _handle_steve(target, nick, text, trusted, send_raw, emit):
    global _steve_busy
    rest = text[len("!steve"):].strip()
    if target.lower() != STEVE_CHANNEL:
        send_raw("PRIVMSG %s :!steve only runs in %s." % (target, STEVE_CHANNEL))
        return True
    if rest in ("", "help", "-h", "--help"):
        send_raw("PRIVMSG %s :usage: !steve <sentence> — feeds it to Steve, then "
                 "feeds each reply back, %d rounds. one loop at a time."
                 % (target, STEVE_ROUNDS))
        return True
    with _steve_lock:
        if _steve_busy:
            send_raw("PRIVMSG %s :!steve: already running, %s — one loop at a time."
                     % (target, nick))
            return True
        _steve_busy = True
    t = threading.Thread(
        target=_steve_run,
        args=(target, rest[:STEVE_MAX_CHARS], send_raw, emit),
        daemon=True)
    t.start()
    return True


# --- !tom: fire the classic trio at tom, for everyone (katia, 2026-10-31) ---
# !slap is TOOLS, !pernacchio and !sessua are Ambrogio: all channel bots. So
# "!tom" simply has ocbot send their three triggers to the channel, and they
# answer. No trust gate (katia asked for it to be usable by everyone), just a
# per-channel cooldown so it cannot be used as a flood amplifier.
TOM_COMMANDS = ("!slap tom", "!tombola", "!giocatori tom oracle", "!wakeup")
TOM_COOLDOWN = 28800.0
_tom_lock = threading.Lock()
_tom_last = {}             # channel -> monotonic timestamp


def _handle_tom(target, send_raw):
    nowm = time.monotonic()
    with _tom_lock:
        if nowm - _tom_last.get(target, 0.0) < TOM_COOLDOWN:
            return True
        _tom_last[target] = nowm

    def fire():
        for i, cmd in enumerate(TOM_COMMANDS):
            if i:
                time.sleep(0.6)
            send_raw("PRIVMSG %s :%s" % (target, cmd))

    threading.Thread(target=fire, daemon=True).start()
    return True


# --- !wakeup: mass-ping the channel (katia, 2026-11-03) ---------------------
# Roster comes from the bot's own events log (BOT_EVENTS), replayed in file
# order: JOIN adds, PART removes, QUIT removes everywhere, NICK_CHANGE renames.
# Trusted-only and rate-limited per channel; the nick list is split over
# several PRIVMSGs so no single line can hit the IRC 512-byte limit.
WAKEUP_COOLDOWN = 60.0
WAKEUP_CHUNK = 380                 # max chars of nicks per PRIVMSG
_wakeup_lock = threading.Lock()
_wakeup_last = {}                  # channel -> monotonic timestamp
_rx_ev_join = re.compile(r"^\[\d\d:\d\d\] JOIN (\S+) HOST=\S+ (#\S+)\s*$")
_rx_ev_part = re.compile(r"^\[\d\d:\d\d\] PART (\S+) HOST=\S+ (#\S+)\s*$")
_rx_ev_quit = re.compile(r"^\[\d\d:\d\d\] QUIT (\S+) HOST=")
_rx_ev_nick = re.compile(r"^\[\d\d:\d\d\] NICK_CHANGE (\S+) HOST=\S+ (\S+)\s*$")


def _events_path():
    return os.environ.get("BOT_EVENTS") or "/data/events.log"


def _channel_roster(target):
    """Current nick list for one channel, reconstructed from the events log."""
    chans = {}
    try:
        with open(_events_path(), errors="replace") as f:
            for line in f:
                m = _rx_ev_join.match(line)
                if m:
                    chans.setdefault(m.group(2).lower(), {})[m.group(1).lower()] = m.group(1)
                    continue
                m = _rx_ev_part.match(line)
                if m:
                    chans.get(m.group(2).lower(), {}).pop(m.group(1).lower(), None)
                    continue
                m = _rx_ev_quit.match(line)
                if m:
                    n = m.group(1).lower()
                    for members in chans.values():
                        members.pop(n, None)
                    continue
                m = _rx_ev_nick.match(line)
                if m:
                    old, new = m.group(1).lower(), m.group(2)
                    for members in chans.values():
                        if old in members:
                            members.pop(old)
                            members[new.lower()] = new
    except OSError:
        return []
    members = chans.get(target.lower(), {})
    self_nick = (os.environ.get("IRC_NICK") or "ocbot").lower()
    return [n for k, n in members.items() if k != self_nick]


def _handle_wakeup(target, nick, rest, trusted, send_raw):
    if not trusted:
        return True
    nowm = time.monotonic()
    with _wakeup_lock:
        wait = WAKEUP_COOLDOWN - (nowm - _wakeup_last.get(target, 0.0))
        if wait > 0:
            send_raw("PRIVMSG %s :!wakeup: calma, %ds." % (target, int(wait) + 1))
            return True
        _wakeup_last[target] = nowm

    nicks = _channel_roster(target)
    if not nicks:
        send_raw("PRIVMSG %s :!wakeup: nessuno da svegliare qui." % target)
        return True

    tail = (" " + rest) if rest else ""
    chunks, cur = [], ""
    for n in nicks:
        if cur and len(cur) + 1 + len(n) > WAKEUP_CHUNK:
            chunks.append(cur)
            cur = n
        else:
            cur = (cur + " " + n) if cur else n
    if cur:
        chunks.append(cur)

    def fire():
        for i, chunk in enumerate(chunks):
            if i:
                time.sleep(0.4)
            send_raw("PRIVMSG %s :%s%s" % (target, chunk, tail))

    threading.Thread(target=fire, daemon=True).start()
    return True


# --- !secondome: ogni "secondo me..." di #cybernet (katia, 2026-10-31) ------
# One random line per invocation, collected from the channel.
SECONDOME_LINES = [
    "secondo me tom vuole essere inchiappettato con lo strap on",
    "vertex mi si vuol fare secondo me",
    "secondo me per diventà ricchi bisogna esse rincoglioniti",
]
def _handle_secondome(target, send_raw):
    send_raw("PRIVMSG %s :%s" % (target, random.choice(SECONDOME_LINES)))
    return True


# --- !millelire: lire/euro nostalgia (katia, 2026-10-31, solo #cybernet) ----
LIRE_PER_EURO = 1936.27
LIRE_WORDS = {
    "mille": 1000, "duemila": 2000, "tremila": 3000, "quattromila": 4000,
    "cinquemila": 5000, "seimila": 6000, "settemila": 7000,
    "ottomila": 8000, "novemila": 9000, "diecimila": 10000,
    "ventimila": 20000, "cinquantamila": 50000, "centomila": 100000,
    "unmilione": 1000000,
}

def _lire_to_euro(target, v, send_raw):
    send_raw("PRIVMSG %s :%s lire = %s €." % (
        target, format(int(round(v)), ",d").replace(",", "."),
        ("%.2f" % (v / LIRE_PER_EURO)).replace(".", ",")))
    return True

def _handle_millelire(target, rest, send_raw):
    r = rest.strip().replace(",", ".")
    if not r:
        send_raw("PRIVMSG %s :mille lire = 0,52 € — e con 1 € ci facevi 1936 lire e un caffè."
                 % target)
        return True
    euros = r.startswith(("€", "eur", "euro"))
    r = r.lstrip("€").strip()
    if r.lower().startswith(("eur", "euro")):
        r = r[3:].strip()
    r = re.sub(r"(?<=\d)\.(?=\d{3}(?:\D|$))", "", r)
    try:
        v = float(r)
    except ValueError:
        send_raw("PRIVMSG %s :dammi un numero, tipo !millelire 5000 (lire) o !millelire €5."
                 % target)
        return True
    if euros:
        send_raw("PRIVMSG %s :%s € = %s lire." % (
            target, ("%.2f" % v).replace(".", ","),
            format(int(round(v * LIRE_PER_EURO)), ",d").replace(",", ".")))
    else:
        return _lire_to_euro(target, v, send_raw)
    return True


# --- !aperisesso: aperitivo con sesso prima di cena (tom/katia, 2026-10-31) --
# Ten escalating tiers of five lines. One random line per invocation (never
# the whole pile), so it is "one at a time" and never the same twice.
APERISESSO_COOLDOWN = 2.0
APERISESSO_TIERS = [
    [
        "%s stappa l'aperisesso: due spritz e un flirt che sa di guai.",
        "%s brinda e l'occhiata vale già il conto.",
        "%s ordina per due, nessuno chiede chi sia il secondo.",
        "%s sceglie il tavolo più buio e sorride.",
        "%s attacca bottone col cameriere, per sport.",
    ],
    [
        "%s «fa caldo» e si sbottona; è gennaio.",
        "%s appoggia il ginocchio al vicino e lo chiama «pressing etilico».",
        "%s divide il tagliere con la sinistra, le intenzioni con la destra.",
        "%s si lecca le dita delle olive guardando fisso qualcuno.",
        "%s sposta la sedia un po' troppo vicino.",
    ],
    [
        "%s sussurra che l'aperitivo è solo un pretesto.",
        "%s rovescia lo spritz apposta per chinarsi vicino a qualcuno.",
        "%s chiede se il tavolo è libero stasera, non solo per cena.",
        "%s fa cadere il tovagliolo e ci mette un minuto a raccoglierlo.",
        "%s propone di dividere non solo il tagliere.",
    ],
    [
        "%s: «prima di cena si fa l'antipasto», e giù mani con delicatezza da muletto.",
        "%s sposta il tavolo col piede per fare posto a un menu fuori cartello.",
        "%s salta l'aperitivo e passa al brutto vizio, con classe.",
        "%s ordina un giro di limoncello e due intenzioni.",
        "%s accavalla le gambe e il tavolo se ne accorge.",
    ],
    [
        "%s: «il sesso prima di cena è come il pane, fammi il bis», e il tavolo trema.",
        "%s fa l'aperisesso: spritz sul collo, olive sparse, dignità al guardaroba.",
        "%s brinda ai «tavoli a conduzione familiare» mentre il ginocchio viaggia.",
        "%s chiede il conto e poi chiede altro.",
        "%s sparge sale sul tavolo e dice che serve per dopo.",
    ],
    [
        "%s infila la mano sotto il tavolo e trova più di un appetito.",
        "%s: «il cameriere porta il vassoio, io porto le mani», e nessuno protesta.",
        "%s propone un brindisi a lingua piena.",
        "%s slaccia una cintura che non è la sua.",
        "%s fa cadere la forchetta e resta sotto il tavolo più del necessario.",
    ],
    [
        "%s infila la mano sotto il tavolo e scopre che l'appetito non si ferma al menu.",
        "%s chiede se il bagno è «a conduzione familiare» e si alza con un sorriso.",
        "%s slaccia la cintura del vicino «per comodità».",
        "%s: «il sesso prima di cena apre lo stomaco», e lo dimostra.",
        "%s sposta i piatti e sale sul tavolo: «cambio di portata».",
    ],
    [
        "%s fa un brindisi col corpo; il bicchiere resta in mano a qualcun altro.",
        "%s: «prima di cena si fa l'antipasto», e la tovaglia copre il resto.",
        "%s ordina ostriche e guarda il vicino come se fosse il limone.",
        "%s mette la mano dove il menu non arriva e nessuno chiama il cameriere.",
        "%s chiede il bis prima ancora del primo piatto.",
    ],
    [
        "%s: «l'aperisesso non si spiega, si serve», e il tavolo diventa un letto.",
        "%s rovescia tutto tranne le intenzioni, che restano dritte.",
        "%s fa il conto a mente e le posate a memoria, sotto il tavolo.",
        "%s: «un'ultima oliva», ma non era un'oliva.",
        "%s porta il dessert prima della cena e nessuno si lamenta.",
    ],
    [
        "%s chiude l'aperisesso con un brindisi, due gemiti e la dignità in ammollo.",
        "%s: «era solo un aperitivo», mentre il tavolo chiede un avvocato.",
        "%s lascia la mancia e un biglietto con un orario e nessun nome.",
        "%s saluta il cameriere con la lingua e paga col sorriso.",
        "%s esce dal locale e rientra dal retro, per non dare nell'occhio.",
    ],
]
_aper_lock = threading.Lock()
_aper_last = {}            # channel -> monotonic timestamp until it is free


def _handle_aperisesso(target, nick, rest, send_raw):
    who = (rest.split() or [nick])[0]
    flat = [line for tier in APERISESSO_TIERS for line in tier]
    nowm = time.monotonic()
    with _aper_lock:
        if nowm < _aper_last.get(target, 0.0):
            return True
        _aper_last[target] = nowm + APERISESSO_COOLDOWN
    send_raw("PRIVMSG %s :%s" % (target, random.choice(flat) % who))
    return True


# --- !violenta: nonostante il nome, e' tutto puccioso (katia, 2026-10-31) ----
# "tipo !love: tutto puccioso pieno di emoji carine, cuori e kaomoji".
# One random line per invocation, same cooldown shape as !aperisesso.
VIOLENTA_COOLDOWN = 2.0
VIOLENTA_LINES = [
    "%s riceve un abbraccio morbidoso (づ｡◕‿‿◕｡)づ ♥",
    "%s: «ti voglio bene» (´｡• ᵕ •｡`) ♡",
    "%s lancia mille cuoricini ♥♥♥ (｡♥‿♥｡)",
    "%s fa una carezza sulla guancia (っ˘ω˘ς ) ♡",
    "%s prepara cioccolata calda per tutti ☕♡ (๑˃ᴗ˂)ﻭ",
    "%s manda un bacio volante (๑♡⌓♡๑)",
    "%s si scioglie in un abbraccio puccioso (つ≧▽≦)つ ♥",
    "%s: «sei la mia personcina preferita» (´,,•ω•,,)♡",
    "%s tira fuori i pompom e fa il tifo per te ٩(◕‿◕)۶ ♡",
    "%s manda un cuore grande così (づ￣ ³￣)づ ♥‿♥",
]
_violenta_lock = threading.Lock()
_violenta_last = {}        # channel -> monotonic timestamp until it is free


def _handle_violenta(target, nick, rest, send_raw):
    who = (rest.split() or [nick])[0]
    nowm = time.monotonic()
    with _violenta_lock:
        if nowm < _violenta_last.get(target, 0.0):
            return True
        _violenta_last[target] = nowm + VIOLENTA_COOLDOWN
    send_raw("PRIVMSG %s :%s" % (target, random.choice(VIOLENTA_LINES) % who))
    return True


# --- !vandalizza: sfogo disagiato, stesso formato di !aperisesso ------------
# Katia (2026-10-31): "molto piu' disagiato di aperisesso e stesso format".
# Ten tiers of five lines, one random line per invocation, target nick in %s.
VANDALIZZA_COOLDOWN = 2.0
VANDALIZZA_TIERS = [
    [
        "%s bomboletta alla mano: il muretto del condominio ora è un museo.",
        "%s scrive il tuo nome sul portone, con la vernice sbagliata.",
        "%s fa un tag sul cartello del divieto di sosta: ora è un'opera.",
        "%s bomboletta nera sul cassonetto, firma con tre errori.",
        "%s disegna una faccia sul vetro del bus e scappa.",
    ],
    [
        "%s incolla un adesivo sul citofono: «sono io, aprite».",
        "%s suona tutti i citofoni del palazzo urlando il tuo nome.",
        "%s blocca il portone con lo scotch e aspetta il caos.",
        "%s scrive «vendesi» sul tuo citofono, col tuo numero.",
        "%s stacca la targhetta del campanello e se la mette in tasca.",
    ],
    [
        "%s buca le gomme dello scooter e lascia un biglietto: «ciao».",
        "%s lega il monopattino al palo con dieci lucchetti.",
        "%s mette lo zucchero nel serbatoio e fa finta di niente.",
        "%s ruba lo specchietto e lo appende al muro come trofeo.",
        "%s scrive «fermo» sul sellino col pennarello indelebile.",
    ],
    [
        "%s uova sul portone, farina sul tappeto, sorriso in faccia.",
        "%s ketchup sul citofono: sembra un horror low budget.",
        "%s riempie la cassetta delle lettere di coriandoli e yoghurt.",
        "%s spalma la maionese sulle maniglie e aspetta.",
        "%s lancia farina dal balcone gridando «nevica».",
    ],
    [
        "%s gira tutti i cartelli del paese di 90 gradi.",
        "%s attacca adesivi «buca qui» sui lampioni.",
        "%s porta via il cartello STOP e lo mette in salotto.",
        "%s dipinge le strisce pedonali a zig-zag, per la scienza.",
        "%s mette gli occhi di plastica su tutte le fioriere.",
    ],
    [
        "%s ribalta il cassonetto e ci si siede sopra come un trono.",
        "%s chiude la serranda del bar con catena e lucchetto.",
        "%s mette il sale nel deumidificatore del negozio.",
        "%s infila un carrello della spesa nella fontana.",
        "%s sposta i coni in mezzo alla strada e si nasconde.",
    ],
    [
        "%s mangia un panino al tonno sui gradini della chiesa e urla.",
        "%s balla la macarena davanti alla banca.",
        "%s entra nel bar, ordina acqua del rubinetto e resta tre ore.",
        "%s parla col piccione e litiga per il pane.",
        "%s fa il verso del clacson a ogni semaforo rosso.",
    ],
    [
        "%s prende a calci il citofono e poi chiede scusa al citofono.",
        "%s si sdraia nel materasso abbandonato e ci dorme dentro.",
        "%s mette la colla sulla panchina e aspetta il primo che si siede.",
        "%s trasforma la cabina elettrica in un altare con candele e peluche.",
        "%s ruba la bici del prete e la rivende al mercatino.",
    ],
    [
        "%s dipinge il cane del vicino di blu e lo chiama «nuovo look».",
        "%s svuota il cestino del bar nella cassetta delle lettere.",
        "%s mette un cartello «gratis» sulla macchina del vicino.",
        "%s pota le piante del condominio a forma di fallo.",
        "%s scrive «vota %s» sui muri della scuola.",
    ],
    [
        "%s arriva col monopattino, bomboletta in bocca, e riscrive la toponomastica.",
        "%s mette il tappeto del vicino sul tetto e lo chiama «moquette».",
        "%s chiude il paese con un nastro di plastica e un cartello «lavori».",
        "%s fa un falò con le sedie del bar e ci arrostisce i marshmallow.",
        "%s si proclama sindaco del nulla e intitola la piazza a te.",
    ],
]
_van_lock = threading.Lock()
_van_last = {}             # channel -> monotonic timestamp until it is free


def _handle_vandalizza(target, nick, rest, send_raw):
    who = (rest.split() or [nick])[0]
    flat = [line for tier in VANDALIZZA_TIERS for line in tier]
    nowm = time.monotonic()
    with _van_lock:
        if nowm < _van_last.get(target, 0.0):
            return True
        _van_last[target] = nowm + VANDALIZZA_COOLDOWN
    line = random.choice(flat)
    try:
        send_raw("PRIVMSG %s :%s" % (target, line % who))
    except TypeError:
        send_raw("PRIVMSG %s :%s" % (target, line % (who, who)))
    return True


# --- !doccia: doccia col vapore "speciale" (katia/tom, 2026-10-31) -----------
# Tom's idea, katia made it official: people get sent into shower cabins
# where the "vapore" is actually gas. Same shape as !aperisesso: tiered
# lines, one random per call, nick in %s, short channel cooldown.
DOCCIA_COOLDOWN = 2.0
DOCCIA_TIERS = [
    [
        "%s entra nella doccia: il cartello dice «vapore», il vapore è gas.",
        "%s apre il rubinetto e dal soffione esce un sibilo che sa di bruciato.",
        "%s si insapona e la schiuma non fa bolle, fa fumo.",
        "%s prenota la doccia del camping: l'acqua è tiepida, l'aria no.",
        "%s trova un gettone della doccia e una maschera antigas nel cestino.",
    ],
    [
        "%s accende la doccia e il vapore gli sussurra il suo nome.",
        "%s nella doccia canta, ma la voce che risponde non è la sua.",
        "%s esce dalla doccia con i capelli asciutti e un mal di testa chimico.",
        "%s mette lo shampoo, il vapore lo sostituisce col gas esilarante.",
        "%s chiede «ma è vapore?» e il gettone risponde «quasi».",
    ],
    [
        "%s apre la doccia del motel e il vapore ha un colore che non esiste.",
        "%s si rade nella doccia e lo specchio si appanna di verde.",
        "%s porta la maschera antigas in doccia, per sicurezza, e ci fa la doccia.",
        "%s sente puzza di uova nella doccia e pensa «è solo zolfo».",
        "%s trova il pulsante «extra vapore» e lo preme, per curiosità.",
    ],
    [
        "%s attacca la doccia al gas del fornello e chiama il risultato «spa».",
        "%s entra nella doccia coi cerini in tasca, per sport.",
        "%s mette il metano nell'acqua calda e dice «è solo aromaterapia».",
        "%s fa la doccia col gas del frigo: fresco, ma tossico.",
        "%s monta la bombola del campeggio sullo scaldabagno: «vapore potenziato».",
    ],
    [
        "%s nella doccia del circolo trova un cartello: «non respirare, sorridi».",
        "%s esce dalla doccia con la pelle che fischia.",
        "%s fa la doccia e il vapore gli chiede il documento.",
        "%s si insapona col gas lacrimogeno e piange per motivi igienici.",
        "%s dalla doccia sente la sigla del telegiornale e un conto alla rovescia.",
    ],
    [
        "%s prenota la doccia alle 3:33, il vapore è già lì che lo aspetta.",
        "%s apre il rubinetto e dal soffione cade un biglietto: «respira piano».",
        "%s trova la doccia del condominio sigillata col nastro adesivo e la apre lo stesso.",
        "%s si fa la doccia e il vapore gli ruba un polmone, restituisce l'altro.",
        "%s esce dalla doccia e il vicino gli chiede «tutto bene?» con la maschera.",
    ],
    [
        "%s installa la doccia a gas in salotto e invita gli amici.",
        "%s fa la doccia col gas esilarante e ride finché non sviene, poi ride ancora.",
        "%s mette il cartello «vapore» sulla bombola e ci fa la doccia.",
        "%s entra nella doccia e il vapore gli dice «benvenuto a casa».",
        "%s dalla doccia chiama il tecnico: «il vapore mi sta guardando».",
    ],
    [
        "%s brevetta la doccia che fa gas e la chiama «benessere olistico».",
        "%s nella doccia respira a rate e conta i secondi.",
        "%s esce dalla doccia col vapore che lo segue e si siede accanto.",
        "%s fa la doccia e il contatore del gas gira al contrario.",
        "%s lascia la doccia accesa per il prossimo e un biglietto: «goditela».",
    ],
]
_doccia_lock = threading.Lock()
_doccia_last = {}          # channel -> monotonic timestamp until it is free


def _handle_doccia(target, nick, rest, send_raw):
    who = (rest.split() or [nick])[0]
    flat = [line for tier in DOCCIA_TIERS for line in tier]
    nowm = time.monotonic()
    with _doccia_lock:
        if nowm < _doccia_last.get(target, 0.0):
            return True
        _doccia_last[target] = nowm + DOCCIA_COOLDOWN
    send_raw("PRIVMSG %s :%s" % (target, random.choice(flat) % who))
    return True


# --- !volcano: Volcano vaporizer / stoner cringe (katia, 2026-10-31) --------
# Same shape as !doccia: tiered lines, one random per call, nick in %s,
# 10s per-channel cooldown. Storz & Bickel bag-and-balloon humour.
VOLCANO_COOLDOWN = 10.0
VOLCANO_TIERS = [
    [
        "%s fires up the Volcano and waits for the bag like it's life support.",
        "%s fills a bag, forgets about it, and it goes stale on the table.",
        "%s cranks the dial to 9 and says «trust me, it's smooth».",
        "%s inhales from the bag and immediately reviews the terp profile.",
        "%s holds the balloon like a newborn and refuses to share.",
    ],
    [
        "%s explains convection vs combustion to a room that stopped listening.",
        "%s says «it's medicinal» while filling the fourth bag.",
        "%s coughs once and blames the temperature, not the twenty bags.",
        "%s puts the whip on and narrates the vapour like a sommelier.",
        "%s fills the bag and googles «how long does AVB last» at the same time.",
    ],
    [
        "%s asks if anyone wants a hit, then doesn't pass it.",
        "%s scrapes the AVB into a jar labelled «later», which never comes.",
        "%s inhales and says «tastes like pine» with total confidence.",
        "%s turns the Volcano on and the whole room smells like a dorm.",
        "%s claims the bag is «reusable» and then proves it.",
    ],
    [
        "%s packs the chamber like it owes him money.",
        "%s says «I only vape now» and coughs up a lung.",
        "%s fills a bag, loses the bag, fills another.",
        "%s explains the boiling point of THC to people checking their phones.",
        "%s calls the balloon «the oracle» and asks it questions.",
    ],
    [
        "%s sets it to 230°C and the vapour comes out looking like car exhaust.",
        "%s offers a «microdose» that fills two bags.",
        "%s says the vapour tastes «earthy» and everyone nods politely.",
        "%s brings the Volcano to the party and becomes the party.",
        "%s cleans the chamber once a year, whether it needs it or not.",
    ],
    [
        "%s hits the bag so hard the neighbours can hear the inhale.",
        "%s says «I'm barely high» while holding on to the table.",
        "%s labels the AVB «compost» and eats a spoon of it.",
        "%s argues indica vs sativa while the bag slowly deflates.",
        "%s puts a second bag on and calls it «the encore».",
    ],
    [
        "%s names the Volcano, talks to it, and apologises when it cools down.",
        "%s says «the terpenes are crazy» about a bag of plain hot air.",
        "%s fills a bag for the cat and the cat leaves.",
        "%s says the whip is «for guests» and never lets anyone touch it.",
        "%s performs the ritual: grind, pack, wait, forget, refill.",
    ],
    [
        "%s turns the Volcano into a personality and the room into a hotbox.",
        "%s says «one more bag» for the ninth time.",
        "%s hits the bag, coughs, and says «that's the good stuff».",
        "%s falls asleep mid-sentence with the fan still running.",
        "%s calls it «the lung», and the lung has been running all night.",
    ],
]
_volcano_lock = threading.Lock()
_volcano_last = {}         # channel -> monotonic timestamp until it is free


def _handle_volcano(target, nick, rest, send_raw):
    who = (rest.split() or [nick])[0]
    flat = [line for tier in VOLCANO_TIERS for line in tier]
    nowm = time.monotonic()
    with _volcano_lock:
        if nowm < _volcano_last.get(target, 0.0):
            return True
        _volcano_last[target] = nowm + VOLCANO_COOLDOWN
    send_raw("PRIVMSG %s :%s" % (target, random.choice(flat) % who))
    return True


# --- !ciclo: where is tom in his cycle? (katia, 2026-10-31; join/quit and
# #104 added 2026-11-02) -----------------------------------------------------
# Reads the network's own bot log and counts the MODE/KICK/JOIN/PART/QUIT
# lines whose source is tom; the running total picks the phase. Cheap,
# read-only, no state. Works in every channel the bot is in, #104 included.
CICLO_PHASES = [
    "fase 0 — dorme",
    "fase 1 — si sveglia e tocca il nick",
    "fase 2 — oppa per noia",
    "fase 3 — deoppa per sport",
    "fase 4 — primo kick di prova",
    "fase 5 — modalità buttafuori",
    "fase 6 — picco: mode e kick a raffica",
    "fase 7 — si pente e rioppa tutti",
    "fase 8 — down, fissa il vuoto",
    "fase 9 — silenzio tombale",
    "fase 10 — riparte piano",
    "fase 11 — di nuovo carico",
]
CICLO_COOLDOWN = 3.0
_ciclo_lock = threading.Lock()
_ciclo_last = {}
_rx_tom_action = re.compile(r":tom!\S+ (MODE|KICK|JOIN|PART|QUIT)\b", re.I)
_rx_tom_dest = re.compile(r"\bKICK \S+ tom\b", re.I)


def _handle_ciclo(target, send_raw):
    nowm = time.monotonic()
    with _ciclo_lock:
        if nowm - _ciclo_last.get(target, 0.0) < CICLO_COOLDOWN:
            return True
        _ciclo_last[target] = nowm
    path = os.environ.get("BOT_LOG", "/data/bot.ircnet.log")
    counts = {"MODE": 0, "KICK": 0, "JOIN": 0, "PART": 0, "QUIT": 0}
    n_kicked = 0
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line_l = line.lower()
                if ":tom!" not in line_l and not _rx_tom_dest.search(line):
                    continue
                m = _rx_tom_action.search(line)
                if m:
                    counts[m.group(1).upper()] += 1
                elif _rx_tom_dest.search(line):
                    n_kicked += 1
    except OSError as e:
        send_raw("PRIVMSG %s :ciclo: non riesco a leggere il log (%s)"
                 % (target, e))
        return True
    n_mode, n_kick = counts["MODE"], counts["KICK"]
    n_join, n_part, n_quit = counts["JOIN"], counts["PART"], counts["QUIT"]
    total = n_mode + n_kick + n_join + n_part + n_quit + n_kicked
    phase = CICLO_PHASES[total % len(CICLO_PHASES)]
    send_raw("PRIVMSG %s :tom: %s (mode=%d kick=%d join=%d part=%d quit=%d "
             "subiti=%d, tot=%d)"
             % (target, phase, n_mode, n_kick, n_join, n_part, n_quit,
                n_kicked, total))
    return True


# --- !cc: realistic-looking but non-chargeable card + balance --------------
# Only in #cybernet and #amici (katia). Five seconds after the
# trigger it emits ONE synthetic card, formatted like an account panel: holder,
# brand, PAN, expiry, CVV, CAP and a balance between 50 and 940 EUR. The PAN is
# a random Luhn-valid number built on real-looking neobank BIN prefixes, so it
# looks plausible but is generated locally and never corresponds to an account.
CC_CHANNELS = ("#cybernet", "#amici")
CC_DELAY = 5.0
CC_COOLDOWN = 15.0
# (brand, BIN prefix, total digits) — Revolut / N26 / generic Visa-MC prefixes
CC_BINS = (
    ("Revolut", "459654", 16),
    ("Revolut", "535456", 16),
    ("N26", "535461", 16),
    ("N26", "535462", 16),
    ("Visa", "453914", 16),
    ("Mastercard", "524157", 16),
)
CC_FIRST = ("Giulia", "Marco", "Sara", "Luca", "Elena", "Davide", "Chiara",
            "Matteo", "Alice", "Paolo", "Federica", "Andrea", "Valentina",
            "Stefano", "Martina", "Simone", "Francesca", "Alessio", "Ilaria",
            "Michele", "Beatrice", "Gabriele", "Silvia", "Riccardo", "Anna",
            "Lorenzo", "Camilla", "Filippo", "Giorgia", "Nicola")
CC_LAST = ("Rossi", "Bianchi", "Ferrari", "Romano", "Esposito", "Ricci",
           "Marino", "Greco", "Conti", "De Luca", "Galli", "Costa", "Fontana",
           "Moretti", "Barbieri", "Rinaldi", "Giordano", "Mancini", "Russo",
           "Lombardi", "Leone", "Martini", "Colombo", "Rizzo", "Vitale",
           "Sartori", "Cattaneo", "Pagano", "Caruso", "Fabbri")
CC_FOREIGN_FIRST = ("James", "Emma", "Lukas", "Sofia", "Pierre", "Camille",
                    "Miguel", "Lucía", "Hans", "Greta", "Ivan", "Olga",
                    "Yusuf", "Aylin", "Kenji", "Mei", "Noah", "Léa",
                    "Andrei", "Marta", "Tomasz", "Zofia", "Erik", "Astrid")
CC_FOREIGN_LAST = ("Smith", "Müller", "Dubois", "García", "Novak", "Kowalski",
                   "Johansson", "O'Brien", "Fernández", "Schmidt", "Petrov",
                   "Yılmaz", "Tanaka", "Kim", "de Vries", "Andersen",
                   "Popescu", "Horváth", "Silva", "Costa", "Nielsen", "Weber")
_cc_lock = threading.Lock()
_cc_last = {}              # channel -> monotonic timestamp of last run


def _luhn_check_digit(prefix):
    total = 0
    for i, ch in enumerate(reversed(prefix)):
        d = ord(ch) - 48
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def _fake_pan(prefix, length):
    body = prefix + "".join(str(random.randint(0, 9))
                            for _ in range(length - 1 - len(prefix)))
    return body + _luhn_check_digit(body)


def _group_pan(pan):
    if len(pan) == 15:                       # Amex: 4-6-5
        return "%s %s %s" % (pan[:4], pan[4:10], pan[10:])
    return " ".join(pan[i:i + 4] for i in range(0, len(pan), 4))


def _fake_cc_line(holder):
    brand, prefix, length = random.choice(CC_BINS)
    pan = _group_pan(_fake_pan(prefix, length))
    mm = random.randint(1, 12)
    yy = random.randint(27, 33)
    cvv = "%04d" % random.randint(0, 9999) if brand == "Amex" \
        else "%03d" % random.randint(0, 999)
    cap = "%05d" % random.randint(10, 98099)
    balance = random.uniform(50, 940)
    return ("💳 %s · %s %s · scad. %02d/%02d · CVV %s · CAP %s · saldo €%.2f"
            % (holder, brand, pan, mm, yy, cvv, cap, balance))


def _handle_cc(target, nick, rest, send_raw):
    if target not in CC_CHANNELS:
        return True
    nowm = time.monotonic()
    with _cc_lock:
        if nowm - _cc_last.get(target, 0.0) < CC_COOLDOWN:
            return True
        _cc_last[target] = nowm
    if rest.strip():
        holder = rest.strip()[:40]
    elif random.random() < 0.5:
        holder = "%s %s" % (random.choice(CC_FOREIGN_FIRST),
                            random.choice(CC_FOREIGN_LAST))
    else:
        holder = "%s %s" % (random.choice(CC_FIRST), random.choice(CC_LAST))

    def fire():
        time.sleep(CC_DELAY)
        send_raw("PRIVMSG %s :%s" % (target, _fake_cc_line(holder)))

    threading.Thread(target=fire, daemon=True).start()
    return True


# --- !takedown: theatre-grade DDoS takedown (katia, 2026-10-31) --------------
# Takes a nick or an IP. A nick goes through WHOIS (parsed out of the bot's
# own log, since irc_commands never sees numerics); the host is used if it is
# an IP, otherwise a stable fake IP is derived from the nick because cloaks do
# not resolve. The ASN/geo come from an open lookup, then a staged, realistic
# operational log: recon, C2 selection, ramp-up, peak, mitigation, verdict.
TAKEDOWN_COOLDOWN = 20.0
TAKEDOWN_WHOIS_WAIT = 9.0
_rx_whois311 = re.compile(r"\s311\s+\S+\s+(\S+)\s+\S+\s+(\S+)\s+\*")
_td_lock = threading.Lock()
_td_last = {}              # channel -> monotonic timestamp
_TD_FIRST_OCTETS = (45, 51, 62, 77, 85, 91, 103, 109, 141, 151,
                    176, 185, 188, 193, 194, 195, 212, 213)
_TD_ORGS = ("LAMER-NET", "BOTNET HOLDINGS", "SKID ROW ISP",
            "BLACKHAT TRANSIT", "1337 BACKBONE", "RANSOM CLOUD")
_TD_CITIES = ("Frankfurt, DE", "Amsterdam, NL", "Bucharest, RO",
              "Sofia, BG", "Vinnytsia, UA", "Riga, LV", "Chisinau, MD",
              "Sao Paulo, BR", "Singapore, SG", "Dallas, US")
_TD_VECTORS = ("TCP SYN", "UDP amplification", "HTTP/2 rapid reset",
               "TLS renegotiation", "DNS/NTP reflection", "QUIC flood")
_TD_PORTS = ("443", "80", "53", "22", "3389", "8080", "9001")


def _td_log_paths():
    paths = ["/data/bot.log"]
    try:
        for name in sorted(os.listdir("/data")):
            if name.startswith("bot.") and name.endswith(".log"):
                paths.append(os.path.join("/data", name))
    except OSError:
        pass
    return paths


def _td_whois(nick, target, send_raw, timeout=TAKEDOWN_WHOIS_WAIT):
    """Send WHOIS and return the 311 host from whichever bot log gets it."""
    tails = []
    for path in _td_log_paths():
        try:
            tails.append(_LogTail(path))
        except OSError:
            pass
    send_raw("WHOIS %s" % nick)
    try:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for tail in tails:
                for line in tail.new_lines():
                    m = _rx_whois311.search(line)
                    if m and m.group(1).lower() == nick.lower():
                        return m.group(2)
            time.sleep(0.25)
    finally:
        for tail in tails:
            tail.close()
    return None


def _td_resolve(host):
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except Exception:
        return None


def _td_fake_ip(seed):
    h = hashlib.sha256(seed.lower().encode()).digest()
    return "%d.%d.%d.%d" % (
        _TD_FIRST_OCTETS[h[0] % len(_TD_FIRST_OCTETS)],
        h[1] or 1, h[2], (h[3] % 254) + 1)


def _td_fake_asn(seed):
    h = hashlib.sha256(("asn:" + seed.lower()).encode()).digest()
    return "AS%d" % (1000 + h[0] * 256 + h[1]), _TD_ORGS[h[2] % len(_TD_ORGS)]


def _td_asn(ip):
    try:
        req = urllib.request.Request(
            "https://api.hackertarget.com/aslookup/?q=" + urllib.parse.quote(ip),
            headers={"User-Agent": "ocbot/takedown"})
        with urllib.request.urlopen(req, timeout=8) as r:
            txt = r.read().decode("utf-8", "replace").strip()
        m = re.search(r'"([0-9.]+)"\s*,\s*"(\d+)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"', txt)
        if m:
            return "AS" + m.group(2), m.group(4)
    except Exception:
        pass
    try:
        req = urllib.request.Request("https://ipinfo.io/%s/json" % ip,
                                     headers={"User-Agent": "ocbot/takedown"})
        with urllib.request.urlopen(req, timeout=8) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
        org = (obj.get("org") or "").strip()
        m = re.match(r"AS(\d+)\s*(.*)", org)
        if m:
            return "AS" + m.group(1), m.group(2) or "?"
    except Exception:
        pass
    return None


def _td_geo(ip, seed):
    try:
        req = urllib.request.Request("https://ipinfo.io/%s/json" % ip,
                                     headers={"User-Agent": "ocbot/takedown"})
        with urllib.request.urlopen(req, timeout=6) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
        city = (obj.get("city") or "").strip()
        country = (obj.get("country") or "").strip()
        if city and country:
            return "%s, %s" % (city, country)
    except Exception:
        pass
    h = hashlib.sha256(("geo:" + seed.lower()).encode()).digest()
    return _TD_CITIES[h[0] % len(_TD_CITIES)]


def _td_run(target, arg, send_raw, emit):
    def say(text):
        send_raw("PRIVMSG %s :%s" % (target, text))

    try:
        t0 = time.monotonic()
        arg = arg.lstrip("@+")
        ip = None
        host = None
        try:
            ipaddress.ip_address(arg)
            ip = arg
            who = arg
        except ValueError:
            who = arg
            host = _td_whois(who, target, send_raw)
            if host:
                ip = _td_resolve(host)
        note = ""
        if not ip:
            ip = _td_fake_ip(who)
            note = " (cloak %s, IP stimato)" % (host or "n/d")
        asn = _td_asn(ip)
        if not asn:
            asn = _td_fake_asn(who)
        asn_s, org = asn
        geo = _td_geo(ip, who)
        ports = random.sample(_TD_PORTS, random.randint(2, 4))
        vector = random.choice(_TD_VECTORS)
        nodes = random.randint(9000, 48000)
        clusters = random.randint(4, 9)
        reflect = random.randint(80, 640)
        peak = random.uniform(120.0, 940.0)
        mpps = random.uniform(1.5, 28.0)
        loss = random.choice((99, 100))

        parts = [
            "▸ takedown %s — %s%s" % (who, ip, note),
            "AS%s %s" % (asn_s.lstrip("AS"), org),
            geo,
            ",".join(ports),
            vector,
            "%d cluster/%d nodi/%d reflector" % (clusters, nodes, reflect),
            "picco %.1f Gbps / %.1f Mpps" % (peak, mpps),
            "loss %d%%" % loss,
        ]
        if random.random() < 0.65:
            parts.append("scrubbing anycast → origin %.1f Gbps"
                         % (peak * random.uniform(0.03, 0.12)))
        parts.append("non raggiungibile 3/3")
        parts.append("%.1fs" % (time.monotonic() - t0))
        say(" · ".join(parts))
        emit("TAKEDOWN", target, "who=%s" % who, "ip=%s" % ip,
             "asn=%s" % asn_s, "src_ips=%d" % nodes)
    except Exception as e:
        emit("TAKEDOWN_ERROR", target, repr(e))
        try:
            say("takedown failed: %s" % str(e)[:180])
        except Exception:
            pass


def _handle_takedown(target, nick, rest, send_raw, emit):
    arg = rest.strip()
    if not arg:
        send_raw("PRIVMSG %s :uso: !takedown <nick|ip>" % target)
        return True
    arg = arg.split()[0]
    nowm = time.monotonic()
    with _td_lock:
        if nowm - _td_last.get(target, 0.0) < TAKEDOWN_COOLDOWN:
            return True
        _td_last[target] = nowm
    threading.Thread(target=_td_run, args=(target, arg, send_raw, emit),
                     daemon=True).start()
    return True


# --- !ignore / !unignore (katia, 2026-10-31) --------------------------------
# Trusted-only. Edits /data/ignore.txt, the same file the brain watches: an
# ignored nick's events never reach the model (no reply, even when addressed).
# Line format: "nick" (all networks) or "net|nick".
IGNORE_FILE = os.environ.get("BOT_IGNORE_FILE", "/data/ignore.txt")
_ignore_lock = threading.Lock()


def _net_label():
    base = os.path.basename(os.environ.get("BOT_LOG", ""))
    if base.startswith("bot.") and base.endswith(".log"):
        return base[4:-4].lower()
    return ""


def _ignore_rule(net, who):
    return "%s|%s" % (net or "*", who.lower())


def _handle_ignore(target, nick, rest, trusted, send_raw, remove=False):
    if not target.startswith("#"):
        return False
    if not trusted:
        send_raw("PRIVMSG %s :%s: comando riservato ai trusted." % (target, nick))
        return True
    net = _net_label() or "*"
    who = rest.strip().split()[0] if rest.strip() else ""
    if not who:
        try:
            with open(IGNORE_FILE, errors="replace") as f:
                cur = [ln.strip() for ln in f
                       if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError:
            cur = []
        send_raw("PRIVMSG %s :ignore: %s" % (target, ", ".join(cur) or "(vuoto)"))
        return True
    rule = _ignore_rule(net, who)
    with _ignore_lock:
        try:
            with open(IGNORE_FILE, errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        kept = [ln for ln in lines if ln.strip().lower() != rule]
        if remove:
            changed = len(kept) != len(lines)
            lines = kept
        else:
            changed = len(kept) == len(lines)
            if changed:
                kept.append(rule)
            lines = kept
        if changed:
            tmp = IGNORE_FILE + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
            os.replace(tmp, IGNORE_FILE)
    if remove:
        send_raw("PRIVMSG %s :%s non è più ignorato." % (target, who))
    else:
        send_raw("PRIVMSG %s :ok, ignoro %s su %s." % (target, who, net))
    return True


# --- !russianroulette / !bang ----------------------------------------------
RR_CHAMBERS = 6
RR_LOCK = threading.RLock()
RR_GAMES = {}          # channel(lower) -> game dict
RR_INSTANCE = uuid.uuid4().hex
RR_OWNER = RR_INSTANCE
RR_LOADED = False
RR_SEND = None
RR_THREAD = None
RR_REMIND_AFTER = float(os.environ.get("BOT_RR_REMIND", "30"))
RR_REMIND_MAX = int(os.environ.get("BOT_RR_REMIND_MAX", "3"))
RR_CATASTROPHES = (
    "{nick} is hit by a falling piano.",
    "{nick} is struck by lightning.",
    "{nick} is swept away by a surprise flood.",
    "{nick} is abducted by a passing UFO.",
    "{nick} is crushed by a meteor.",
    "{nick} spontaneously combusts.",
    "{nick} falls into a plot hole.",
    "{nick} is eaten by a grue.",
)
RR_REASONS = (
    "PWNAGE!!!!", "BANG", "click click BOOM", "roulette is a harsh mistress",
    "REKT", "skill issue", "the chamber was loaded", "and stay out",
    "you played yourself", "ejected by the roulette police", "bonk",
    "no luck today", "wiki-free and bullet-full", "bye felicia", "YEET",
)
RR_SELF = frozenset(
    n.strip().lower()
    for n in os.environ.get("BOT_SELF_NICKS", "ocbot,mammt").split(",")
    if n.strip()
)
# Extra names that never get a live round, on top of the per-user
# no_quick_time_events flag. Empty by default: no hardcoded favourites.
RR_IMMUNE = frozenset(
    n.strip().lower()
    for n in os.environ.get("BOT_RR_IMMUNE", "").split(",")
    if n.strip()
)
RR_ACTIVE_WINDOW = float(os.environ.get("BOT_RR_ACTIVE_WINDOW", "300"))
RR_NAMES_WAIT = float(os.environ.get("BOT_RR_NAMES_WAIT", "1.5"))
RR_SERVICE = frozenset({
    "chanserv", "nickserv", "memoserv", "operserv", "hostserv", "botserv",
    "helpserv", "statserv", "global", "services", "server",
})
_RX_BOTLOG_MSG = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}) < :([^!]+)!\S+ PRIVMSG (\S+) :")
_RX_NAMES353 = re.compile(r"\s353\s+\S+\s+\S+\s+(\S+)\s+:(.*)$")
_RX_NAMES366 = re.compile(r"\s366\s+\S+\s+(\S+)\s+:")


def _rr_log_path():
    path = os.environ.get("BOT_LOG") or ""
    if path:
        return path
    label = _net_label()
    return ("/data/bot.%s.log" % label) if label else "/data/bot.log"


def _rr_live_roster(target, send_raw, timeout=None):
    """Authoritative member list for `target` from a live NAMES query.

    The events-log replay in _channel_roster only knows what the bot happened
    to see (missed joins before it joined, holes after a reconnect), so katia
    asked for the real thing: ask the server. The reply only exists as 353/366
    lines in the bot's own raw log, so tail that and parse. Returns a dict
    lower-case nick -> server casing, or None when the server never answered
    (caller falls back to the event replay).
    """
    timeout = RR_NAMES_WAIT if timeout is None else timeout
    try:
        tail = _LogTail(_rr_log_path())
    except OSError:
        return None
    names = {}
    done = False
    try:
        send_raw("NAMES %s" % target)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for line in tail.new_lines():
                m = _RX_NAMES353.search(line)
                if m and m.group(1).lower() == target.lower():
                    for n in m.group(2).split():
                        n = n.lstrip("@+%&~!").strip()
                        if n:
                            names.setdefault(n.lower(), n)
                    continue
                m = _RX_NAMES366.search(line)
                if m and m.group(1).lower() == target.lower():
                    done = True
            if done:
                break
            time.sleep(0.1)
    finally:
        tail.close()
    return names if done else None


def _rr_resolve_members(names, roster):
    """Map requested nicks onto the live roster: exact case-insensitive match
    first, then a unique prefix (so "hexa" finds "hexa-"). Returns
    (present, absent); present keeps the server's casing."""
    if roster is None:
        return list(names), []
    present, absent = [], []
    for n in names:
        nl = n.lower()
        if nl in roster:
            present.append(roster[nl])
            continue
        hits = [m for m in roster if m.startswith(nl)]
        if len(hits) == 1:
            present.append(roster[hits[0]])
        else:
            absent.append(n)
    return present, absent


def _rr_recent_active(target, send_raw, window=None, roster=None):
    """Nicks that spoke in `target` within the last `window` seconds and are
    still in the channel.

    The bot's own raw log has second-resolution PRIVMSG lines; the events log
    only has whole minutes, so we read bot.<net>.log behind the bot's back.
    Speakers are intersected with the live roster (a real NAMES query, with
    the events replay only as fallback) so nobody who is not actually in the
    room gets dragged into a game. Returns [] when the log/roster is
    unavailable (caller falls back to the founder).
    """
    window = RR_ACTIVE_WINDOW if window is None else window
    if roster is None:
        roster = _rr_live_roster(target, send_raw)
    if roster is None:
        roster = {n.lower(): n for n in _channel_roster(target)}
    if not roster:
        return []
    path = _rr_log_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            start = max(0, size - 500000)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    # The log uses the bot host's clock, which may differ from this process's
    # (UTC vs local): never compare against time.time(). Anchor on the newest
    # inbound line in the tail and measure ages in seconds-of-day from there.
    tgt = target.lower()
    entries = []
    ref = None
    for line in lines:
        m = _RX_BOTLOG_MSG.match(line)
        if not m:
            continue
        hh, mm, ss, nk, chan = m.groups()
        sod = int(hh) * 3600 + int(mm) * 60 + int(ss)
        if ref is None or sod > ref:
            ref = sod
        if chan.lower() == tgt:
            entries.append((sod, nk))
    if ref is None:
        return []
    seen = {}
    for sod, nk in reversed(entries):
        delta = ref - sod
        if delta < 0:
            delta += 86400  # crossed midnight
        if delta > window:
            continue
        nl = nk.lower()
        if (nl in RR_SELF or nl in RR_SERVICE or nl in seen
                or nl not in roster):
            continue
        seen[nl] = roster[nl]
    return list(seen.values())


def _rr_state_path():
    label = _net_label()
    return "/data/.rr_games.%s.json" % label if label else "/data/.rr_games.json"


def _rr_save_locked():
    try:
        path = _rr_state_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"owner": RR_INSTANCE, "games": RR_GAMES}, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _rr_load_locked():
    global RR_LOADED, RR_OWNER
    if RR_LOADED:
        return
    RR_LOADED = True
    try:
        with open(_rr_state_path(), errors="replace") as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    owner = data.get("owner")
    if isinstance(owner, str) and owner:
        RR_OWNER = owner
    games = data.get("games")
    if isinstance(games, dict):
        for k, v in games.items():
            if (isinstance(v, dict) and isinstance(v.get("players"), list)
                    and v["players"] and isinstance(v.get("idx"), int)):
                RR_GAMES[str(k)] = v


def _rr_reminder_loop():
    while True:
        time.sleep(5)
        try:
            with open(_rr_state_path(), errors="replace") as f:
                if json.load(f).get("owner") != RR_INSTANCE:
                    return
        except Exception:
            pass
        now = time.time()
        lines = []
        with RR_LOCK:
            for key, g in list(RR_GAMES.items()):
                last = g.get("last_action") or g.get("start") or now
                if now - last < RR_REMIND_AFTER:
                    continue
                g["last_action"] = now
                chan = g.get("chan", key)
                cur = g["players"][g["idx"]]
                if g.get("reminders", 0) >= RR_REMIND_MAX:
                    players = g["players"]
                    # Buddha mode (no_quick_time_events): a catastrophe must
                    # not take the immune staller out either. It fizzles and
                    # the turn passes on.
                    if (_has_flag(cur, "no_quick_time_events")
                            or cur.lower() in RR_IMMUNE):
                        g["idx"] = (g["idx"] + 1) % len(players)
                        g["reminders"] = 0
                        g["last_action"] = now
                        lines.append(
                            "PRIVMSG %s :*fwip* the catastrophe fizzles, a "
                            "little flag pops out that says BANG, and %s "
                            "lives to spin again. next up: %s, !bang."
                            % (chan, cur, players[g["idx"]]))
                        continue
                    event = random.choice(RR_CATASTROPHES).format(nick=cur)
                    players.pop(g["idx"])
                    lines.append("PRIVMSG %s :%s" % (chan, event))
                    lines.append("KICK %s %s :natural causes" % (chan, cur))
                    if not players:
                        RR_GAMES.pop(key, None)
                        lines.append("PRIVMSG %s :roulette: everyone is gone. "
                                     "game over." % chan)
                    else:
                        if g["idx"] >= len(players):
                            g["idx"] = 0
                        g["reminders"] = 0
                        lines.append("PRIVMSG %s :%s is up — !bang."
                                     % (chan, players[g["idx"]]))
                else:
                    g["reminders"] = g.get("reminders", 0) + 1
                    lines.append("PRIVMSG %s :%s: still waiting — !bang "
                                 "(or !russianroulette stop). [%d/%d]"
                                 % (chan, cur, g["reminders"],
                                    RR_REMIND_MAX))
            if lines:
                _rr_save_locked()
        for line in lines:
            if RR_SEND:
                try:
                    RR_SEND(line)
                except Exception:
                    pass


def _rr_ensure_thread():
    global RR_THREAD
    if RR_THREAD is not None and RR_THREAD.is_alive():
        return
    RR_THREAD = threading.Thread(target=_rr_reminder_loop, daemon=True)
    RR_THREAD.start()


def _rr_housekeep():
    global RR_OWNER
    with RR_LOCK:
        _rr_load_locked()
        if RR_OWNER != RR_INSTANCE:
            RR_OWNER = RR_INSTANCE
            _rr_save_locked()
        have = bool(RR_GAMES)
    if have:
        _rr_ensure_thread()


def _rr_players(parts):
    out = []
    for n in parts:
        n = n.strip().lstrip("@+%&~").strip(",;:")
        if not n or n.lower() in RR_SELF:
            continue
        if any(n.lower() == p.lower() for p in out):
            continue
        out.append(n)
    return out[:12]


def _handle_russianroulette(target, nick, rest, send_raw):
    key = target.lower()
    low = rest.strip().lower()
    if low in ("stop", "abort", "cancel", "end", "gg"):
        with RR_LOCK:
            g = RR_GAMES.pop(key, None)
            if g:
                _rr_save_locked()
        if g:
            send_raw("PRIVMSG %s :roulette: game over, nobody else gets shot."
                     % target)
        else:
            send_raw("PRIVMSG %s :roulette: no game running." % target)
        return True
    parts = rest.split()
    roster = _rr_live_roster(target, send_raw)
    absent = []
    if parts:
        players, absent = _rr_resolve_members(_rr_players(parts), roster)
    else:
        players = _rr_players(_rr_recent_active(target, send_raw,
                                                roster=roster))
        if not players:
            players = _rr_players([nick])
    if not players:
        if absent:
            send_raw("PRIVMSG %s :roulette: not in here: %s — /names does not "
                     "lie :3" % (target, ", ".join(absent)))
        else:
            send_raw("PRIVMSG %s :roulette: give me some players, e.g. "
                     "!russianroulette alice bob carol :3" % target)
        return True
    now = time.time()
    with RR_LOCK:
        existing = RR_GAMES.get(key)
        if existing is None:
            RR_GAMES[key] = {
                "players": players,
                "idx": 0,
                "pull": 0,
                "bullet": random.randint(1, RR_CHAMBERS),
                "start": now,
                "last_action": now,
                "reminders": 0,
                "chan": target,
            }
            _rr_save_locked()
    if existing is not None:
        send_raw("PRIVMSG %s :roulette: already running — %s is up. !bang"
                 % (target, existing["players"][existing["idx"]]))
        return True
    _rr_ensure_thread()
    send_raw("PRIVMSG %s :roulette: %d chambers, 1 bullet. players: %s. "
             "first up: %s — !bang when ready. nag every %ds; after %d nags "
             "a natural catastrophe takes the staller out."
             % (target, RR_CHAMBERS, ", ".join(players), players[0],
                max(1, int(round(RR_REMIND_AFTER))), RR_REMIND_MAX))
    return True


def _handle_bang(target, nick, send_raw, rest=""):
    key = target.lower()
    now = time.time()
    with RR_LOCK:
        g = RR_GAMES.get(key)
    if not g:
        return _handle_russianroulette(target, nick, rest, send_raw)
    # Self-pull only: "!bang" pulls for the sender. Pulling on someone else's
    # behalf was an antifeature (hexa-, 2026-11-10) and is gone; any trailing
    # nick is ignored so people cannot fire for each other.
    who = nick
    with RR_LOCK:
        g = RR_GAMES.get(key)
        if not g:
            return False
        cur = g["players"][g["idx"]]
        if who.lower() != cur.lower():
            result = ("wrong", who, cur)
        else:
            g["pull"] += 1
            if g["pull"] >= g["bullet"]:
                if _has_flag(cur, "no_quick_time_events") or cur.lower() in RR_IMMUNE:
                    g["pull"] = 0
                    g["bullet"] = random.randint(1, RR_CHAMBERS)
                    g["idx"] = (g["idx"] + 1) % len(g["players"])
                    g["last_action"] = now
                    _rr_save_locked()
                    result = ("flag", cur, g["players"][g["idx"]])
                else:
                    reason = random.choice(RR_REASONS)
                    RR_GAMES.pop(key, None)
                    _rr_save_locked()
                    result = ("bang", cur, reason)
            else:
                g["idx"] = (g["idx"] + 1) % len(g["players"])
                g["last_action"] = now
                _rr_save_locked()
                result = ("click", g["players"][g["idx"]], None)
    kind, a, b = result
    if kind == "wrong":
        send_raw("PRIVMSG %s :%s: it's %s's turn." % (target, a, b))
    elif kind == "flag":
        send_raw("PRIVMSG %s :*fwip* a little flag pops out that says BANG, "
                 "somewhere a slide whistle goes wheeee-oooop, and %s lives "
                 "to spin again. next up: %s, !bang." % (target, a, b))
    elif kind == "bang":
        send_raw("PRIVMSG %s :*BANG* — %s is out." % (target, a))
        send_raw("KICK %s %s :%s" % (target, a, b))
    else:
        send_raw("PRIVMSG %s :*click* — %s, you're up." % (target, a))
    return True


def _handle_bangs(target, send_raw):
    """Roulette kick leaderboard, replayed from this network's events log."""
    path = _events_path()
    rx = re.compile(r"^\[\d\d:\d\d\] KICK (\S+) HOST=\S+ (#\S+) (\S+) ?(.*)$")
    counts = {}
    total = 0
    try:
        with open(path, errors="replace") as f:
            for line in f:
                m = rx.match(line.rstrip("\n"))
                if not m:
                    continue
                actor, chan, victim, reason = m.groups()
                if chan.lower() != target.lower():
                    continue
                if actor.lower() not in RR_SELF:
                    continue
                key = victim.lower()
                if key in RR_SERVICE:
                    continue
                if _has_flag(victim, "no_roulette_stats"):
                    continue
                if key not in counts:
                    counts[key] = [victim, 0]
                counts[key][1] += 1
                total += 1
    except OSError:
        send_raw("PRIVMSG %s :bang stats: no log to read yet." % target)
        return True
    if not counts:
        send_raw("PRIVMSG %s :bang stats: nobody has been ejected here yet."
                 % target)
        return True
    ranked = sorted(counts.values(), key=lambda x: (-x[1], x[0].lower()))
    top = ", ".join("%s=%d" % (name, n) for name, n in ranked[:8])
    send_raw("PRIVMSG %s :bang stats: %s (%d total kicks)"
             % (target, top, total))
    return True


# --- !species: persistent per-user species, on disk -------------------------
SPECIES_FILE = os.environ.get("BOT_SPECIES_FILE", "/data/species.json")


def _species_load():
    try:
        with open(SPECIES_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in data.items()}


def _species_save(data):
    tmp = SPECIES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, SPECIES_FILE)


def _handle_setspecies(target, nick, rest, trusted, send_raw):
    parts = [p for p in rest.split() if p.lower() != "--persist"]
    if not parts:
        send_raw("PRIVMSG %s :usage: !setspecies <nick> <species>" % target)
        return True
    who = parts[0]
    species = " ".join(parts[1:]).strip()
    if not species:
        send_raw("PRIVMSG %s :usage: !setspecies <nick> <species>" % target)
        return True
    if who.lower() != (nick or "").lower() and not trusted:
        send_raw("PRIVMSG %s :only a trusted hand may set someone else's "
                 "species." % target)
        return True
    data = _species_load()
    data[who.lower()] = species
    try:
        _species_save(data)
    except OSError as e:
        send_raw("PRIVMSG %s :could not persist species: %s" % (target, e))
        return True
    send_raw("PRIVMSG %s :noted and persisted: %s is a %s."
             % (target, who, species))
    return True


def _handle_species(target, rest, send_raw):
    data = _species_load()
    if rest.strip():
        who = rest.strip()
        sp = data.get(who.lower())
        if sp is None:
            send_raw("PRIVMSG %s :no species on record for %s."
                     % (target, who))
        else:
            send_raw("PRIVMSG %s :%s is a %s." % (target, who, sp))
        return True
    if not data:
        send_raw("PRIVMSG %s :no species on record yet." % target)
        return True
    items = ", ".join("%s=%s" % (k, v) for k, v in sorted(data.items()))
    send_raw("PRIVMSG %s :species on record: %s" % (target, items))
    return True


# --- per-user flags: persistent switches like no_quick_time_events ----------
FLAGS_FILE = os.environ.get("BOT_USER_FLAGS_FILE", "/data/user_flags.json")


def _flags_load():
    try:
        with open(FLAGS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if isinstance(v, list):
            out[str(k).lower()] = sorted({str(x) for x in v if str(x)})
    return out


def _flags_save(data):
    tmp = FLAGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, FLAGS_FILE)


def _has_flag(nick, flag):
    if not nick:
        return False
    return flag in _flags_load().get(nick.lower(), [])


def _handle_setflag(target, nick, rest, trusted, send_raw, remove=False):
    parts = rest.split()
    if len(parts) < 2:
        send_raw("PRIVMSG %s :usage: !%s <nick> <flag>"
                 % (target, "unsetflag" if remove else "setflag"))
        return True
    who, flag = parts[0], parts[1].strip().lower()
    if not re.match(r"^[a-z0-9_]{1,48}$", flag):
        send_raw("PRIVMSG %s :flag names are a-z, 0-9 and _." % target)
        return True
    if who.lower() != (nick or "").lower() and not trusted:
        send_raw("PRIVMSG %s :only a trusted hand may change someone else's "
                 "flags." % target)
        return True
    data = _flags_load()
    flags = set(data.get(who.lower(), []))
    if remove:
        flags.discard(flag)
    else:
        flags.add(flag)
    if flags:
        data[who.lower()] = sorted(flags)
    else:
        data.pop(who.lower(), None)
    try:
        _flags_save(data)
    except OSError as e:
        send_raw("PRIVMSG %s :could not persist flags: %s" % (target, e))
        return True
    if remove:
        send_raw("PRIVMSG %s :%s no longer has %s." % (target, who, flag))
    else:
        send_raw("PRIVMSG %s :%s now has %s." % (target, who, flag))
    return True


def _handle_flags(target, rest, send_raw):
    data = _flags_load()
    if rest.strip():
        who = rest.strip().lower()
        flags = data.get(who)
        if not flags:
            send_raw("PRIVMSG %s :no flags on record for %s."
                     % (target, rest.strip()))
        else:
            send_raw("PRIVMSG %s :%s: %s" % (target, rest.strip(),
                                             ", ".join(flags)))
        return True
    if not data:
        send_raw("PRIVMSG %s :no user flags on record yet." % target)
        return True
    items = "; ".join("%s=%s" % (k, ",".join(v))
                      for k, v in sorted(data.items()))
    send_raw("PRIVMSG %s :user flags: %s" % (target, items))
    return True


def handle(target, nick, body, trusted, send_raw, emit, split_say):
    global _busy, _last_run, RR_SEND
    RR_SEND = send_raw
    _rr_housekeep()
    text = (body or "").strip()
    first = text.lower().split()[0:1]
    if first == ["!setspecies"]:
        if not target.startswith("#"):
            return False
        return _handle_setspecies(target, nick,
                                  text[len("!setspecies"):].strip(),
                                  trusted, send_raw)
    if first == ["!species"]:
        if not target.startswith("#"):
            return False
        return _handle_species(target, text[len("!species"):].strip(), send_raw)
    if first == ["!setflag"]:
        if not target.startswith("#"):
            return False
        return _handle_setflag(target, nick, text[len("!setflag"):].strip(),
                               trusted, send_raw)
    if first == ["!unsetflag"]:
        if not target.startswith("#"):
            return False
        return _handle_setflag(target, nick, text[len("!unsetflag"):].strip(),
                               trusted, send_raw, remove=True)
    if first == ["!flags"]:
        if not target.startswith("#"):
            return False
        return _handle_flags(target, text[len("!flags"):].strip(), send_raw)
    if first == ["!ignore"]:
        return _handle_ignore(target, nick, text[len("!ignore"):].strip(),
                              trusted, send_raw)
    if text.lower().split()[0:1] == ["!unignore"]:
        return _handle_ignore(target, nick, text[len("!unignore"):].strip(),
                              trusted, send_raw, remove=True)
    if text.lower().split()[0:1] == ["!tom"]:
        if not target.startswith("#"):
            return False
        return _handle_tom(target, send_raw)
    if text.lower().split()[0:1] == ["!secondome"]:
        if not target.startswith("#"):
            return False
        return _handle_secondome(target, send_raw)
    if text.lower().split()[0:1] == ["!millelire"]:
        if target.lower() != "#cybernet":
            return False
        return _handle_millelire(target, text[len("!millelire"):], send_raw)
    if target.lower() == "#cybernet":
        m = re.match(r"^!([a-z]+)lire$", text.lower())
        if m and m.group(1) in LIRE_WORDS:
            return _lire_to_euro(target, LIRE_WORDS[m.group(1)], send_raw)
        m = re.match(r"^!(\d+)lire$", text.lower())
        if m:
            return _lire_to_euro(target, int(m.group(1)), send_raw)
    if text.lower().split()[0:1] == ["!aperisesso"]:
        if not target.startswith("#"):
            return False
        return _handle_aperisesso(target, nick, text[len("!aperisesso"):].strip(),
                                  send_raw)
    if text.lower().split()[0:1] in (["!violenta"], ["!violenza"]):
        if not target.startswith("#"):
            return False
        cmd = text.split()[0]
        return _handle_violenta(target, nick, text[len(cmd):].strip(),
                                send_raw)
    if text.lower().split()[0:1] == ["!doccia"]:
        if not target.startswith("#"):
            return False
        return _handle_doccia(target, nick, text[len("!doccia"):].strip(),
                              send_raw)
    if text.lower().split()[0:1] == ["!volcano"]:
        if not target.startswith("#"):
            return False
        return _handle_volcano(target, nick, text[len("!volcano"):].strip(),
                               send_raw)
    if text.lower().split()[0:1] == ["!vandalizza"]:
        if not target.startswith("#"):
            return False
        return _handle_vandalizza(target, nick, text[len("!vandalizza"):].strip(),
                                  send_raw)
    if text.lower().split()[0:1] == ["!ciclo"]:
        if not target.startswith("#"):
            return False
        return _handle_ciclo(target, send_raw)
    if text.lower().split()[0:1] == ["!cc"]:
        if not target.startswith("#"):
            return False
        return _handle_cc(target, nick, text[len("!cc"):].strip(), send_raw)
    if text.lower().split()[0:1] == ["!takedown"]:
        if not target.startswith("#"):
            return False
        return _handle_takedown(target, nick, text[len("!takedown"):].strip(),
                                send_raw, emit)
    if text.lower().split()[0:1] == ["!wakeup"]:
        if not target.startswith("#"):
            return False
        return _handle_wakeup(target, nick, text[len("!wakeup"):].strip(),
                              trusted, send_raw)
    if text.strip().lower() in ("gg", "!gg"):
        if not target.startswith("#"):
            return False
        with RR_LOCK:
            running = target.lower() in RR_GAMES
        if running:
            return _handle_russianroulette(target, nick, "stop", send_raw)
        return False
    if text.lower().split()[0:1] == ["!russianroulette"]:
        if not target.startswith("#"):
            return False
        return _handle_russianroulette(
            target, nick, text[len("!russianroulette"):].strip(), send_raw)
    if text.lower().split()[0:1] == ["!bang"]:
        if not target.startswith("#"):
            return False
        return _handle_bang(target, nick, send_raw,
                            text[len("!bang"):].strip())
    if text.lower().split()[0:1] == ["!bangs"]:
        if not target.startswith("#"):
            return False
        return _handle_bangs(target, send_raw)
    if text.lower().startswith("!steve"):
        return _handle_steve(target, nick, text, trusted, send_raw, emit)
    if not text.lower().startswith("!ab2filter"):
        return False
    if not target.startswith("#"):
        # keep it a channel command; DMs already have their own gate
        return False

    rest = text[len("!ab2filter"):].strip()
    if rest in ("", "help", "-h", "--help"):
        send_raw("PRIVMSG %s :usage: !ab2filter <url> [field] <pattern> | "
                 "url must be https://%s/*.jsonl | "
                 "flags: --field F --pattern P --any --links --values"
                 % (target, AB2_HOST))
        return True

    url, field, pattern, any_mode, links, values = _parse_args(rest)
    if not url or not _ab2_url_ok(url):
        send_raw("PRIVMSG %s :ab2filter: only https://%s/*.jsonl URLs are allowed."
                 % (target, AB2_HOST))
        return True
    if links and values:
        send_raw("PRIVMSG %s :ab2filter: --links and --values are mutually exclusive." % target)
        return True
    # With --links the field is unnecessary: a lone second positional is the
    # pattern, and without a field we scan the raw line.
    if links and pattern is None and field is not None:
        pattern, field = field, None
    if links and not field:
        any_mode = True
    if values and any_mode:
        send_raw("PRIVMSG %s :ab2filter: --values needs a field, not --any." % target)
        return True
    if not pattern:
        send_raw("PRIVMSG %s :ab2filter: a pattern is required. usage: "
                 "!ab2filter <url> <field> <pattern>" % target)
        return True
    if not any_mode and not links and not field:
        send_raw("PRIVMSG %s :ab2filter: a field is required (or use --any). usage: "
                 "!ab2filter <url> <field> <pattern>" % target)
        return True

    nowm = time.monotonic()
    with _lock:
        if _busy:
            send_raw("PRIVMSG %s :ab2filter: one job at a time, %s — try again shortly."
                     % (target, nick))
            return True
        if nowm - _last_run < COOLDOWN:
            send_raw("PRIVMSG %s :ab2filter: cooldown, %ds to go."
                     % (target, int(COOLDOWN - (nowm - _last_run)) + 1))
            return True
        _busy = True
        _last_run = nowm

    mode = "links" if links else "values" if values else "lines"
    scope = "any line" if any_mode else field
    send_raw("PRIVMSG %s :ab2filter: on it — %s, mode=%s matching %r, streaming (no full download)."
             % (target, scope, mode, pattern))
    t = threading.Thread(
        target=_run_job,
        args=(target, url, field, pattern, any_mode, mode, send_raw, emit),
        daemon=True)
    t.start()
    return True
