#!/usr/bin/env python3
"""stub_brain.py — deterministic stand-in for the OpenCode CLI.

OC_BRAIN_CMD points the brain at this instead of `opencode`, so CI and the
on-cluster smoke test stay offline and reproducible. It reads the prompt on
stdin, finds the first channel mentioned in it, and answers with a fixed
`SAY <channel> <marker>` line — exactly the contract brain.py parses.

Marker is STUB_MARKER (default `stub-ok`) so the mock IRC server can assert on
a known string.
"""
import os
import re
import sys

prompt = sys.stdin.read()
marker = os.environ.get("STUB_MARKER", "stub-ok")
chan = "#test"
m = re.search(r"[#&][A-Za-z0-9_\-]+", prompt)
if m:
    chan = m.group(0)
print(f"SAY {chan} {marker}")
