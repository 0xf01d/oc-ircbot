import json
import time
from unittest import mock

import pytest

import watchdog
from stub_opencode_serve import StubServe


def test_is_stalled_missing_and_old():
    assert watchdog.is_stalled(None)
    assert watchdog.is_stalled(100.0, stall=30.0)
    assert not watchdog.is_stalled(5.0, stall=30.0)


def test_heartbeat_age_missing_file(monkeypatch):
    monkeypatch.setattr(watchdog, "HEARTBEAT", "/nonexistent/brain.heartbeat")
    assert watchdog.heartbeat_age() is None


def test_heartbeat_age_reads_mtime(tmp_path, monkeypatch):
    hb = tmp_path / "brain.heartbeat"
    hb.write_text("1 1\n")
    monkeypatch.setattr(watchdog, "HEARTBEAT", str(hb))
    age = watchdog.heartbeat_age()
    assert age is not None and age < 5


def test_serve_healthy_against_stub():
    serve = StubServe().start()
    try:
        assert watchdog.serve_healthy(serve.url)
        assert not watchdog.serve_healthy("http://127.0.0.1:1")
    finally:
        serve.stop()


def test_busy_sessions_lists_busy():
    serve = StubServe().start()
    try:
        assert watchdog.busy_sessions(serve.url) == {}
        sid = serve.state.new_sid()
        serve.state.sessions[sid] = {"id": sid, "title": "t",
                                     "messages": [], "busy": True}
        assert watchdog.busy_sessions(serve.url) == {sid: {"type": "busy"}}
    finally:
        serve.stop()


def test_busy_sessions_unreachable_returns_none():
    assert watchdog.busy_sessions("http://127.0.0.1:1", timeout=0.2) is None


def test_agent_snapshot(tmp_path, monkeypatch):
    p = tmp_path / "agent.status"
    p.write_text(json.dumps({"ts": 1, "turns": 3, "refusals": 1,
                             "wedges": 0, "sessions": {}}))
    monkeypatch.setattr(watchdog, "AGENT_STATUS", str(p))
    snap = watchdog.agent_snapshot()
    assert snap["turns"] == 3
    monkeypatch.setattr(watchdog, "AGENT_STATUS", str(tmp_path / "nope"))
    assert watchdog.agent_snapshot() is None


def test_abort_session_hits_the_stub():
    serve = StubServe().start()
    try:
        sid = serve.state.new_sid()
        assert watchdog.abort_session(serve.url, sid)
        assert sid in serve.state.aborted
        assert not watchdog.abort_session("http://127.0.0.1:1", sid)
    finally:
        serve.stop()


def test_escalate_exit_on_stall_and_restart_cmd(monkeypatch):
    monkeypatch.setattr(watchdog, "EXIT_ON_STALL", False)
    ran = []
    monkeypatch.setattr(watchdog, "RESTART_CMD", "true")
    with mock.patch.object(watchdog.subprocess, "run",
                           side_effect=lambda cmd, **kw: ran.append(cmd)):
        watchdog.escalate("stall")          # no exit when the flag is off
        assert ran
    monkeypatch.setattr(watchdog, "EXIT_ON_STALL", True)
    with mock.patch.object(watchdog.subprocess, "run"), \
            pytest.raises(SystemExit) as e:
        watchdog.escalate("stall")
    assert e.value.code == 1
