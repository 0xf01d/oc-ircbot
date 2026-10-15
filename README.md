# oc-ircbot

A re-creation of [vjt/claude-ircbot](https://github.com/vjt/claude-ircbot)
where the brain is a **long-running OpenCode agent** — `opencode serve` runs
once and every IRC channel owns one warm, persistent session — and the whole
stack runs in **Docker**, on **Kubernetes**, or as plain **systemd** user
units.

The bridge is a near-verbatim port of the original: same events, same FIFO
grammar, same trust model, same DCC. Round 1 swapped Claude Code's Monitor
for a per-reply `opencode run`; round 2 replaced that with the real thing:
persistent sessions, streaming replies, an agent guard, disk-backed memory
and a permission gate. The bridge itself never changed.

```
              ┌──────────────────────── shared /data ────────────────────────┐
              │ events.<net>.log   bot.send.<net>   workspace/   agent.status│
              └───▲──────────────────────▲──────────────────▲───────────────┘
                  │ events               │ FIFO verbs       │ heartbeat/status
   IRC net A ── bot.py ──────────▶ brain.py ────────────────▶ watchdog.py
   IRC net B ── bot.py            │ opencode serve (1×)      3 layers: process /
   (one process per network)      │ 1 warm session per       server / agent
                                  │ (network, channel)
```

## Architecture

**One `opencode serve` process for the whole stack** (`brain.py` spawns it in
its workspace, or attaches to a running one via `BRAIN_SERVE_URL`):

- **One session per (network, channel).** The session pointer persists in
  `workspace/sessions.json`; a brain restart re-attaches to the warm session
  and re-counts its turns from the server's history.
- **Non-blocking submit.** New events are batched per channel and submitted
  with `POST /session/{id}/prompt_async` (HTTP 204) — never a cold process
  per reply.
- **Streaming replies.** An SSE thread on `GET /event` wakes on
  `session.idle`, fetches the last assistant message, parses FIFO verbs and
  writes them to the right network's FIFO.
- **Cancellation.** A turn stuck beyond `BRAIN_TURN_TIMEOUT` is cancelled
  with `POST /session/{id}/abort`; liveness is polled via
  `GET /session/status`.

**Measured latency** (real `opencode serve` v1.19.0, 2026-10-15): a warm
session answers in **~1.0 s** (0.94–1.00 s across three turns); the first
turn on a fresh server took 2.44 s including the provider handshake. The
round-1 per-reply `opencode run` measured 6–9 s, cold, every time.

**The agent guard** (vjt's `aup_watchdog` triggers, ported into the loop):
a refusal-shaped reply, a session over `BRAIN_MAX_TURNS` (150) assistant
turns, or a wedged turn each reset the session. Every new session is
**rehydrated from disk** — `memory/MEMORY.md` plus today's activity log are
injected with an explicit "do not send anything to IRC right now" order — so
memory survives resets and compaction (`memory_log.py`, 14-day activity log
+ archive).

**The permission gate** (vjt's `gate-permission.py`, headless): the shipped
`opencode.json` allows reads, allows edits only under `memory/**`, and denies
`bash`/`webfetch`/`task`; a permission request that still arrives is
**rejected on sight** (`POST /permission/{id}/reply`) and announced to the
admin over the bot FIFO — an interactive prompt would dead-lock a headless
agent.

**Multi-network.** `bot.py` is env-parameterised, so one process per IRC
network shares one brain: `IRC_NETWORKS=azzurra,libera` (plus
`networks.json` per-label overrides) makes the brain tail
`events.<net>.log` and write `bot.send.<net>` per network. Hosts run the
`oc-ircbot-bot@.service` template unit; containers run `nets.py`, a small
supervisor that spawns one bot per network and cleans up with a real `QUIT`.

**The Monitor-equivalent.** `opencode serve` has no mid-turn notification
API, but `prompt_async` is accepted while a turn streams (verified: the
prompt was folded into the in-flight turn's reply) and the brain drains any
still-pending batch the moment a turn goes idle. Rationale and the honest
differences: `docs/monitor.md`.

**Watchdog v2.** Three layers — heartbeat mtime (process), `serve` health +
wedged-session abort (server), and the brain's own `agent.status` counters
(agent) — with escalation via `WATCHDOG_RESTART_CMD` or exit-nonzero for the
platform's restart policy.

## Sidecars

The original ran a small zoo of sidecars next to the bridge, and the pattern
they all follow is supported here on purpose: **a sidecar tails `bot.log`
(and/or `events.log`) and writes `VERB <args>` lines into the FIFO, with an
origin tag** — `SAY:stats #chan text` — which changes nothing on the wire but
stamps `bot.log` so deterministic sidecars are distinguishable from the LLM's
lines. Five sidecars can share one FIFO because of that tag.

The brain may spawn such helpers **inside its own container** (they only need
the shared volume): the shipped example is `watchdog.py`. The counters and
game bots of the original (`roll_counter.py`, `firma_counter.py`, stats,
`impiccato`, …) are deliberately not ported — they are programs that happen
to speak IRC, not parts of the bridge — but this is exactly the seam they
would plug into. A sidecar needs no changes to `bot.py` or `brain.py`, ever.

## The reply policy

The model's reply must be zero or more FIFO command lines, one per line:

```
SAY <target> <text>
ACT <target> <text>
NOTICE <nick> <text>
JOIN <chan> | PART <chan> | WHOIS <nick> | QUIT [reason]
RAW <irc line>
```

Everything else in the answer is prose and is ignored. Bodies are stripped
of ANSI/control characters, so a reply can never forge a second FIFO line
inside a message. Silence is the default (`policy.build_system`): the prompt
is an opportunity, not an obligation. The system prompt also carries the
identity-discipline rule (never claim to be a human; refuse spoofed-identity
games) and, optionally, per-channel registers
(`BRAIN_REGISTERS='{"#chan":"register text"}'` or `registers.json`).

Send a command by hand exactly as the original documented:

```bash
printf 'SAY #mychannel hello everyone\n' > bot.send
./bot.say '#mychannel' <<'EOF'   # body on stdin: no quoting footgun
100% d'accordo — accents and apostrophes survive
EOF
```

## Running it

```bash
export OPENCODE_API_KEY=oc_sk_...          # never committed
docker compose up --build
```

| service | image base | role |
|---|---|---|
| `bot` | `python:3.12-slim` | the IRC bridge (`bot.py`), TLS by default |
| `brain` | `node:22-slim` + OpenCode CLI | persistent-session agent (`brain.py`) |
| `watchdog` | `python:3.12-slim` | three-layer health sidecar (`watchdog.py`) |

The `OPENCODE_API_KEY` is written by the brain into
`~/.local/share/opencode/auth.json` (the CLI's own format) *before* it spawns
`opencode serve`. The serve workspace lives on the shared volume
(`/data/workspace`): `opencode.json` (the permission gate) is installed there
at startup, and `memory/` is the agent's persistent memory.

Several networks at once: either set `IRC_NETWORKS` + `networks.json` and run
`nets.py` as the bot container's command, or (host install) enable
`oc-ircbot-bot@azzurra`, `oc-ircbot-bot@libera`, … from the template unit —
each with its own `.env.<net>` and its own trust file. Trust does not travel
between networks.

### systemd (Docker-less host)

```bash
mkdir -p ~/.config/systemd/user && cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now oc-ircbot-brain.service oc-ircbot-bot@azzurra.service
loginctl enable-linger "$USER"   # survive logout, start at boot
```

The bot units restart with a growing backoff (30 s, ×1.5 steps, capped at
30 min). `ExecStop` writes `QUIT :bye` through the FIFO first, so
`systemctl --user stop` is a clean part instead of a ping timeout.

### Offline smoke (no API key, no network)

```bash
# without Docker:
sh tests/smoke_local.sh

# with Docker (same seam CI uses):
docker compose -f docker-compose.yml -f docker-compose.smoke.yml \
    up --abort-on-container-exit --exit-code-from mock-irc
```

Both run the **real brain pipeline** — tail → `prompt_async` → SSE → idle →
FIFO — against a **stub serve** (`tests/stub_opencode_serve.py`, the same
v1 API), a mock IRC server, and the real `bot.py`. The mock injects a channel
message, the brain answers `SAY #test stub-ok`, the bot sends the PRIVMSG,
and the mock exits 0 the moment it records it. Only the model is stubbed.

## Configuration

Bot variables (unchanged from round 1 — the bridge is the same program):

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
| `BOT_TRUST` | `bot.trust` | trust list (per network!) |
| `BOT_RESERVED` / `BOT_RESERVED_STORE` | `bot.reserved` / `reserved_recent.tsv` | leak gate (see `leak_guard.py`) |
| `BOT_ENV` | `.env` | NickServ password file |
| `BOT_STARTUP` | `bot.startup` | post-auth command replay |
| `NICKSERV_PASS` | *(empty)* | NickServ identify |

Brain variables:

| Variable | Default | What |
|---|---|---|
| `BRAIN_MODEL` | `opencode/deepseek-v4.1-flash-free` | model for new sessions |
| `BRAIN_SERVE_URL` | *(empty)* | attach to a running serve (else spawn one) |
| `BRAIN_SERVE_PORT` / `BRAIN_SERVE_HOST` | `46099` / `127.0.0.1` | spawn port; host `0.0.0.0` lets a sidecar poll it |
| `BRAIN_OPENCODE_BIN` | `opencode` | server binary |
| `BRAIN_WORKDIR` | `/data/workspace` | serve cwd: `opencode.json`, sessions.json, memory |
| `BRAIN_MEMORY_DIR` | `$WORKDIR/memory` | agent memory (index + activity log) |
| `BRAIN_AGENT_STATUS` | `/data/agent.status` | JSON snapshot for the watchdog |
| `IRC_NETWORKS` | *(empty)* | comma list of network labels (multi-network) |
| `BRAIN_ADMIN` | `admin` | NOTICE target for permission-gate announcements |
| `BRAIN_MUTE` | *(empty)* | CSV of muted channels (chan_filter.awk port) |
| `BRAIN_ADDRESS_ONLY` | `0` | wake only when addressed |
| `BRAIN_REGISTERS` / `registers.json` | *(empty)* | per-channel register text |
| `BRAIN_REFUSAL_PATTERNS` | built-in AUP set | JSON list of refusal regexes |
| `BRAIN_MAX_TURNS` | `150` | assistant turns before a session reset |
| `BRAIN_TURN_TIMEOUT` | `180` | seconds before a busy turn is aborted |
| `BRAIN_POLL` / `BRAIN_BATCH_WINDOW` / `BRAIN_BATCH_MAX` | `1.0` / `3.0` / `5` | batching |
| `BRAIN_RING` / `BRAIN_MAX_CHANS` | `8` / `64` | bounded per-channel context |
| `BRAIN_RATE` / `BRAIN_GLOBAL_RATE` | `3.0` / `1.0` | send rate limits (s) |
| `BRAIN_FROM_START` | `0` | read event logs from the top on start |
| `BRAIN_DRY_RUN` / `brain.py --dry-run` | `0` | print the prompt instead of submitting |

Watchdog variables: `WATCHDOG_POLL` (`10`), `WATCHDOG_STALL` (`30`),
`WATCHDOG_MAX_STALLS` (`3`), `WATCHDOG_WEDGED` (`300`),
`WATCHDOG_SERVE_MAX_FAILS` (`3`), `WATCHDOG_RESTART_CMD`,
`WATCHDOG_EXIT_ON_STALL`, `BRAIN_SERVE_URL`.

Nets supervisor (containers): `NETS_RESTART_SEC` (`30`),
`NETS_RESTART_MAX` (`1800`), `NETS_QUIT_GRACE` (`10`).

## Kubernetes

`k8s/` expresses the same topology for the `hermes-generated-content`
namespace: the code is a ConfigMap mounted at `/code`, the bridge pod holds
**brain (main) + stub-serve + watchdog + bot** sharing an `emptyDir`, and a
mock IRC Deployment+Service drives the smoke test.

```bash
kubectl apply -f k8s/00-configmap.yaml
kubectl apply -f k8s/20-mock-irc.yaml
kubectl apply -f k8s/10-oc-ircbot.yaml
```

`k8s/README.md` explains why `emptyDir` (pod-scoped) forces the containers
into one pod here, how to regenerate the ConfigMap after code changes, and
ships `k8s/30-split-deployments.yaml` for the PVC-backed split to use on a
real cluster.

## Testing & CI

`.github/workflows/ci.yml` runs on every push:

1. unit tests (`pytest`, 72 of them) — including a full brain round trip per
   test against the stub SSE server, the policy layer, multi-network
   derivation, memory archiving and the three-layer watchdog;
2. the Docker-less end-to-end smoke (`tests/smoke_local.sh`);
3. `docker compose build` for the images + the containerised end-to-end
   smoke (mock IRC + real bot + real brain + stub serve), asserting the mock
   received the PRIVMSG.

Run the units locally with `python -m pytest tests -q`.

## Differences from vjt/claude-ircbot

- **The brain.** Claude Code's Monitor is replaced by `brain.py` driving
  `opencode serve` with persistent sessions (`docs/monitor.md` records what
  is honestly different). This is the whole point of the project.
- **The agent guard.** `aup_watchdog.py` is ported *into* the brain as the
  turn-cap / refusal / wedge resets; the watchdog sidecar gains the serve and
  agent layers.
- **The permission gate.** `gate-permission.py`'s deny-fast rationale is
  re-implemented headlessly: `opencode.json` + auto-reject + admin NOTICE.
- **chan_filter.awk** returns as `BRAIN_MUTE`.
- **Multi-network** is first-class: per-network bot processes, FIFOs and
  brain sessions (`networks.py`, `nets.py`, `bot.trust.libera`,
  `oc-ircbot-bot@.service`).
- **Packaging.** Dockerfiles + compose + `k8s/` alongside the original's
  systemd approach (both shipped; the bot units grow a template and a clean
  QUIT on stop).
- **One sidecar, not the zoo.** Only the watchdog was ported. The counters
  and game bots (`roll_counter`, `firma_counter`, stats, impiccato, …) are
  *deliberately not* ported — out of scope for this project by design.
- **`leak_guard.py`** is re-implemented: the `.reference/` snapshot does not
  include the original module; the API and file names match what `bot.py`
  expects.
- **A plaintext escape hatch** (`IRC_TLS=0`) for the offline mock-server
  smoke test; default remains TLS.
- The original `docs/img/*.jpg` hero/illustration images are not
  redistributed.

## License

MIT (see `LICENSE`). Original claude-ircbot © vjt; this re-creation © 0xf01d.
