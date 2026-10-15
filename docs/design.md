# Design notes

Round 1 ported the vjt/claude-ircbot bridge verbatim and stood a per-reply
`opencode run` behind it. Round 2 replaces that brain with the long-running
agent the design always aimed at; the bridge (`bot.py`) is untouched by both
rounds. This file documents what each layer now guarantees.

## Process topology

```
IRC net A ── bot.py (proc 1) ── events.A.log ─┐                    ┌─ bot.send.A ──▶ net A
                                              ├─▶ brain.py ───────┤
IRC net B ── bot.py (proc 2) ── events.B.log ─┘   │  opencode     └─ bot.send.B ──▶ net B
                                                  │  serve (1×)
watchdog.py ── reads heartbeat / serve health / agent.status
```

One `opencode serve` process for the whole stack; one bot per IRC network
(systemd `oc-ircbot-bot@.service` template, or one container each); one
brain; one watchdog. The brain derives each network's file names from
`IRC_NETWORKS` / `networks.json` (`networks.py`), so bot and brain never need
to agree on anything except those paths.

## Event stream

Unchanged from round 1 (see `bot.py`): `[HH:MM] KIND [fields…]` lines in
`Europe/Rome`, appended to the network's `BOT_EVENTS` file. The brain treats
`MSG`, `INVITE`, `IDLE`, `CTCP`, `KICK`, `NOTICE` as actionable
(`policy.wakes_model`), and — new in round 2 — everything first passes the
mute filter (`BRAIN_MUTE`, the chan_filter.awk port): a muted channel's
events never create a session, never enter a ring, never wake the model,
"neanche se interpellato".

## Sessions: one per (network, channel)

`brain.get_session(net, chan)` finds or creates **the** `opencode` session
for a channel. The live pointer persists in `WORKDIR/sessions.json`, so a
brain restart re-attaches to the warm session instead of forking a new one;
the turn count is re-derived from the server's message history at re-attach.

A session is deliberately thrown away and rebuilt by three guards (the
aup_watchdog triggers, ported into the loop):

| trigger | detection | action |
|---|---|---|
| **refusal** | `policy.refusal_hits` on the assistant text (`policy.DEFAULT_REFUSAL_PATTERNS`) | `reset_session`, log to the activity log |
| **turn cap** | `assistant_turn_count >= MAX_TURNS` (150, in `brain.py`) | `reset_session` — a fresh context, not a zombie |
| **wedged turn** | `session.status` busy/retry longer than `TURN_TIMEOUT` (180 s, in `brain.py`) | `POST /session/{id}/abort`; a second wedge resets the session |

Every new session is **rehydrated from disk**: a `rehydrate_prompt` injects
`memory/MEMORY.md` + today's activity log and orders the agent to stay silent
("Do not send anything to IRC right now"). Memory is the only continuity that
survives resets — which is why it lives in files, not in the session
(`memory_log.py`: index + 14-day activity log + archive; compaction events
are logged to the activity log when they arrive on SSE).

## Reply path

1. `policy.build_prompt(batch, ring, …)` renders recent channel context, the
   new events (`[ADDRESSED]`-marked when the nick is addressed), the channel
   register (`registers.json`) and the verb contract.
2. `prompt_async` submits it (HTTP 204; accepted even while a turn streams —
   see `docs/monitor.md` for why this is the Monitor-equivalent).
3. On SSE `session.idle`, the last assistant message is fetched and parsed by
   `policy.parse_answer` into FIFO verb lines, `filter_answer` drops lines
   addressed to the bot's own nick, a digest set dedupes loops, RATE /
   GLOBAL_RATE throttle chat verbs, and `write_fifo` writes to the network's
   FIFO with a deadline (a bot that is down never wedges the brain).

The FIFO verb grammar is the bot's full set — `SAY`, `ACT`, `NOTICE`, `JOIN`,
`PART`, `WHOIS`, `QUIT`, `RAW` — not just `SAY` (round 1 accepted one verb).
`policy._clean` strips ANSI and control characters from every body, so a
model answer can never forge a second FIFO line inside a message body.

## Permission gate (gate-permission.py port)

`opencode.json` (installed into the serve working directory by
`setup_workdir`) sets the permission model: reads/list/glob allowed, edits
only under `memory/**`, `bash`/`webfetch`/`task`/external directories denied.
When the model still hits a permission prompt, the brain answers it
headlessly: `POST /permission/{id}/reply {"reply":"reject"}` immediately —
an interactive prompt would dead-lock a headless agent — and announces the
denial to the admin over the bot FIFO:

```
NOTICE admin [gate] denied bash (rm -rf) on irc#test
```

## Watchdog (three layers)

`watchdog.py` v2 keeps the round-1 process layer (heartbeat mtime → stall →
escalate) and adds the layers a long-running agent actually needs:

- **server**: `GET /global/health` + `GET /session/status`; consecutive
  failures escalate; a session busy/retry beyond `WATCHDOG_WEDGED` gets an
  abort and an escalation.
- **agent**: `agent.status` (the brain's own JSON snapshot: turns, refusals,
  wedges, per-session state) is logged loudly every poll — the numbers a
  human needs to see after an incident.

Escalation is `WATCHDOG_RESTART_CMD` (e.g.
`systemctl --user restart oc-ircbot-brain`) or `WATCHDOG_EXIT_ON_STALL=1`
(exit non-zero, let the platform's restart policy act). A sidecar cannot
restart its own container in Kubernetes; the restart command is the honest
lever it has.

## Measured, not assumed

Warm-session reply latency was measured against real `opencode serve`
(v1.19.0, 2026-10-15): **~1.0 s warm** (0.94–1.00 s over three turns), 2.44 s
for the first turn on a fresh server (provider handshake included). The
round-1 `opencode run` per reply measured 6–9 s cold every time.
