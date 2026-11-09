#!/usr/bin/env python3
"""util.py — the few lines bot.py, brain.py and watchdog.py all share."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")


def now(fmt):
    return datetime.now(TZ).strftime(fmt)


def env(key, default=""):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    v = os.environ.get(key)
    return default if v is None else v not in ("0", "no", "off", "false", "")


def emit(tag, kind, *parts):
    print(f"[{now('%H:%M:%S')}] {tag} {kind} " +
          " ".join(str(p) for p in parts), flush=True)
