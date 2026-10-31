#!/usr/bin/env python3
"""plug.py — the one hot-plugin loader shared by brain.py and bot.py.

A plugin is a single file on the PVC (BRAIN_PLUGIN, default
/data/brain_plugin.py; BOT_PLUGIN, default /data/bot_plugin.py) defining
optional hook callables. The loader is the handlers.py pattern made
generic: mtime-gated reload (one stat per call, re-exec only when the file
changed), one lock, a fresh module object per load (never importlib.reload
on a stale module), and isolation in every direction —

  missing file    no-op, whatever is loaded keeps serving
  broken file     PLUGIN_IMPORT_ERROR, previous module keeps serving
  raising hook    PLUGIN_ERROR <hook> <repr>, reads as a no-op

Plugins live on the PVC; /app is read-only and never patched.
"""
import importlib.util
import os
import sys
import threading

# call() returns this when the plugin does not define the hook at all. A
# sentinel, not None: for rewrite and outbound "hook said None" and "no
# hook" mean opposite things (None drops/blocks; absent means pass through).
MISSING = object()


class Plugin:
    """One hot-reloaded plugin file. `log` is the host's emit callable."""

    def __init__(self, path, log):
        self.path = path
        self.log = log
        self._lock = threading.Lock()
        self._mod = None
        self._mtime = None
        self._seq = 0

    def load(self):
        """(Re)load the file when its mtime moved. Returns the effective
        module, or None when no file has ever loaded."""
        with self._lock:
            try:
                mtime = os.stat(self.path).st_mtime_ns
            except OSError:
                return self._mod            # absent: normal, not an error
            if self._mod is not None and mtime == self._mtime:
                return self._mod
            name = f"ocbot_plugin_{self._seq}"
            self._seq += 1
            sys.modules.pop(name, None)
            spec = importlib.util.spec_from_file_location(name, self.path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception as e:
                sys.modules.pop(name, None)
                self.log("PLUGIN_IMPORT_ERROR", self.path, repr(e))
                return self._mod            # broken edit: previous keeps serving
            self._mod = mod
            self._mtime = mtime
            self.log("PLUGIN_LOADED", self.path)
            return mod

    def call(self, name, *args):
        """Run the optional hook `name`. MISSING = not defined or the hook
        raised (logged as PLUGIN_ERROR): both read as a no-op. That matters
        for rewrite/outbound, whose real None return means drop/block — a
        broken hook must never be mistaken for a verdict."""
        mod = self.load()
        if mod is None:
            return MISSING
        fn = getattr(mod, name, None)
        if not callable(fn):
            return MISSING
        try:
            return fn(*args)
        except Exception as e:
            self.log("PLUGIN_ERROR", name, repr(e))
            return MISSING
