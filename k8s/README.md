# Kubernetes manifests

The same three-container topology as `docker-compose.yml`, expressed for the
`hermes-generated-content` namespace (ServiceAccount
`system:serviceaccount:hermes:hermes`).

| file | what | applied in this namespace? |
|---|---|---|
| `00-configmap.yaml` | all Python/scripts/data files, mounted read-only at `/code` | yes |
| `10-oc-ircbot.yaml` | the bridge pod: `brain` (main) + `watchdog` (sidecar) + `bot`, one `emptyDir` at `/data` | yes |
| `20-mock-irc.yaml` | mock IRC server Deployment + Service for the smoke test | yes |
| `30-split-deployments.yaml` | production split: separate `bot` Deployment + `brain`/`watchdog` Deployment on a shared **PVC** | no (PVCs forbidden here) |
| `90-live-check.example.yaml` | self-contained pod that runs the real OpenCode CLI | template (needs the secret) |

## Why one pod for bot + brain + watchdog?

`emptyDir` is **pod-scoped**: two Deployments cannot share one. The task asks
for exactly that shared `emptyDir` (`bot.log` / `bot.send` / heartbeat), so
the brain (main container), the watchdog (genuine sidecar) and the bot run as
three containers of one pod. The bot is still "its own container" and knows
nothing about the brain. When a `ReadWriteMany` PVC is available, prefer the
`30-split-deployments.yaml` layout: the bot and the brain/watchdog pod then
share the PVC instead of an `emptyDir`.

## Bring it up (smoke)

```bash
kubectl apply -f k8s/00-configmap.yaml
kubectl apply -f k8s/20-mock-irc.yaml
kubectl apply -f k8s/10-oc-ircbot.yaml
kubectl logs -n hermes-generated-content deploy/oc-mock-irc --previous   # shows the round trip
```

The mock injects `#test hello bot`, the bot emits it on `events.log`, the
brain answers through the stub (`python3 /code/stub_brain.py`) with
`SAY #test stub-ok`, and the mock records `PRIVMSG #test :stub-ok` and exits
0. That exit-0 is the assertion.

## Go live

Replace the brain container's `OC_BRAIN_CMD` with nothing and give it a Node
image plus the API key (see `90-live-check.example.yaml`). The API key is
never committed; inject it as an env var:

```bash
kubectl create secret generic oc-ircbot-secrets \
  --from-literal=OPENCODE_API_KEY="$OPENCODE_API_KEY" \
  -n hermes-generated-content
```

(Secrets are forbidden in this namespace, so the live run here passes the key
as an inline `env.value` in a throwaway manifest, and the result is recorded
in the README/PR. `optional: true` on the secretKeyRef lets the pod run in
stub mode with no secret present.)
