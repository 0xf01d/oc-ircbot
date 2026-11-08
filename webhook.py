#!/usr/bin/env python3
"""webhook.py — configurable inbound webhooks for the bot.

One listener (ThreadingHTTPServer, daemon threads) serving exactly two
routes: POST /hook/<name> delivers a synthetic MSG event into the named
net's events file — the brain tails it like any other event — and GET
/healthz answers 200 ok. Nothing else is routed.

Config lives on the PVC (BOT_WEBHOOKS_FILE, default /data/webhooks.json)
and is hot-reloaded on mtime (the `listen` address is read once at bot
start; changing it needs a restart):

    {"listen": "0.0.0.0:8088",
     "hooks": {"alert": {"token": "…", "net": "freebnc",
                         "target": "#hermes", "from": "webhook",
                         "trust": "TRUSTED"}}}

The synthetic line mirrors bot.emit()'s MSG format byte for byte so
policy.parse_event sees an ordinary trusted message; the helper is
deliberately not imported — bot.py is a script, not a library.

Auth is a per-hook shared token in the X-Webhook-Token header, compared
constant-time. Unknown hook and bad token are the same flat 403 with no
body echoed: no oracle for probing hook names.

The listener can never take the IRC bot down: every request is handled in
its own daemon thread, every exception is caught and logged, and a bind
failure (port taken, bad address) just leaves webhooks off. When nets.py
runs one bot per network, the first bot process to start owns the
listener; delivery routes by the hook's `net` field regardless of which
process took the port.
"""
import hmac
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import networks
from util import env, env_bool, now

WEBHOOKS_FILE = env("BOT_WEBHOOKS_FILE", "/data/webhooks.json")
MAX_TEXT = 4000          # bytes: one IRC-shaped event line, not a pastebin
MAX_BODY_READ = 65536    # request-body read cap: a flood cannot balloon RAM

_HOOK_PATH = re.compile(r"^/hook/([^/?]+)$")


def clean_text(text):
    """One IRC line: newlines become spaces, hard cap at MAX_TEXT bytes
    without splitting a utf-8 character."""
    text = re.sub(r"\r\n|\r|\n", " ", text or "")
    b = text.encode("utf-8")
    if len(b) > MAX_TEXT:
        text = b[:MAX_TEXT].decode("utf-8", "ignore")
    return text


class Webhooks:
    """Config + delivery: mtime-gated config reads, event-line writes."""

    def __init__(self, config_path=None, log=None, net_events=None):
        self.config_path = config_path or WEBHOOKS_FILE
        self.log = log or (lambda *parts:
                           print(" ".join(str(p) for p in parts), flush=True))
        # label -> events path; injectable so tests never touch /data
        self.net_events = net_events or (
            lambda label: networks.net_for(label).events)
        self._mtime = None
        self._cfg = {}

    def config(self):
        try:
            mt = os.stat(self.config_path).st_mtime_ns
        except OSError:
            self._mtime, self._cfg = None, {}
            return self._cfg              # absent: listener has nothing to do
        if mt == self._mtime:
            return self._cfg
        try:
            with open(self.config_path) as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            self.log("WEBHOOK_CONFIG_ERROR", self.config_path, repr(e))
            self._mtime = mt              # same mtime: don't re-spam the log
            self._cfg = {}
            return self._cfg
        self._mtime = mt
        self._cfg = data if isinstance(data, dict) else {}
        return self._cfg

    def hooks(self):
        h = self.config().get("hooks")
        return h if isinstance(h, dict) else {}

    def deliver(self, hook, text):
        """One synthetic MSG line into the hook's net events file. The line
        format is bot.emit()'s own, verbatim, so the brain parses it like a
        real message — HOST=webhook marks where it came from."""
        text = clean_text(text)
        label = hook.get("net") or "irc"
        line = (f"[{now('%H:%M:%S')}] MSG {hook.get('trust') or 'TRUSTED'} "
                f"FROM={hook.get('from') or 'webhook'} HOST=webhook "
                f"TO={hook.get('target') or ''} BODY={text}")
        with open(self.net_events(label), "a") as f:
            f.write(line + "\n")
        self.log("WEBHOOK_DELIVERED", label, hook.get("target") or "",
                 f"LEN={len(text)}")


def make_handler(wh):
    """The request class over one Webhooks instance; every path is caught —
    a raising hook or a torn-down client must not kill the server thread."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass                          # the bot's own log is the record

        def _reply(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _fail(self, e):
            try:
                wh.log("WEBHOOK_ERROR", repr(e))
                self._reply(500)
            except Exception:
                pass                      # client gone mid-reply: fine

        def do_GET(self):
            try:
                if self.path.split("?", 1)[0] == "/healthz":
                    self._reply(200, b"ok")
                else:
                    self._reply(404)
            except Exception as e:
                self._fail(e)

        def do_POST(self):
            try:
                self._post()
            except Exception as e:
                self._fail(e)

        def _post(self):
            m = _HOOK_PATH.match(self.path.split("?", 1)[0])
            hook = wh.hooks().get(m.group(1)) if m else None
            token = hook.get("token") if isinstance(hook, dict) else None
            given = self.headers.get("X-Webhook-Token")
            # bytes, not str: compare_digest raises TypeError on non-ASCII
            # str, and a weird token is a 403, never a 500.
            if not isinstance(token, str) or given is None \
                    or not hmac.compare_digest(given.encode("utf-8"),
                                               token.encode("utf-8")):
                self._reply(403)          # flat: no name-probing oracle
                return
            wh.deliver(hook, self._text())
            self._reply(200, b"ok")

        def _text(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            body = (self.rfile.read(min(n, MAX_BODY_READ)) if n > 0
                    else b"").decode("utf-8", "replace")
            try:
                data = json.loads(body)
            except ValueError:
                return body               # raw text/plain
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                return data["text"]
            return body

    return Handler


def start(config_path=None, log=None, net_events=None, enabled=None):
    """Bind the listener when enabled (env BOT_WEBHOOK, default on) and the
    config names a listen address. Returns the running ThreadingHTTPServer,
    or None when off. Never raises into the caller: a broken config or a
    taken port is logged and the bot runs on without webhooks."""
    if enabled is None:
        enabled = env_bool("BOT_WEBHOOK", True)
    if not enabled:
        return None
    wh = Webhooks(config_path, log=log, net_events=net_events)
    listen = wh.config().get("listen")
    if not listen or ":" not in str(listen):
        wh.log("WEBHOOK_OFF", wh.config_path)
        return None
    host, _, port = str(listen).rpartition(":")
    try:
        httpd = ThreadingHTTPServer((host or "127.0.0.1", int(port)),
                                    make_handler(wh))
    except (OSError, ValueError) as e:
        wh.log("WEBHOOK_BIND_ERROR", str(listen), repr(e))
        return None
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="webhooks").start()
    wh.log("WEBHOOK_UP", str(listen), f"hooks={len(wh.hooks())}")
    return httpd
