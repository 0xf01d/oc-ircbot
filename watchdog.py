#!/usr/bin/env python3
"""watchdog.py — a small, honest health sidecar for the brain.

The original repo ships several sidecars (aup_watchdog.py, roll_counter.py,
firma_counter.py, ...). This project ports exactly ONE of them, the watchdog,
because the OpenCode brain needs a stall detector and nothing else from the
companion zoo is about the bridge. The other sidecars are deliberately not
ported; see README.md.

What it does: the brain touches BRAIN_HEARTBEAT once per loop. This sidecar
stats that file every WATCHDOG_POLL seconds. If the heartbeat is older than
WATCHDOG_STALL, it reports a stall. After WATCHDOG_MAX_STALLS consecutive
stalls it either runs WATCHDOG_RESTART_CMD (if set) or, when
WATCHDOG_EXIT_ON_STALL=1, exits non-zero so the platform's restart policy
(compose `restart:`, k8s RestartPolicy Always) brings the pod back.

It does not pretend to restart the *brain* container from inside Kubernetes —
a sidecar has no such power. The real liveness enforcement is the pod's
livenessProbe on the same heartbeat; this sidecar is the loud, structured
reporter that makes a silent stall visible in `kubectl logs`. Stated plainly
in the README.
"""
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

HEARTBEAT = os.environ.get("BRAIN_HEARTBEAT", "/data/brain.heartbeat")
POLL = float(os.environ.get("WATCHDOG_POLL", "10"))
STALL = float(os.environ.get("WATCHDOG_STALL", "30"))
MAX_STALLS = int(os.environ.get("WATCHDOG_MAX_STALLS", "3"))
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


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    emit("START", f"heartbeat={HEARTBEAT}", f"poll={POLL:.0f}s",
         f"stall={STALL:.0f}s", f"max_stalls={MAX_STALLS}")
    consecutive = 0
    seen_once = False
    while True:
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
                if RESTART_CMD:
                    try:
                        subprocess.run(RESTART_CMD, shell=True, timeout=30)
                        emit("RESTART_CMD_RUN", RESTART_CMD)
                    except Exception as e:  # noqa: BLE001 - report, never die
                        emit("RESTART_CMD_ERROR", repr(e))
                if EXIT_ON_STALL:
                    emit("EXIT", "restart policy should bring us back")
                    sys.exit(1)
                consecutive = 0
        else:
            if consecutive:
                emit("RECOVERED", f"age={age:.1f}s")
            consecutive = 0
        time.sleep(POLL)


if __name__ == "__main__":
    main()
