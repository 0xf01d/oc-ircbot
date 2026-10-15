# oc-ircbot — live on irc.freebnc.net (#hermes)

The compose stack, deployed into `hermes-generated-content` as ONE pod:

| container | image | role |
|---|---|---|
| `brain` (main) | `ghcr.io/0xf01d/oc-ircbot:brain` | persistent OpenCode session; spawns `opencode serve` on `127.0.0.1:46099` |
| `bot` | `ghcr.io/0xf01d/oc-ircbot:bot` | TLS IRC bridge to `irc.freebnc.net:6697`; `nets.py` supervises `bot.py`, joins `#hermes` |
| `watchdog` (sidecar) | `ghcr.io/0xf01d/oc-ircbot:watchdog` | heartbeat + serve checks; shares the pod netns, so the loopback-only serve is reachable |

Shared state (FIFO `bot.send`, `events.log`, `bot.log`, heartbeat,
workspace) is a pod-scoped `emptyDir` at `/data` — the same files compose
put on the `botdata` volume. `bot.trust` / `bot.startup` / `bot.reserved`
come from the `oc-ircbot-live` ConfigMap (`00-config.yaml`), mounted at
`/conf`.

Secrets are **never in git**: create them out-of-band and the pod picks
them up on the next rollout.

```bash
kubectl -n hermes-generated-content create secret generic oc-ircbot-secrets \
  --from-literal=OPENCODE_API_KEY='<key>' \
  --from-literal=NICKSERV_PASS='<pass-if-the-nick-is-registered>'
kubectl -n hermes-generated-content rollout restart deploy/oc-ircbot
```

Without `OPENCODE_API_KEY` the bot still connects and joins `#hermes`, but
model turns fail until the key lands.

## First bring-up / redeploy

Images are built by `.github/workflows/image.yml` (buildx, one per
Dockerfile target) and pushed to ghcr as the role tag plus an immutable
`sha-<sha>` tag. The manifest uses the role tags with `imagePullPolicy:
Always`, so:

```bash
# after code changes: rerun the build, then
gh workflow run image.yml --ref lane/oc-ircbot && gh run watch
kubectl -n hermes-generated-content rollout restart deploy/oc-ircbot

# after manifest changes only:
kubectl apply -f k8s/live/
```

Watch it come up:

```bash
kubectl -n hermes-generated-content get pods -l app=oc-ircbot -w
kubectl -n hermes-generated-content logs -f deploy/oc-ircbot -c bot     # IRC wire
kubectl -n hermes-generated-content logs -f deploy/oc-ircbot -c brain  # the agent
kubectl -n hermes-generated-content exec deploy/oc-ircbot -c bot -- tail -f /data/events.log
```

The join is proven by `TLS_OK` → `CONNECTED` → `STARTUP_FIRED` and the
`JOIN #hermes` line in the bot container log.

## Talk to it

Say `ocbot: ...` in `#hermes`. Only `katia` (any host, WHOIS-verified
account) is trusted for orders; everyone else is untrusted context, and
DMs are rejected at the bot. The brain replies only when addressed
(`policy.py` addressing gate).

## Attach to the brain tmux

The images wrap each service in a detached tmux session (`tmuxsup.py`,
pane mirrored to the container stdout):

```bash
kubectl -n hermes-generated-content exec -it deploy/oc-ircbot -c brain \
  -- tmux attach -t brain          # full view (needs -t)
kubectl -n hermes-generated-content exec deploy/oc-ircbot -c brain \
  -- tmux capture-pane -p -t brain | tail -40   # peek, no TTY
```

Note: `tmuxsup.py` and the tmux entrypoints land with the in-flight
lane work; images built before that commit run the services as plain
PIDs — use `logs -f -c brain` until then.

## Flux

These manifests are applied directly (namespace is ours; out-of-band
objects carry no kustomize labels, so Flux will not prune them). If/when
katia wants gitops, the same two files ride an MR to the flux repo as-is.
