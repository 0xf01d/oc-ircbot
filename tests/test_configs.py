"""Config-contract tests: the shipped topologies must never widen the
`opencode serve` bind beyond loopback (PR #1 review blocker 1). These read
the repo files — the assert the review asked for, in code, so a manifest that
re-exposes the agent's control plane turns the suite red."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def test_no_config_mentions_a_serve_host_override():
    # The knob is gone on purpose (hardcoded 127.0.0.1 in brain.py); its
    # return in any manifest would be the blocker reintroduced.
    for rel in ("docker-compose.yml", "docker-compose.smoke.yml",
                "k8s/10-oc-ircbot.yaml", "k8s/30-split-deployments.yaml",
                "k8s/90-live-check.example.yaml"):
        assert "BRAIN_SERVE_HOST" not in read(rel), rel


def test_compose_watchdog_shares_brain_netns_and_dials_loopback():
    compose = read("docker-compose.yml")
    assert re.search(r'network_mode:\s*"service:\s*brain"', compose), \
        "watchdog must join the brain's netns to reach a loopback-only serve"
    assert "BRAIN_SERVE_URL: http://127.0.0.1:" in compose
    assert "BRAIN_SERVE_URL: http://brain:" not in compose


def test_k8s_split_exposes_no_serve_service():
    split = read("k8s/30-split-deployments.yaml")
    assert "kind: Service" not in split, \
        "the agent API must stay pod-local: no Service in front of serve"
    assert "0.0.0.0" not in split
    assert 'BRAIN_SERVE_URL, value: "http://127.0.0.1:46099"' in split


def test_k8s_single_pod_serves_loopback_only():
    single = read("k8s/10-oc-ircbot.yaml")
    assert "0.0.0.0" not in single
    assert '"--host", "127.0.0.1"' in single      # the stub serve


# --- the live (production) manifests ---------------------------------------
# k8s/live/ is what actually runs in hermes-generated-content. It drifted from
# the repo once already: /data was an emptyDir here while the cluster mounted
# the lane-work PVC, and NETWORKS_FILE was missing, so the network map the bot
# really used lived nowhere in git. These tests pin the production shape and
# parse the YAML, so prose in a comment can never satisfy them.

def _live_yaml(name):
    import yaml
    with open(os.path.join(ROOT, "k8s", "live", name)) as f:
        return yaml.safe_load(f)


def _containers():
    dep = _live_yaml("10-deployment.yaml")
    return {c["name"]: c for c in dep["spec"]["template"]["spec"]["containers"]}, \
        dep["spec"]["template"]["spec"]


def test_live_deployment_mounts_the_shared_pvc_not_an_emptydir():
    _, spec = _containers()
    data = [v for v in spec["volumes"] if v["name"] == "data"][0]
    assert data["persistentVolumeClaim"]["claimName"] == "lane-work", \
        "state (networks map, per-net trust/startup, FIFOs) must survive a rollout"
    assert "emptyDir" not in data


def test_live_deployment_points_every_container_at_the_pvc_network_map():
    # One source of truth: a second copy of the map is how a label mismatch
    # spawns a second bot.
    conts, _ = _containers()
    for name in ("bot", "brain"):
        env = {e["name"]: e.get("value") for e in conts[name]["env"]}
        assert env.get("NETWORKS_FILE") == "/data/networks.json", name
    cm = _live_yaml("00-config.yaml")
    assert "networks.json" not in cm["data"], \
        "the ConfigMap must not carry a second copy of the map"


def test_live_network_map_is_valid_and_label_consistent():
    import json
    nets = json.loads(read("k8s", "live", "networks.json"))
    assert "freebnc" in nets and "hackint" in nets
    for label, conf in nets.items():
        assert conf["IRC_HOST"], label
        # Per-network files must live on /data, never the shared /conf default:
        # two labels pointing at one BOT_TRUST would share a trust list.
        for key in ("BOT_TRUST", "BOT_STARTUP"):
            if key in conf:
                assert conf[key].startswith("/data/"), (label, key, conf[key])


def test_live_deployment_keeps_the_model_ladder_in_the_manifest():
    conts, _ = _containers()
    env = {e["name"]: e.get("value") for e in conts["brain"]["env"]}
    assert env["BRAIN_MODEL"] == "aisix/auto"
    assert "aisix/auto" in env["BRAIN_MODEL_LADDER"]
    assert env["BRAIN_PREEMPT_ON_ADDRESS"] == "0"


def test_no_turn_cap_remains_in_shipped_files():
    # The cap threw the whole conversation away; compaction is the token
    # guard now. No shipped file may reintroduce the knob.
    for rel in ("brain.py", "README.md", "docker-compose.yml",
                "docs/design.md", "k8s/00-configmap.yaml",
                "k8s/live/10-deployment.yaml"):
        assert "MAX_TURNS" not in read(rel), rel


def test_live_deployment_uses_the_six_hour_age_reset():
    conts, _ = _containers()
    env = {e["name"]: e.get("value") for e in conts["brain"]["env"]}
    assert env["BRAIN_RESET_SECS"] == "21600"
