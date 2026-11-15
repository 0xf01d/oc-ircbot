#!/usr/bin/env python3
"""watchdog.py — agent-aware health sidecar, three layers:

  process  BRAIN_HEARTBEAT mtime (brain stall detector);
  server   /global/health + /session/status on the opencode serve — a dead
           server, or a session stuck busy/retry beyond WATCHDOG_WEDGED
           seconds, is aborted and escalated (the brain cannot respawn an
           attached serve);
  agent    BRAIN_AGENT_STATUS reported loudly; the brain acts on its own state.

Escalation: run WATCHDOG_RESTART_CMD, or with WATCHDOG_EXIT_ON_STALL=1 exit
non-zero and let the platform's restart policy bring the stack back (a
sidecar cannot restart its own container in Kubernetes; the restart command
is the honest lever it has).
"""
import json
import os
import signal
import subprocess
import sys
import time
from functools import partial

import oc_compat
from util import emit as _emit, env, env_bool

emit = partial(_emit, "WATCHDOG")

HEARTBEAT = env("BRAIN_HEARTBEAT", "/data/brain.heartbeat")
AGENT_STATUS = env("BRAIN_AGENT_STATUS", "/data/agent.status")
SERVE_URL = env("BRAIN_SERVE_URL",
                "http://127.0.0.1:" + env("BRAIN_SERVE_PORT", "46099"))
OPENCODE_VERSION = env("BRAIN_OPENCODE_VERSION", "")
SERVER_USERNAME = env("OPENCODE_SERVER_USERNAME", "opencode")
SERVER_PASSWORD = env("OPENCODE_SERVER_PASSWORD", "")
POLL = float(env("WATCHDOG_POLL", "10"))
STALL = float(env("WATCHDOG_STALL", "30"))
MAX_STALLS = int(env("WATCHDOG_MAX_STALLS", "3"))
WEDGED = float(env("WATCHDOG_WEDGED", "300"))
SERVE_MAX_FAILS = int(env("WATCHDOG_SERVE_MAX_FAILS", "3"))
RESTART_CMD = env("WATCHDOG_RESTART_CMD", "")
EXIT_ON_STALL = env_bool("WATCHDOG_EXIT_ON_STALL")


def heartbeat_age(now_ts=None):
    """Seconds since the heartbeat file was last written, or None if absent."""
    now_ts = time.time() if now_ts is None else now_ts
    try:
        return now_ts - os.path.getmtime(HEARTBEAT)
    except OSError:
        return None


def is_stalled(age, stall=STALL):
    """A missing heartbeat counts as stalled; None handled by the caller."""
    return age is None or age > stall


def _v2():
    return oc_compat.is_v2(OPENCODE_VERSION)


def _request(url, path, method="GET", body=None, timeout=3,
             version=None):
    return oc_compat.request(
        url.rstrip("/") + path, method=method, body=body, timeout=timeout,
        password=SERVER_PASSWORD, username=SERVER_USERNAME,
        version=version if version is not None else (OPENCODE_VERSION
                                                      or "1.19.5"))


def serve_healthy(url=None, timeout=3):
    """True when the version-appropriate health route answers."""
    url = (url or SERVE_URL).rstrip("/")
    try:
        if _v2():
            data = _request(url, "/api/info", timeout=timeout)
            return bool(data and (data.get("version") or data.get("urls")))
        data = _request(url, "/global/health", timeout=timeout,
                        version="1.19.5")
        if data and data.get("healthy") is True:
            return True
        if OPENCODE_VERSION:
            return False
        data = _request(url, "/api/info", timeout=timeout, version="2.3.0")
        return bool(data and (data.get("version") or data.get("urls")))
    except (OSError, ValueError):
        return False


def busy_sessions(url=None, timeout=5):
    """{sessionID: status} for busy sessions, None when unreachable."""
    url = (url or SERVE_URL).rstrip("/")
    try:
        if _v2():
            data = _request(url, "/api/session/active", timeout=timeout)
            data = (data or {}).get("data") or {}
            return {sid: {"type": "busy"} for sid, st in data.items()
                    if (st or {}).get("type") == "running"}
        return _request(url, "/session/status", timeout=timeout,
                        version="1.19.5") or {}
    except (OSError, ValueError):
        return None


def agent_snapshot(path=None):
    try:
        with open(path or AGENT_STATUS) as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return None


def abort_session(url, sid, timeout=10):
    try:
        if _v2():
            data = _request(url, f"/api/session/{sid}/interrupt",
                            method="POST", timeout=timeout)
            return data is not None
        data = _request(url, f"/session/{sid}/abort", method="POST",
                        body={}, timeout=timeout, version="1.19.5")
        return data is not None and data is not False
    except OSError:
        return False


def escalate(action, note=""):
    """Restart command first, exit-for-restart-policy second; report always."""
    if RESTART_CMD:
        try:
            subprocess.run(RESTART_CMD, shell=True, timeout=60)
            emit("RESTART_CMD_RUN", RESTART_CMD, note)
        except Exception as e:  # report, never die
            emit("RESTART_CMD_ERROR", repr(e))
    if EXIT_ON_STALL:
        emit("EXIT", f"{action}: restart policy should bring us back", note)
        sys.exit(1)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    emit("START", f"heartbeat={HEARTBEAT}", f"serve={SERVE_URL}",
          f"poll={POLL:.0f}s", f"stall={STALL:.0f}s", f"wedged={WEDGED:.0f}s")
    consecutive = 0
    seen_once = False
    serve_fails = 0
    busy_seen = {}                     # sid -> first-seen monotonic
    while True:
        # -- process layer ----------------------------------------------------
        age = heartbeat_age()
        if age is not None and not seen_once:
            emit("HEARTBEAT_SEEN", f"{age:.1f}s")
            seen_once = True
        if is_stalled(age):
            consecutive += 1
            shown = "missing" if age is None else f"{age:.1f}s"
            emit("STALL", f"age={shown}", f"consecutive={consecutive}",
                  f"threshold={STALL:.0f}s")
            if consecutive >= MAX_STALLS:
                emit("STALL_CONFIRMED", f"{consecutive} consecutive")
                escalate("stall")
                consecutive = 0
        else:
            if consecutive:
                emit("RECOVERED", f"age={age:.1f}s")
            consecutive = 0

        # -- server + session layer --------------------------------------------
        if serve_healthy():
            if serve_fails:
                emit("SERVE_RECOVERED")
            serve_fails = 0
            busy = busy_sessions()
            nowm = time.monotonic()
            for sid in list(busy_seen):
                if not busy or sid not in busy:
                    busy_seen.pop(sid, None)
            for sid, status in (busy or {}).items():
                first = busy_seen.setdefault(sid, nowm)
                held = nowm - first
                if held >= WEDGED:
                    emit("WEDGED_SESSION", sid,
                          f"status={status.get('type')}", f"{held:.0f}s")
                    if abort_session(SERVE_URL, sid):
                        emit("ABORT_SENT", sid)
                    busy_seen.pop(sid, None)
                    escalate("wedged-session", sid)
        else:
            serve_fails += 1
            emit("SERVE_UNHEALTHY", f"consecutive={serve_fails}")
            if serve_fails >= SERVE_MAX_FAILS:
                escalate("serve-down")
                serve_fails = 0

        # -- agent layer (loud reporting; the brain acts on its own state) -----
        snap = agent_snapshot()
        if snap:
            emit("AGENT", f"turns={snap.get('turns', 0)}",
                  f"refusals={snap.get('refusals', 0)}",
                  f"wedges={snap.get('wedges', 0)}",
                  f"sessions={len(snap.get('sessions', {}))}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
