#!/usr/bin/env python3
"""networks.py — per-network config shared by the bots and the brain.

One `bot.py` process per IRC network: same code, per-network env and files
("a second network is a second process with its own env"). This module is the
single source of truth for those paths, so the spawned bots and the brain
(which tails their events and writes their FIFOs) can never disagree.

  1. `IRC_NETWORKS` — space/comma-separated labels, e.g. "azzurra libera".
     Unset: a single implicit network `irc` whose paths come straight from
     BOT_EVENTS / BOT_FIFO / BOT_LOG (the single-instance layout).
  2. `networks.json` (next to this file) — per-label env overrides, e.g.
     {"libera": {"IRC_HOST": "irc.libera.chat", "BOT_TRUST": "bot.trust.libera"}}

Labelled networks derive data files from the label (events.<label>.log,
bot.send.<label>, bot.<label>.log) unless the label's overrides set the
BOT_EVENTS / BOT_FIFO / BOT_LOG paths explicitly.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = "/data"
NETWORKS_FILE = os.path.join(HERE, "networks.json")


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
    """Derive one Net from a label + overrides; `env` is what a bot subprocess
    for this network runs with (base_env + overrides + resolved paths)."""
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

    env_ = dict(base_env)
    env_.update(overrides)
    env_["BOT_EVENTS"], env_["BOT_FIFO"], env_["BOT_LOG"] = events, fifo, log
    return Net(label, events, fifo, log, env_)


def load(data_dir=None, overrides_path=None, base_env=None):
    """All configured networks, in IRC_NETWORKS order; unset means the single
    implicit network `irc` from the environment's BOT_* paths."""
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
