# Captured from the running pod, 2026-11-14

These files are NOT part of the build. They are a verbatim `kubectl cp` capture of code
that was running in production but exists in no git repository, recorded because
`TRUST_FORCED` — the log line proving a prompt injection granted trust — could not be
found anywhere in `/app` or in this repo.

Pod: `oc-ircbot-59664d5c45-2bm2h` (namespace `hermes-generated-content`), captured
2026-11-14 ~17:09-17:10 Europe/Rome with:

```bash
kubectl -n hermes-generated-content cp -c bot  <pod>:/data/handlers.py     handlers.py.live
kubectl -n hermes-generated-content cp -c bot  <pod>:/data/irc_commands.py irc_commands.py.live
kubectl -n hermes-generated-content cp -c brain <pod>:/data/brain_plugin.py brain_plugin.py.live
kubectl -n hermes-generated-content cp -c brain <pod>:/app/policy.py       policy.py.live
kubectl -n hermes-generated-content cp -c bot  <pod>:/app/bot.py           bot.py.live
```

| file | live path | source container |
|---|---|---|
| `handlers.py.live` | `/data/handlers.py` | bot |
| `irc_commands.py.live` | `/data/irc_commands.py` | bot |
| `brain_plugin.py.live` | `/data/brain_plugin.py` | brain |
| `policy.py.live` | `/app/policy.py` | brain |
| `bot.py.live` | `/app/bot.py` | bot |

`/data/handlers.py` is the important one: it held `h_trust`, which appended rules to the
trust file and flipped the live `verified` set from a FIFO command. The `/data` copies of
`handlers.py` and `irc_commands.py` had drifted far beyond the image; they are captured
here before the hardening patch so the fix is reviewable as a diff against what ran.

Do not edit these to "fix" behaviour — the fix belongs in the source files in the repo and
the build. They are evidence.
