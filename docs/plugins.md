# ocbot plugins and webhooks

Plugins are plain Python files on the PVC (`/data`). They hot-reload on
mtime, they are optional (every hook is optional, any subset works), and a
broken file or a raising hook is logged and ignored — it can never take the
bot or the brain down. **Plugins live on the PVC; `/app` is read-only and
never patched.**

Both processes load their plugin with the same loader, `plug.py`:

| process | file (env) | default |
|---|---|---|
| brain.py | `BRAIN_PLUGIN` | `/data/brain_plugin.py` |
| bot.py | `BOT_PLUGIN` | `/data/bot_plugin.py` |

Drop a file at the path, or edit it in place: the next call re-reads it
(one stat per call, re-exec only when the mtime moved). Deleting the file
keeps the last good copy loaded. A file that fails to import is reported
(`PLUGIN_IMPORT_ERROR`) and the previous copy keeps serving; a hook that
raises is reported (`PLUGIN_ERROR <hook> <repr>`) and behaves as a no-op.

## Brain hooks (`/data/brain_plugin.py`)

Loaded by `brain.py`. Call order inside `feed()`:

```
drop -> rewrite -> mute / own-echo / address gates -> !stats -> handle -> batch
```

`!stats` is checked before `handle`, so the built-in always wins.

| hook | signature | semantics |
|---|---|---|
| `drop` | `drop(ev, net) -> bool` | `True` = the event is never batched, not even when addressed |
| `rewrite` | `rewrite(ev, net) -> dict \| None` | return the replacement event (may mutate in place); `None` = drop |
| `handle` | `handle(ev, net, ctx) -> bool` | `True` = handled without a model turn |
| `outbound` | `outbound(net, verb, target, text) -> str \| None` | `None` = block the line (logged `OUTBOUND_BLOCKED`), a `str` replaces the text; applied just before the FIFO write, for `SAY`/`ACT`/`NOTICE` only — never `RAW`, never `DCC` |
| `event` | `event(name, payload)` | fire-and-forget lifecycle notification |

`ev` is the parsed event dict (`kind`, `from`, `to`, `body`, `trust`, ...);
`net` is the network label.

`ctx` (built fresh per call; it never imports `bot.py`):

| member | meaning |
|---|---|
| `ctx.say(target, text)` | `SAY` over that net's FIFO |
| `ctx.notice(target, text)` | `NOTICE` over that net's FIFO |
| `ctx.send(raw)` | one raw FIFO verb line, e.g. `"JOIN #chan"` |
| `ctx.emit(*parts)` | one line into the brain's log |
| `ctx.net`, `ctx.nick` | the event's network label and the nick the brain uses there |

### Minimal `handle` example: an `!ignore` command

```python
IGNORED = set()

def handle(ev, net, ctx):
    body = (ev.get("body") or "").strip()
    if not body.startswith("!ignore"):
        return False                      # not ours: fall through to the batch
    parts = body.split()
    if len(parts) == 2:
        IGNORED.add(parts[1].lower())
        ctx.notice(ev.get("to"), f"ignoring {parts[1]}")
    elif body == "!ignore list":
        ctx.notice(ev.get("to"), ", ".join(sorted(IGNORED)) or "nobody")
    return True
```

### Minimal `outbound` example: a redaction filter

```python
WORDS = ("hunter2", "s3cr3t")

def outbound(net, verb, target, text):
    for w in WORDS:
        if w in text:
            return text.replace(w, "[redacted]")
    # return None here would BLOCK the line — only return None to censor
    # the whole message, not to pass it through
```

### `event` names

`start`, `serve_up`, `connect`, `reset`, `turn_start`, `turn_end`, `error`.
`payload` is a dict, keys as applicable: `net`, `channel`, `nick`, `reason`,
`turns`, `seconds`. The hook runs inline (no threads are spawned for it) and
its exceptions are swallowed like any other hook's.

## Bot hook (`/data/bot_plugin.py`)

Loaded by `bot.py`. One hook, applied in `process_cmd` immediately after the
leak gate (`outbound_ok`) and before anything reaches the wire — so the leak
guard always runs first, and the plugin sees only lines the guard passed:

| hook | signature | semantics |
|---|---|---|
| `outbound` | `outbound(target, text, verb) -> str \| None` | `None` = block (logged `BOT_PLUGIN_BLOCKED`), a `str` replaces the text; only for the `SAY`/`ACT`/`NOTICE` verbs |

## Inbound webhooks

`bot.py` starts one HTTP listener (stdlib `http.server`, `ThreadingHTTPServer`
with daemon threads) when enabled. Config: `/data/webhooks.json`
(env `BOT_WEBHOOKS_FILE`), hot-reloaded on mtime — the `listen` address is
read once at bot start, changing it needs a restart:

```json
{"listen": "0.0.0.0:8088",
 "hooks": {"alert": {"token": "…", "net": "freebnc", "target": "#hermes",
                     "from": "webhook", "trust": "TRUSTED"}}}
```

- `POST /hook/<name>` — requires header `X-Webhook-Token` equal to the
  hook's token (constant-time compare). Unknown hook or bad/missing token:
  flat `403`, no body echoed. Body: JSON `{"text": "…"}` or raw text/plain;
  newlines are collapsed to spaces and the text is capped at 4000 bytes
  (never splitting a utf-8 character).
- Delivery: one synthetic line in that net's events file, in exactly the
  format the bot writes for real messages, so the brain tails it like any
  other event: `[HH:MM:SS] MSG TRUSTED FROM=<from> HOST=webhook TO=<target>
  BODY=<text>`.
- `GET /healthz` — `200 ok`. Nothing else is routed.
- Off switches: `listen` absent from the config, or `BOT_WEBHOOK=0`.
- The listener binds from config only (no hardcoded port) and can never take
  the IRC bot down: every exception is caught and logged, a bind failure
  just leaves webhooks off. With several bot processes (one per network) the
  first to start owns the port; delivery routes by the hook's `net` field
  regardless of which process holds it.

### Posting to a hook

```sh
curl -s -X POST http://host:8088/hook/alert \
     -H 'X-Webhook-Token: …' \
     -H 'Content-Type: application/json' \
     -d '{"text": "disk almost full"}'
```

The brain then receives the message as a trusted `MSG` from `webhook` and
may answer in channel like it would to any other event.
