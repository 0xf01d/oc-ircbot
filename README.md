# oc-ircbot

A faithful re-creation of [vjt/claude-ircbot](https://github.com/vjt/claude-ircbot)
where the brain is the **OpenCode CLI** running the free model
`opencode/deepseek-v4.1-flash-free`, and the whole stack runs in **Docker**
(bot + brain + watchdog wired by compose) and on **Kubernetes**.

The original is a minimal, stdlib-only Python IRC bridge that lets a Claude
Code session sit in an IRC channel as a real user. This project keeps the
bridge **exactly as it was** (same events, same FIFO grammar, same trust
model) and swaps the one agent-specific part: Claude Code's *Monitor* tool
becomes a small supervisor (`brain.py`) that tails the bot's event stream and
shells out to `opencode`. Nothing else about the transport changed.

```
        ┌─────────────────────────── shared volume /data ───────────────────────────┐
        │  bot.log        bot.send (FIFO)        events.log        brain.heartbeat    │
        └───▲───────────────────▲──────────────────────▲──────────────────▲──────────┘
            │ raw IRC           │ SAY …                │ events           │ mtime
    ┌───────┴────────┐  ┌───────┴────────┐   ┌──────────┴─────────┐  ┌─────┴────────┐
    │      bot       │  │     brain      │   │   brain (tailer)   │  │  watchdog    │
    │  (bot.py)      │  │  (brain.py)    │   │  per-channel ring  │  │(watchdog.py) │
    │                │  │                │   │  → opencode run    │  │ stall report │
    └───────┬────────┘  └────────────────┘   └────────────────────┘  └──────────────┘
            │ TLS / TCP
            ▼
       IRC network  (or the mock server in tests)
```

## The Monitor → brain substitution

In the original, Claude Code attached its **Monitor** tool to `bot.py`'s
stdout: each structured line (`MSG`, `INVITE`, `CTCP`, `NOTICE`, `KICK`,
`IDLE`, …) became a notification mid-conversation, and the agent wrote
`SAY #chan text` back into the `bot.send` FIFO.

`bot.py` here does the same thing, except every emitted event is **also
appended to `BOT_EVENTS`** (`events.log`) so a *separate container* can tail
it. `brain.py` is that container:

1. tails `events.log`,
2. batches new events and keeps a small **per-channel ring buffer** so replies
   have context,
3. builds a prompt and runs
   `opencode run --auto --model opencode/deepseek-v4.1-flash-free "<prompt>"`,
4. parses the answer for `SAY <target> <text>` lines and writes them into the
   `bot.send` FIFO,
5. ignores its own nick, rate-limits, dedupes so it never echoes a loop, and
   touches `brain.heartbeat` every loop.

`bot.py` still knows **nothing** about the brain — it is the same generic
bridge it always was.

### The brain contract

The model answers with zero or more lines of exactly this shape, one PRIVMSG
each:

```
SAY #channel hello world
```

Anything that is not a `SAY` line is ignored; a reply that does not speak is
allowed (the prompt is an opportunity, not an obligation). The channel is
taken verbatim from the event the model is answering.

## Running it

```bash
export OPENCODE_API_KEY=oc_sk_...          # never committed
docker compose up --build
```

Three services, one network, one shared volume:

| service | image base | role |
|---|---|---|
| `bot` | `python:3.12-slim` | the IRC bridge (`bot.py`), TLS by default |
| `brain` | `node:22-slim` + OpenCode CLI | the Monitor stand-in (`brain.py`) |
| `watchdog` | `python:3.12-slim` | heartbeat stall reporter (`watchdog.py`) |

A `Dockerfile.*` per image and a `docker-compose.yml` with `depends_on`,
`restart: unless-stopped` and a `botdata` volume. Point it at a real network
with the `IRC_*` variables; the default is Azzurra (`irc.azzurra.chat:6697`).

The `OPENCODE_API_KEY` is written by the brain container into
`~/.local/share/opencode/auth.json` as
`{"opencode":{"type":"api","key":"<key>"}}` — the CLI's own auth format.

### Offline smoke (no API key, no network)

```bash
docker compose -f docker-compose.yml -f docker-compose.smoke.yml \
    up --abort-on-container-exit --exit-code-from mock-irc
```

That starts a **mock IRC server** (`tests/mock_irc_server.py`), points the
real `bot` at it, and runs the brain with `OC_BRAIN_CMD` set to the
**stub brain** (`tests/stub_brain.py`). The mock injects a channel message,
the brain answers `SAY #test stub-ok`, the bot sends the PRIVMSG, and the mock
exits 0 the moment it records it. That exit code is the end-to-end assertion.

## Configuration

Everything the original exposed is preserved, plus the brain's own knobs.
The bot's variables (all env-overridable, defaults reproduce the original):

| Variable | Default | What |
|---|---|---|
| `IRC_HOST` / `IRC_PORT` | `irc.azzurra.chat` / `6697` | TLS endpoint |
| `IRC_NICK` / `IRC_IDENT` / `IRC_REAL` | `vjt-claude` / `claude` / repo URL | registration |
| `IRC_BIND` | *(empty)* | source address to bind |
| `IRC_TLS_VERIFY` | `1` | cert verification (set `0` for broken-cert nets) |
| `IRC_TLS` | `1` | `0` = plaintext (offline smoke test only) |
| `BOT_LOG` | `bot.log` | raw traffic log, both directions |
| `BOT_FIFO` | `bot.send` | command inbox |
| `BOT_EVENTS` | `/data/events.log` | structured event stream for the brain |
| `BOT_TRUST` | `bot.trust` | trust list |
| `BOT_RESERVED` / `BOT_RESERVED_STORE` | `bot.reserved` / `reserved_recent.tsv` | leak gate (see `leak_guard.py`) |
| `BOT_ENV` | `.env` | NickServ password file |
| `BOT_STARTUP` | `bot.startup` | post-auth command replay |
| `NICKSERV_PASS` | *(empty)* | NickServ identify |

Brain variables:

| Variable | Default | What |
|---|---|---|
| `BRAIN_MODEL` | `opencode/deepseek-v4.1-flash-free` | model passed to `--model` |
| `OC_BRAIN_CMD` | *(empty)* | stub command (prompt on stdin) — used by tests |
| `BRAIN_SYSTEM` | built-in | system prompt |
| `BRAIN_POLL` / `BRAIN_BATCH_WINDOW` / `BRAIN_BATCH_MAX` | `1.0` / `3.0` / `5` | batching |
| `BRAIN_RING` / `BRAIN_MAX_CHANS` | `8` / `32` | bounded per-channel context |
| `BRAIN_TIMEOUT` | `120` | hard timeout on every `opencode` call |
| `BRAIN_RATE` / `BRAIN_GLOBAL_RATE` | `3.0` / `1.0` | send rate limits (s) |
| `BRAIN_FROM_START` | `0` | read `events.log` from the top on start |
| `BRAIN_HEARTBEAT` | `/data/brain.heartbeat` | watchdog's input |
| `BRAIN_DRY_RUN` | `0` | print the prompt instead of invoking the model |

Watchdog variables: `WATCHDOG_POLL` (`10`), `WATCHDOG_STALL` (`30`),
`WATCHDOG_MAX_STALLS` (`3`), `WATCHDOG_RESTART_CMD`, `WATCHDOG_EXIT_ON_STALL`.

Send a command by hand exactly as the original documented:

```bash
printf 'SAY #mychannel hello everyone\n' > bot.send
./bot.say '#mychannel' <<'EOF'   # body on stdin: no quoting footgun
100% d'accordo — accents and apostrophes survive
EOF
```

Supported FIFO verbs: `SAY`, `ACT`, `NOTICE`, `JOIN`, `PART`, `WHOIS`,
`QUIT`, `RAW`, `DCC` — unchanged from the original.

## Kubernetes

`k8s/` expresses the same topology for the `hermes-generated-content`
namespace: the code is a ConfigMap mounted at `/code`, the bridge pod holds
**brain (main) + watchdog (sidecar) + bot** sharing an `emptyDir`, and a mock
IRC Deployment+Service drives the smoke test.

```bash
kubectl apply -f k8s/00-configmap.yaml
kubectl apply -f k8s/20-mock-irc.yaml
kubectl apply -f k8s/10-oc-ircbot.yaml
```

`k8s/README.md` explains why `emptyDir` (pod-scoped) forces bot + brain into
one pod here, and ships `k8s/30-split-deployments.yaml` for the PVC-backed
split (`bot` Deployment + `brain`/`watchdog` Deployment) to use on a real
cluster.

### Verified on the cluster

Both modes were exercised on the k3s cluster in this namespace, with the real
bot code, via `kubectl apply` + `kubectl logs` (the dev box has no Docker
daemon, so nothing below was run in Docker):

*Stub round trip* — the mock recorded the brain's PRIVMSG and exited 0:

```
### mock
< NICK ocbot
< USER ocbot 0 * :github.com/0xf01d/oc-ircbot
< PRIVMSG #test :stub-ok
>> recorded: #test stub-ok
EXPECTED PRIVMSG SEEN
### brain
[19:51:52] BRAIN START events=/data/events.log fifo=/data/bot.send model=opencode/deepseek-v4.1-flash-free live
[19:51:54] BRAIN SENT 1 #test
```

*Live mode* — the real OpenCode CLI (`node:22-slim` + `npm i -g opencode-ai`)
answered a real message inside the pod with `deepseek-v4.1-flash-free`:

```
### mock
< PRIVMSG #test :hi alice, i'm a bot hanging out in this channel
>> recorded: #test hi alice, i'm a bot hanging out in this channel
### brain
[19:53:46] BRAIN START … model=opencode/deepseek-v4.1-flash-free live
[19:53:55] BRAIN SENT 1 #test
```

## Testing & CI

`.github/workflows/ci.yml` runs on every push:

1. unit tests (`pytest`) for the prompt builder, event parser, `SAY` parser,
   ring buffer, FIFO writer, trust/leak logic and watchdog;
2. `docker compose build` for all three images;
3. the real end-to-end smoke (mock IRC + real bot + stub brain) and asserts
   the mock received the PRIVMSG.

Run the units locally with `python -m pytest tests -q`.

## Differences from vjt/claude-ircbot

The bridge itself is a **near-verbatim port** — a reviewer diffing `bot.py`
against `.reference/bot_vjt.py` sees the same program. What is genuinely new:

- **The brain.** Claude Code's Monitor is replaced by `brain.py` talking to the
  OpenCode CLI. This is the whole point of the project.
- **Packaging.** Dockerfiles + `docker-compose.yml` + `k8s/` replace the
  original's `systemd/` user units (systemd equivalents are still shipped in
  `systemd/` for a Docker-less host).
- **One sidecar, not the zoo.** Only the watchdog was ported. `aup_watchdog.py`,
  `roll_counter.py`, `stats.py`, `firma_counter.py` and the game bots are
  *deliberately not* ported — they are bookkeeping/games that happen to speak
  IRC, not part of the bridge. The README says so plainly instead of shipping
  half-ports.
- **`leak_guard.py`** is re-implemented: the `.reference/` snapshot does not
  include the original module, so this is an equivalent stdlib contract
  (reserved targets, verbatim-run detection) rather than a copy.
- **A plaintext escape hatch** (`IRC_TLS=0`) for the offline mock-server smoke
  test; default remains TLS.
- **No agent-side Claude Code bits**: `.claude/hooks/gate-permission.py` and
  the permission gate are Claude-Code features and have no OpenCode analogue
  here; the trust model in `bot.py` is unchanged.
- The original `docs/img/*.jpg` hero/illustration images are not redistributed.

## License

MIT (see `LICENSE`). Original claude-ircbot © vjt; this re-creation © 0xf01d.
