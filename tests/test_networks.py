import json
import os

import networks


def test_implicit_single_network_uses_env_paths():
    base = {"BOT_EVENTS": "/d/events.log", "BOT_FIFO": "/d/bot.send",
            "BOT_LOG": "/d/bot.log"}
    nets = networks.load(data_dir="/d", base_env=base,
                         overrides_path="/nonexistent.json")
    assert len(nets) == 1
    n = nets[0]
    assert n.name == "irc"
    assert n.events == "/d/events.log"
    assert n.fifo == "/d/bot.send"
    assert n.env["BOT_EVENTS"] == "/d/events.log"


def test_implicit_single_network_defaults():
    nets = networks.load(data_dir="/d", base_env={},
                         overrides_path="/nonexistent.json")
    n = nets[0]
    assert n.events == "/d/events.log"
    assert n.fifo == "/d/bot.send"
    assert n.log == "/d/bot.log"


def test_json_only_networks_added_at_runtime(tmp_path):
    """networks.json may DEFINE networks: adding a block is enough, no env
    change, no restart of the running bots (nets.py reconciles hot)."""
    p = tmp_path / "networks.json"
    p.write_text('{"libera": {"IRC_HOST": "irc.libera.chat"}}')
    nets = networks.load(data_dir="/d", base_env={}, overrides_path=str(p))
    assert [n.name for n in nets] == ["libera"]
    assert nets[0].env["IRC_HOST"] == "irc.libera.chat"
    assert nets[0].events == "/d/events.libera.log"


def test_irc_networks_derive_labelled_paths():
    base = {"IRC_NETWORKS": "azzurra libera"}
    nets = networks.load(data_dir="/d", base_env=base)
    assert [n.name for n in nets] == ["azzurra", "libera"]
    assert nets[0].events == "/d/events.azzurra.log"
    assert nets[0].fifo == "/d/bot.send.azzurra"
    assert nets[1].fifo == "/d/bot.send.libera"
    # Labels survive commas and repetition of the base env paths: each net
    # gets its OWN files even when the base env sets shared-looking values.
    base["BOT_EVENTS"] = "/d/events.log"
    nets = networks.load(data_dir="/d", base_env=dict(base))
    assert nets[0].events == "/d/events.azzurra.log"
    assert nets[1].events == "/d/events.libera.log"


def test_overrides_apply_per_label(tmp_path):
    ovr = tmp_path / "networks.json"
    ovr.write_text(json.dumps({
        "libera": {"IRC_HOST": "irc.libera.chat",
                   "BOT_TRUST": "bot.trust.libera",
                   "BOT_FIFO": "/custom/fifo"},
    }))
    base = {"IRC_NETWORKS": "azzurra,libera"}
    nets = networks.load(data_dir="/d", overrides_path=str(ovr),
                         base_env=base)
    assert nets[1].env["IRC_HOST"] == "irc.libera.chat"
    assert nets[1].fifo == "/custom/fifo"
    assert nets[1].env["BOT_TRUST"] == "bot.trust.libera"
    # The other network is untouched and derives its own paths.
    assert nets[0].env.get("IRC_HOST") is None
    assert nets[0].events == "/d/events.azzurra.log"
    # Derived paths are pinned in every net's env, so a spawned bot.py sees
    # exactly what the brain tails.
    assert nets[0].env["BOT_EVENTS"] == "/d/events.azzurra.log"


def test_broken_or_missing_overrides_file_is_ignored(tmp_path):
    bad = tmp_path / "networks.json"
    bad.write_text("{not json")
    nets = networks.load(data_dir="/d", overrides_path=str(bad),
                         base_env={"IRC_NETWORKS": "a"})
    assert nets[0].name == "a"
    assert networks.load_overrides(str(tmp_path / "missing.json")) == {}


def test_shipped_networks_json_is_valid():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "networks.json")) as f:
        data = json.load(f)
    assert isinstance(data, dict) and data
    for label, env in data.items():
        assert "IRC_HOST" in env, f"{label} needs IRC_HOST"
        assert "BOT_TRUST" in env, f"{label} needs its own trust file"
