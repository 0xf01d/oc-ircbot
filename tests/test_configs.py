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
