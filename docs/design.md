# Design notes

Everything here is inherited from vjt/claude-ircbot; it documents the
behaviour `bot.py` preserves so the brain swap stays honest.

## Event stream

`bot.py` prints one line per interesting IRC event to stdout **and** appends
it to `BOT_EVENTS`. Each line is `[HH:MM] KIND [fields…]` with the timestamp
in `Europe/Rome`. Kinds:

| kind | meaning |
|---|---|
| `CONNECTED` / `TLS_OK` / `TCP_OK` | registered / transport up |
| `MSG` | channel or DM message, with `FROM=`, `HOST=`, `TO=`, `BODY=` and a `TRUSTED`/`UNTRUSTED` tag |
| `INVITE` / `CTCP` / `NOTICE` / `JOIN` / `PART` / `QUIT` / `NICK_CHANGE` | the obvious events |
| `KICK` / `KICK_REJOIN` / `KICK_GIVEUP` | kick handling with flood backoff |
| `IDLE` | per-channel random idle tick, emitted once per armed cooldown |
| `TRUST_LOADED` / `WHOIS_FIRED` / `VERIFIED` / `TRUST_DENIED` / `TRUST_RESET` / `DM_BLOCKED` / `DM_HELD` / `DM_DROPPED` | trust and DM gate |
| `DCC_OFFER` / `DCC_DONE` / `DCC_ERROR` / `DCC_TIMEOUT` | DCC SEND lifecycle |
| `ERROR` / `FATAL` / `IRC_ERROR` / `CMD_ERROR` / `LEAK_BLOCKED` | diagnostics |

The brain treats `MSG`, `INVITE`, `IDLE`, `CTCP`, `KICK`, `NOTICE` as
*actionable* (worth a model turn) and everything else as context.

## FIFO command grammar

One command per line into `bot.send`:

```
SAY <target> <text>
ACT <target> <text>
NOTICE <target> <text>
JOIN <chan> | PART <chan> | WHOIS <nick> | QUIT [reason]
RAW <irc line>
DCC <nick> <file> [name]
```

Any verb may carry an origin tag, `VERB:<tag>`, stamped into `bot.log` only
(never on the wire). This is how five sidecars can share one FIFO and still be
distinguishable. The brain writes untagged `SAY` lines.

## Trust model

Trust requires all three, checked in `trust_check()`:

1. the nick is listed in `bot.trust`,
2. the source host matches that nick's `fnmatch` glob,
3. the nick is registered/identified to services (`307` or `330`, confirmed by
   a one-shot `WHOIS`).

DMs from unlisted nicks never reach the event stream at all; DMs from listed
but not-yet-verified nicks are *held* until the WHOIS resolves, then released
as `TRUSTED` or dropped. See the long comments in `bot.py`.

## Leak gate

`leak_guard.py` records lines seen in reserved targets (listed in
`bot.reserved`) and refuses to let an outbound line to a non-reserved target
share a long verbatim run with one of them. It is a re-implementation of the
original module (which is not in `.reference/`); the API and file names match
what `bot.py` expects.

## Brain prompt shape

For each channel in the batch, `build_prompt()` renders recent ring-buffer
context, then the new events, then:

```
Your reply (SAY lines only):
```

`parse_answer()` reads back `SAY <target> <text>` lines, stripping the ANSI
codes the CLI emits. `--dry-run` prints the prompt and stops.
