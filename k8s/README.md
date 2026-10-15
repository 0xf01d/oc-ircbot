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
brain submits it to the stub-serve sidecar (`stub_opencode_serve.py`, the
same pipeline as production: prompt_async -> SSE -> idle -> FIFO) and answers
`SAY #test stub-ok`; the mock records `PRIVMSG #test :stub-ok` and exits 0.
That exit-0 is the assertion.

## Regenerating the ConfigMap

`00-configmap.yaml` embeds the repo files verbatim. After changing any of
them, regenerate it (from the repo root):

```bash
python3 - <<'EOF'
files = ["bot.py", "leak_guard.py", "util.py",
         "brain.py", "policy.py", "networks.py", "memory_log.py", "watchdog.py",
         "opencode.json",
         "tests/mock_irc_server.py", "tests/stub_opencode_serve.py",
         "bot.say", "bot.startup", "bot.trust", "bot.trust.ircnet", "bot.reserved"]
out = ["# Generated from the repo files by the snippet in k8s/README.md —",
       "# do not edit by hand.", "apiVersion: v1", "data:"]
for f in files:
    out.append(f"  {f.split('/')[-1]}: |")
    out += [("    " + ln if ln.strip() else "") for ln in open(f).read().splitlines()]
out += ["kind: ConfigMap", "metadata:", "  name: oc-ircbot-code",
        "  namespace: hermes-generated-content"]
open("k8s/00-configmap.yaml", "w").write("\n".join(out) + "\n")
EOF
```

## Go live

Drop the `stub-serve` container, remove the brain's `BRAIN_SERVE_URL` (the
brain then spawns `opencode serve` itself — put a Node image + the OpenCode
CLI in the brain container, see `90-live-check.example.yaml`) and inject the
API key. The key is never committed:

```bash
kubectl create secret generic oc-ircbot-secrets \
  --from-literal=OPENCODE_API_KEY="$OPENCODE_API_KEY" \
  -n hermes-generated-content
```

(Secrets are forbidden in this namespace, so the live run here passes the key
as an inline `env.value` in a throwaway manifest, and the result is recorded
in the README/PR. `optional: true` on the secretKeyRef lets the pod run in
stub mode with no secret present.)
