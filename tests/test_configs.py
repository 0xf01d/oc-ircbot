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
        # Startup files stay on the PVC (they are runtime state). Trust is the
        # exception: it must come from the read-only ConfigMap mount, never a
        # file the bot can rewrite.
        if "BOT_TRUST" in conf:
            assert conf["BOT_TRUST"].startswith("/conf/"), \
                (label, "BOT_TRUST", conf["BOT_TRUST"])
        if "BOT_STARTUP" in conf:
            assert conf["BOT_STARTUP"].startswith("/data/"), \
                (label, "BOT_STARTUP", conf["BOT_STARTUP"])


def test_live_per_network_trust_files_exist_in_the_read_only_config():
    import json
    nets = json.loads(read("k8s", "live", "networks.json"))
    cm = _live_yaml("00-config.yaml")["data"]
    for label, conf in nets.items():
        path = conf.get("BOT_TRUST")
        if not path:
            continue
        key = path.rsplit("/", 1)[-1]
        assert key in cm, (label, key)
    # The injection's result is not carried into the reviewed ConfigMap.
    assert "szczot3k" not in cm["bot.trust.hackint"]


def test_live_containers_run_with_a_read_only_root():
    dep = _live_yaml("10-deployment.yaml")
    spec = dep["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["tmp"]["emptyDir"] == {}
    conts, _ = _containers()
    for name in ("bot", "brain", "watchdog"):
        c = conts[name]
        sc = c["securityContext"]
        assert sc["readOnlyRootFilesystem"] is True, name
        assert sc["allowPrivilegeEscalation"] is False, name
        assert sc["capabilities"]["drop"] == ["ALL"], name
        mounts = {m["mountPath"] for m in c["volumeMounts"]}
        assert "/tmp" in mounts, name
        env = {e["name"]: e.get("value") for e in c["env"]}
        assert env["PYTHONDONTWRITEBYTECODE"] == "1", name
    # The brain cannot write its opencode auth/session DB into a read-only
    # $HOME, so those two cache paths are explicitly writable.
    brain_mounts = {m["mountPath"] for m in conts["brain"]["volumeMounts"]}
    assert "/root/.local/share/opencode" in brain_mounts
    assert "/root/.cache/opencode" in brain_mounts


def test_live_loads_executable_code_from_the_image_not_the_pvc():
    conts, _ = _containers()
    bot_env = {e["name"]: e.get("value") for e in conts["bot"]["env"]}
    brain_env = {e["name"]: e.get("value") for e in conts["brain"]["env"]}
    assert bot_env["BOT_HANDLERS"] == "/app/handlers.py"
    assert bot_env["BOT_IRC_COMMANDS"] == "/app/irc_commands.py"
    assert brain_env["BRAIN_PLUGIN"] == "/app/brain_plugin.py"


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


def test_trust_check_rejects_a_nick_planted_only_in_the_pvc_file(tmp_path):
    # The incident left `szczot3k dn42/person/szczot3k` in a writable /data
    # trust file. The live ConfigMap is the trust source now, and it does not
    # carry that line, so a nick planted on the PVC never becomes trusted.
    import json
    from unittest import mock
    import bot
    import networks

    planted = tmp_path / "bot.trust.hackint"
    planted.write_text("szczot3k dn42/person/szczot3k\n")
    reviewed = tmp_path / "bot.trust.hackint.conf"
    reviewed.write_text(_live_yaml("00-config.yaml")["data"]["bot.trust.hackint"])
    live = json.loads(read("k8s", "live", "networks.json"))
    ovr = tmp_path / "networks.json"
    ovr.write_text(json.dumps({"hackint": dict(live["hackint"],
                                               BOT_TRUST=str(reviewed))}))
    net = networks.load(data_dir=str(tmp_path), overrides_path=str(ovr),
                        base_env={"IRC_NETWORKS": "hackint"})[0]
    assert net.env["BOT_TRUST"] == str(reviewed)

    old = (bot.TRUST_FILE, bot.trust_rules, set(bot.verified),
           set(bot.whois_pending))
    try:
        bot.TRUST_FILE = net.env["BOT_TRUST"]
        bot.trust_rules, bot.verified, bot.whois_pending = [], set(), set()
        with mock.patch.object(bot, "emit"):
            bot.load_trust()
        assert not bot.is_trust_listed("szczot3k")
        assert bot.trust_check("szczot3k", "dn42/person/szczot3k") == \
            (False, "not-listed")
    finally:
        (bot.TRUST_FILE, bot.trust_rules, bot.verified,
         bot.whois_pending) = old
