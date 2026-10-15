#!/usr/bin/env python3
"""watchdog.py — agent-aware health sidecar.

Round-1 watchdog answered exactly one question: is the brain's heartbeat
file fresh? Issue #2 (gap 3) is explicit that for a long-running agent the
real failure modes are *session* failures — a wedged turn, a retry storm, a
dead model server — and a green heartbeat proves nothing about any of them.

This version watches three independent layers:

  process   BRAIN_HEARTBEAT mtime (the old stall detector, unchanged);
  server    GET /global/health + GET /session/status on the opencode serve
            URL — a server that dies, or a session stuck busy/retry for
            WATCHDOG_WEDGED seconds, is acted on (abort via POST
            /session/{id}/abort, then escalate);
  agent     BRAIN_AGENT_STATUS (JSON the brain refreshes every loop) —
            reported loudly so refusals/wedges/resets are visible in logs.

Escalation is the same contract as before: run WATCHDOG_RESTART_CMD (set it
to restart brain+serve — e.g. `systemctl --user restart oc-ircbot-brain`) or,
with WATCHDOG_EXIT_ON_STALL=1, exit non-zero and let the platform's restart
policy bring the stack back. A sidecar cannot restart its own container in
Kubernetes; the restart command is the honest lever it has.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

HEARTBEAT = os.environ.get("BRAIN_HEARTBEAT", "/data/brain.heartbeat")
AGENT_STATUS = os.environ.get("BRAIN_AGENT_STATUS", "/data/agent.status")
SERVE_URL = os.environ.get("BRAIN_SERVE_URL",
                           "http://127.0.0.1:" +
                           os.environ.get("BRAIN_SERVE_PORT", "46099"))
POLL = float(os.environ.get("WATCHDOG_POLL", "10"))
STALL = float(os.environ.get("WATCHDOG_STALL", "30"))
MAX_STALLS = int(os.environ.get("WATCHDOG_MAX_STALLS", "3"))
WEDGED = float(os.environ.get("WATCHDOG_WEDGED", "300"))
SERVE_MAX_FAILS = int(os.environ.get("WATCHDOG_SERVE_MAX_FAILS", "3"))
RESTART_CMD = os.environ.get("WATCHDOG_RESTART_CMD", "")
EXIT_ON_STALL = os.environ.get("WATCHDOG_EXIT_ON_STALL", "0") not in \
    ("0", "no", "off", "false", "")

_TZ = ZoneInfo("Europe/Rome")


def emit(kind, *parts):
    line = f"[{datetime.now(_TZ).strftime('%H:%M:%S')}] WATCHDOG {kind} " + \
        " ".join(str(p) for p in parts)
    print(line, flush=True)


def heartbeat_age(now=None):
    """Seconds since the heartbeat file was last written, or None if absent."""
    now = time.time() if now is None else now
    try:
        return now - os.path.getmtime(HEARTBEAT)
    except OSError:
        return None


def is_stalled(age, stall=STALL):
    """A missing heartbeat counts as stalled once the sidecar knows to expect
    one; None is handled by the caller (grace on first sight)."""
    return age is None or age > stall


def serve_healthy(url=None, timeout=3):
    """True only when /global/health answers {"healthy": true}."""
    url = (url or SERVE_URL).rstrip("/")
    try:
        with urllib.request.urlopen(url + "/global/health",
                                    timeout=timeout) as r:
            return json.loads(r.read()).get("healthy") is True
    except (OSError, ValueError):
        return False


def busy_sessions(url=None, timeout=5):
    """{sessionID: status} for every busy/retry session, or None when the
    server cannot be reached at all."""
    url = (url or SERVE_URL).rstrip("/")
    try:
        with urllib.request.urlopen(url + "/session/status",
                                    timeout=timeout) as r:
            return json.loads(r.read()) or {}
    except (OSError, ValueError):
        return None


def wedged_sessions(busy, wedged_for=WEDGED, now=None):
    """Sessions whose status says busy/retry beyond the wedged threshold.

    opencode's status entries carry `next` (retry backoff deadline, epoch ms)
    for retries; for plain busy we rely on consecutive sightings, tracked by
    the caller. Here: retry entries whose `next` is in the past count as
    wedged; busy entries are returned for the caller's sighting counter via
    (sid, is_retry_past_due).
    """
    now_ms = int((now or time.time()) * 1000)
    out = []
    for sid, status in (busy or {}).items():
        kind = status.get("type")
        if kind == "retry" and status.get("next", 0) and \
                status["next"] < now_ms:
            out.append(sid)
        elif kind == "busy" and status.get("since") and \
                (now_ms - status["since"]) > wedged_for * 1000:
            out.append(sid)
    return out


def agent_snapshot(path=None):
    """The brain's agent-state JSON, or None."""
    try:
        with open(path or AGENT_STATUS) as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return None


def abort_session(url, sid, timeout=10):
    try:
        req = urllib.request.Request(
            url.rstrip("/") + f"/session/{sid}/abort", data=b"{}",
            method="POST", headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except OSError:
        return False


def escalate(action, note=""):
    """Restart command first, exit-for-restart-policy second; report always."""
    if RESTART_CMD:
        try:
            subprocess.run(RESTART_CMD, shell=True, timeout=60)
            emit("RESTART_CMD_RUN", RESTART_CMD, note)
        except Exception as e:  # noqa: BLE001 - report, never die
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
        if age is not None:
            if not seen_once:
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
            now = time.monotonic()
            for sid in list(busy_seen):
                if not busy or sid not in busy:
                    busy_seen.pop(sid, None)
            for sid, status in (busy or {}).items():
                first = busy_seen.setdefault(sid, now)
                held = now - first
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
