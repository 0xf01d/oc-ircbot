#!/usr/bin/env python3
"""networks.py — per-network config shared by the bot supervisor and the brain.

The reference runs one `bot.py` process per IRC network — same code,
per-network env and files ("a second network is a second process with its own
env, not a threading model inside the bot"). This module is the single source
of truth for those paths so `nets.py` (which spawns the bots) and `brain.py`
(which tails their events and writes their FIFOs) can never disagree.

Config sources, in order:
  1. `IRC_NETWORKS` — space/comma-separated network labels, e.g.
     `IRC_NETWORKS="azzurra libera"`. When unset: a single implicit network
     named `irc` whose paths come straight from BOT_EVENTS / BOT_FIFO /
     BOT_LOG, reproducing the single-instance layout byte for byte.
  2. `networks.json` (next to this file, or $BOT_NETWORKS_FILE) — per-label
     env overrides, e.g.
     {"libera": {"IRC_HOST": "irc.libera.chat", "BOT_TRUST": "bot.trust.libera"}}

For every labelled network the data files derive from the label:
  events  <data>/events.<label>.log      (brain tails; bot appends)
  fifo    <data>/bot.send.<label>        (brain writes; bot reads)
  log     <data>/bot.<label>.log         (raw traffic)
unless the label's overrides set BOT_EVENTS / BOT_FIFO / BOT_LOG explicitly.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _cfg(key, default):
    return os.environ.get(key, default)


DATA_DIR = _cfg("BOT_DATA", "/data")
NETWORKS_FILE = _cfg("BOT_NETWORKS_FILE", os.path.join(HERE, "networks.json"))


class Net:
    """One IRC network: its label and the three derived file paths."""

    __slots__ = ("name", "events", "fifo", "log", "env")

    def __init__(self, name, events, fifo, log, env):
        self.name = name
        self.events = events
        self.fifo = fifo
        self.log = log
        self.env = env            # full env dict to run this net's bot with

    def __repr__(self):
        return f"Net({self.name!r}, events={self.events!r}, fifo={self.fifo!r})"


def load_overrides(path=None):
    """networks.json -> {label: {env overrides}}, {} when absent."""
    path = path or NETWORKS_FILE
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def net_for(label, data_dir=None, overrides=None, base_env=None):
    """Derive one Net from a label + optional per-label env overrides.

    Paths are label-derived (`events.<label>.log`, ...) unless the label's
    overrides set BOT_EVENTS / BOT_FIFO / BOT_LOG explicitly. `base_env`
    defaults to os.environ; the returned `env` is base_env merged with the
    overrides and the resolved paths, exactly what a bot subprocess for this
    network should run with.
    """
    data_dir = data_dir or DATA_DIR
    base_env = dict(base_env if base_env is not None else os.environ)
    overrides = dict(overrides or {})

    def resolve(env_key, default_name):
        if env_key in overrides:
            return overrides[env_key]
        return os.path.join(data_dir, default_name.format(label=label))

    events = resolve("BOT_EVENTS", "events.{label}.log")
    fifo = resolve("BOT_FIFO", "bot.send.{label}")
    log = resolve("BOT_LOG", "bot.{label}.log")

    env = dict(base_env)
    env.update(overrides)
    env["BOT_EVENTS"], env["BOT_FIFO"], env["BOT_LOG"] = events, fifo, log
    return Net(label, events, fifo, log, env)


def load(data_dir=None, overrides_path=None, base_env=None):
    """All configured networks, in IRC_NETWORKS order.

    With IRC_NETWORKS unset this is the single implicit network `irc`, whose
    paths come straight from the environment's BOT_EVENTS / BOT_FIFO / BOT_LOG
    (the round-1 single-instance layout) with the given defaults.
    """
    base_env = base_env if base_env is not None else os.environ
    data_dir = data_dir or DATA_DIR
    overrides = load_overrides(overrides_path)
    raw = (base_env.get("IRC_NETWORKS") or "").replace(",", " ").split()
    if not raw:
        return [Net(
            "irc",
            base_env.get("BOT_EVENTS", os.path.join(data_dir, "events.log")),
            base_env.get("BOT_FIFO", os.path.join(data_dir, "bot.send")),
            base_env.get("BOT_LOG", os.path.join(data_dir, "bot.log")),
            dict(base_env),
        )]
    return [net_for(label, data_dir, overrides.get(label, {}), base_env)
            for label in raw]
