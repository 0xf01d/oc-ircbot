#!/usr/bin/env python3
"""irc_commands.py — persistent IRC commands for oc-ircbot.

Loaded (and hot-reloaded on mtime change) by bot.py from
BOT_IRC_COMMANDS, default /data/irc_commands.py. Lives on the PVC, so it
survives bot-container restarts as long as bot.py has the small dispatch
hook.

Entry point: handle(target, nick, body, trusted, send_raw, emit, split_say)
  returns True when it handled the command.
"""
import json
import os
import random
import re
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


# --- !ciclo: where is tom in his cycle? (katia, 2026-10-31) -----------------
# Reads the network's own bot log and counts the MODE/KICK lines whose source
# is tom; the running total picks the phase. Cheap, read-only, no state.
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
_rx_tom_action = re.compile(r":tom!\S+ (MODE|KICK)\b")
_rx_tom_dest = re.compile(r"\bKICK \S+ tom\b")


def _handle_ciclo(target, send_raw):
    nowm = time.monotonic()
    with _ciclo_lock:
        if nowm - _ciclo_last.get(target, 0.0) < CICLO_COOLDOWN:
            return True
        _ciclo_last[target] = nowm
    path = os.environ.get("BOT_LOG", "/data/bot.ircnet.log")
    n_mode = n_kick = n_kicked = 0
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                if ":tom!" not in line:
                    continue
                m = _rx_tom_action.search(line)
                if m:
                    if m.group(1) == "MODE":
                        n_mode += 1
                    else:
                        n_kick += 1
                elif _rx_tom_dest.search(line):
                    n_kicked += 1
    except OSError as e:
        send_raw("PRIVMSG %s :ciclo: non riesco a leggere il log (%s)"
                 % (target, e))
        return True
    total = n_mode + n_kick + n_kicked
    phase = CICLO_PHASES[total % len(CICLO_PHASES)]
    send_raw("PRIVMSG %s :tom: %s (mode=%d kick=%d subiti=%d, tot=%d)"
             % (target, phase, n_mode, n_kick, n_kicked, total))
    return True


# --- !cc: realistic-looking but non-chargeable card + balance --------------
# Only in #cybernet, from a katia request (2026-10-31). Five seconds after the
# trigger it emits ONE synthetic card, formatted like an account panel: holder,
# brand, PAN, expiry, CVV and a balance between 50 and 940 EUR. The PAN is a
# random Luhn-valid number built inside the reserved TEST BIN ranges, so it
# looks real but cannot be charged and matches no real account.
CC_CHANNEL = "#cybernet"
CC_DELAY = 5.0
CC_COOLDOWN = 15.0
# (brand, BIN prefix, total digits) — prefixes come from published test cards
CC_BINS = (
    ("Visa", "424242", 16),
    ("Visa", "400000", 16),
    ("Mastercard", "555555", 16),
    ("Mastercard", "510510", 16),
    ("Amex", "378282", 15),
    ("Amex", "371449", 15),
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
    balance = random.uniform(50, 940)
    return ("💳 %s · %s %s · scad. %02d/%02d · CVV %s · saldo €%.2f"
            % (holder, brand, pan, mm, yy, cvv, balance))


def _handle_cc(target, nick, rest, send_raw):
    if target != CC_CHANNEL:
        return True
    nowm = time.monotonic()
    with _cc_lock:
        if nowm - _cc_last.get(target, 0.0) < CC_COOLDOWN:
            return True
        _cc_last[target] = nowm
    holder = rest.strip()[:40] or ("%s %s" % (random.choice(CC_FIRST),
                                              random.choice(CC_LAST)))

    def fire():
        time.sleep(CC_DELAY)
        send_raw("PRIVMSG %s :%s" % (target, _fake_cc_line(holder)))

    threading.Thread(target=fire, daemon=True).start()
    return True


def handle(target, nick, body, trusted, send_raw, emit, split_say):
    global _busy, _last_run
    text = (body or "").strip()
    if text.lower().split()[0:1] == ["!tom"]:
        if not target.startswith("#"):
            return False
        return _handle_tom(target, send_raw)
    if text.lower().split()[0:1] == ["!aperisesso"]:
        if not target.startswith("#"):
            return False
        return _handle_aperisesso(target, nick, text[len("!aperisesso"):].strip(),
                                  send_raw)
    if text.lower().split()[0:1] == ["!ciclo"]:
        if not target.startswith("#"):
            return False
        return _handle_ciclo(target, send_raw)
    if text.lower().split()[0:1] == ["!cc"]:
        if not target.startswith("#"):
            return False
        return _handle_cc(target, nick, text[len("!cc"):].strip(), send_raw)
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
