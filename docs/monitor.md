# The Monitor-equivalent: prompt_async queueing

Why the brain delivers IRC events to the agent the way it does, and what is
genuinely lost compared to Claude Code's Monitor tool.

## What Monitor did in the original

In vjt/claude-ircbot, Claude Code attached its **Monitor** tool to `bot.py`'s
stdout. Every structured event line (`MSG`, `INVITE`, `CTCP`, `KICK`, `IDLE`,
…) arrived as a *mid-conversation notification*: the agent saw IRC traffic
while it was thinking, could fold it into the current turn, and answered (or
stayed silent) by writing into the `bot.send` FIFO. The conversation, the
events and the replies were one continuous context.

## What opencode serve offers

`opencode serve` (v1.19.0, verified live on 2026-10-15) exposes one prompt
stream per session:

- `POST /session/{id}/prompt_async` accepts a prompt **while a turn is
  streaming** and answers `204`. Empirically the prompt is not refused and
  not lost: the new user message lands in the session and the model folded
  it into the in-flight turn's reply (one `session.idle` marked the end —
  the probe prompt "Count 1..8" + a mid-turn "reply: second" produced one
  assistant message covering both).
- The behaviour is not contractual, though: it is best read as
  "delivery at the earliest model boundary" — either folded in (observed) or
  queued as the next turn. The brain's design works under **both**
  interpretations, because it also drains any still-pending batch on
  `session.idle`.
- There is no documented notification/inject API; `POST /session/{id}/abort`
  (cancellation) is the only interrupt surface.

## The emulation

brain.py (ONE session; every network's events interleave in it):

1. `Tailer` reads each network's `events.log`; `feed()` renders the event as
   a vjt-style line — `[HH:MM] net|target <nick>: text (trusted; HOST=…)`,
   the Monitor-never-host-blind rule — and appends it to the session's
   **pending** batch (`policy.wakes_model()`: every event wakes the model,
   like Monitor; only muted channels and `BRAIN_ADDRESS_ONLY` stay out).
2. `due()` releases the batch after `BRAIN_BATCH_WINDOW` seconds (or at once
   at `BRAIN_BATCH_MAX` events), so events that land close together reach the
   model as one prompt instead of N turns.
3. `submit()` renders the new batch via `policy.build_prompt` and calls
   `prompt_async`. If a turn is already streaming, the server queues it —
   the brain does not block, does not poll for the answer; batches that
   arrive mid-turn queue in `sess.pending` and drain on the next idle.
4. The SSE thread wakes on `session.idle`, fetches the messages, parses FIFO
   verbs, dispatches — and **if the pending queue is non-empty, submits the
   next batch immediately**. That last step is what makes the emulation
   tight: during a burst the agent effectively runs back-to-back turns, one
   per batch, without waiting for a new trigger.

The one continuous session does the rest of Monitor's job: every prompt
lands in the same conversation the agent has been reading all along, exactly
as Monitor notifications stayed in Claude Code's context — there is no
context window to reconstruct, because it never left.

## What is honestly different

| Monitor (Claude Code) | this brain |
|---|---|
| Events injected **mid-turn** | Events delivered at the earliest model boundary: folded into a streaming turn when the server does that (observed on v1.19.0), otherwise the next turn starts immediately on idle (the drain step below) |
| One conversation for everything | **The same**: ONE session spans every network and channel; the `[HH:MM] net\|target` prefixes carry the routing, and each answer line routes itself (`SAY azzurra\|#c text`) |
| Agent could act with tools mid-turn | The agent acts only through FIFO verbs parsed from its reply text (`SAY`/`ACT`/`NOTICE`/`JOIN`/`PART`/`WHOIS`/`QUIT`, each net-routed), under the `opencode.json` permission gate |
| Context continuity was the session itself | Continuity is rebuilt from **disk** (`memory/MEMORY.md` + 14-day activity log) on every new/rehydrated session, so resets and compaction do not amnesia the agent |

The last row is the practical compensation: because the agent rehydrates from
disk memory at session start, the turn-boundary delivery model is not
fragile — a wedged or refused session can be thrown away and a fresh one
rebuilt with its memory intact, something Claude Code's Monitor could not do.

## Verified behaviour

- `prompt_async` during a busy turn returns `204` and the prompt reaches the
  model (measured live on v1.19.0: folded into the in-flight turn's reply;
  the stub replays the queue-on-idle interpretation in
  `tests/test_brain.py`).
- A batch submitted while the previous batch's turn is streaming is never
  lost: `submit()` re-queues on transport errors, and `on_idle()` drains the
  queue after every turn — whichever way the server schedules the prompt.
