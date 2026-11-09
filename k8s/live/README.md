# oc-ircbot — live on irc.freebnc.net (#hermes)

The compose stack, deployed into `hermes-generated-content` as ONE pod:

| container | image | role |
|---|---|---|
| `brain` (main) | `ghcr.io/0xf01d/oc-ircbot:brain` | persistent OpenCode session; spawns `opencode serve` on `127.0.0.1:46099` |
| `bot` | `ghcr.io/0xf01d/oc-ircbot:bot` | TLS IRC bridge to `irc.freebnc.net:6697`; `nets.py` supervises one `bot.py` per network (freebnc, hackint, espernet, ircnet, libera, gamesurge) |
| `watchdog` (sidecar) | `ghcr.io/0xf01d/oc-ircbot:watchdog` | heartbeat + serve checks; shares the pod netns, so the loopback-only serve is reachable |

## What is where (and why)

| path | object | contents |
|---|---|---|
| `/conf` | ConfigMap `oc-ircbot-live` (`00-config.yaml`) | `bot.trust`, `bot.reserved`, `bot.startup`, `bot.startup.hackint` — read-only, changes need a rollout |
| `/data` | **PVC `lane-work`** | `networks.json`, per-network `bot.trust.<net>` / `bot.startup.<net>`, FIFOs `bot.send.<net>`, logs, `events.log`, workspace, sidecar state |
| `/data/networks.json` | file on the PVC | the **only** network map (`NETWORKS_FILE`); `nets.py` re-reads it at runtime — which is why it cannot live in the ConfigMap |

The PVC is the point: the network map, the per-network trust/startup files and
the FIFO set must survive a rollout, and `nets.py` hot-adds networks by
re-reading `/data/networks.json` — a ConfigMap edit would need a restart, a PVC
edit does not. There is deliberately no second copy of that map in git's
ConfigMap; `k8s/live/networks.json` in this repo is the reference copy, and
`tests/test_configs.py` asserts the two do not diverge.

Per-network files also explain a classic double-bot: if two labels share one
`BOT_TRUST` (e.g. both fall back to `/conf/bot.trust`), the mismatch shows up as
a second bot on the wrong network. Every `BOT_*` key in `networks.json` must
point at a `/data/...` file.

## Secrets (never in git)

```bash
kubectl -n hermes-generated-content create secret generic oc-ircbot-secrets \
  --from-literal=OPENCODE_API_KEY='<key>' \
  --from-literal=NICKSERV_PASS='<pass-if-the-nick-is-registered>'
kubectl -n hermes-generated-content rollout restart deploy/oc-ircbot
```

The brain also reads `aisix-client-key` (`AISIX_API_KEY`) for the `aisix/*`
model ladder. Without it the bot still connects and joins its channels, but
model turns fail until the key lands.

## Redeploy

Images are built by `.github/workflows/image.yml` (buildx, one per Dockerfile
target) and pushed to ghcr as the role tag plus an immutable `sha-<sha>` tag.
The package is anonymously pullable, so no imagePullSecret is needed. The
manifest uses the role tags with `imagePullPolicy: Always`, so:

```bash
# after code changes: build from the branch that holds them, then
gh workflow run image.yml --ref <branch> && gh run watch
kubectl -n hermes-generated-content rollout restart deploy/oc-ircbot

# after manifest changes only:
kubectl apply -f k8s/live/            # ConfigMap + Deployment, idempotent
```

`image.yml` triggers on push to `master` and `lane/oc-ircbot` for the paths it
needs; a build from any other branch is `workflow_dispatch` only.

## The reconciliation rule

Production used to be "whatever was `kubectl cp`'d into the pod". It is not
any more: **every change lands in git first, in this directory, and the
manifest in git is diffed against the cluster before the image is rebuilt.**
The cheap check, which is what CI-style review should run:

```bash
kubectl -n hermes-generated-content get deploy oc-ircbot -o yaml > /tmp/live.yaml
kubectl -n hermes-generated-content get cm oc-ircbot-live -o yaml > /tmp/live-cm.yaml
# compare env/volumes/resources and the four ConfigMap keys against k8s/live/
```

If they differ, either the cluster is stale (apply) or someone edited the
cluster (port the change back into `k8s/live/` in the same commit). A hand
patch inside a running container is a bug report, not a deploy: `/app` is in
the image, `/data` is a volume, and neither is a source of truth.

## Watch it come up

```bash
kubectl -n hermes-generated-content get pods -l app=oc-ircbot -w
kubectl -n hermes-generated-content logs -f deploy/oc-ircbot -c bot     # IRC wire
kubectl -n hermes-generated-content logs -f deploy/oc-ircbot -c brain  # the agent
kubectl -n hermes-generated-content exec deploy/oc-ircbot -c bot -- tail -f /data/events.log
```

The join is proven by `TLS_OK` → `CONNECTED` → `STARTUP_FIRED`, the
`JOIN #hermes` line in the container log, and `SASL_OK` on networks that
require it (hackint dropped NickServ IDENTIFY, so the password rides SASL at
registration time).

## Talk to it

Say `ocbot: ...` in `#hermes`. Only `katia` (any host, WHOIS-verified account)
is trusted for orders; everyone else is untrusted context, and DMs are rejected
at the bot. The brain replies only when addressed (`policy.py` addressing gate).

Note the two FIFO grammars, because mixing them is the classic 401: the
**brain** writes `SAY <net>|<target> <text>` to the shared FIFO, while a
**per-network** FIFO (`bot.send.<net>`) already implies the network and takes
`SAY <target> <text>`. `bot.py` derives `NET_LABEL` from its own FIFO name and
strips a stray `net|` prefix defensively.

## Attach to the brain tmux

The images wrap each service in a detached tmux session (`tmuxsup.py`, pane
mirrored to the container stdout):

```bash
kubectl -n hermes-generated-content exec -it deploy/oc-ircbot -c brain \
  -- tmux attach -t brain          # full view (needs -t)
kubectl -n hermes-generated-content exec deploy/oc-ircbot -c brain \
  -- tmux capture-pane -p -t brain | tail -40   # peek, no TTY
```

## Flux

These manifests are applied directly (namespace is ours; out-of-band objects
carry no kustomize labels, so Flux will not prune them). If/when katia wants
gitops, the two files ride an MR to the flux repo as-is.
