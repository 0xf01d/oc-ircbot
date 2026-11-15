import time

import pytest

import brain
import oc_compat
from stub_opencode_serve import StubServe


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition never became true")


def assistant_text(messages):
    return brain.last_assistant_text(messages)


def test_cli_version_selects_the_v2_surface(monkeypatch):
    monkeypatch.setattr(oc_compat, "detect_version",
                        lambda *a, **k: "opencode v2.3.0")
    assert brain.Api("http://127.0.0.1:1").v2


def test_cli_version_selects_the_v1_surface(monkeypatch):
    monkeypatch.setattr(oc_compat, "detect_version",
                        lambda *a, **k: "1.19.5")
    assert not brain.Api("http://127.0.0.1:1").v2


def test_health_v1_and_v2(monkeypatch):
    v1 = StubServe(api_version="1.19.5").start()
    v2 = StubServe(api_version="2.3.0", password="pw").start()
    monkeypatch.setattr(brain, "SERVER_PASSWORD", "pw")
    try:
        assert brain.Serve(v1.url, version="1.19.5").healthy()
        assert brain.Serve(v2.url, version="2.3.0").healthy()
    finally:
        v1.stop()
        v2.stop()


@pytest.mark.parametrize("version", ["1.19.5", "2.3.0"])
def test_create_prompt_history_abort(version):
    password = "pw" if version.startswith("2") else ""
    serve = StubServe(api_version=version, password=password).start()
    old_password = brain.SERVER_PASSWORD
    brain.SERVER_PASSWORD = password
    try:
        api = brain.Api(serve.url, version=version)
        sid = api.create_session("compat")["id"]
        api.prompt_async(sid, "hello", system="SYSTEM-BLOCK",
                         model=("aisix", "auto"))
        msgs = wait_for(
            lambda: (lambda m: m if assistant_text(m) else None)(
                api.messages(sid)))
        assert "stub-ok" in assistant_text(msgs)
        assert [m["info"]["role"] for m in msgs].count("assistant") == 1
        if version.startswith("2"):
            users = [m for m in msgs if m["info"]["role"] == "user"]
            assert users[0]["parts"][0]["text"].startswith(
                "SYSTEM-BLOCK\n\n")
            assert serve.state.models[sid] == {"providerID": "aisix",
                                               "id": "auto"}
            api.prompt_async(sid, "again", system="SYSTEM-BLOCK",
                             model=("aisix", "auto"))
            wait_for(lambda: [m for m in api.messages(sid)
                              if m["info"]["role"] == "assistant"].__len__()
                     >= 2)
            users = [m for m in api.messages(sid)
                     if m["info"]["role"] == "user"]
            assert not users[-1]["parts"][0]["text"].startswith(
                "SYSTEM-BLOCK")
        assert api.abort(sid) is not None
        assert sid in serve.state.aborted
    finally:
        brain.SERVER_PASSWORD = old_password
        serve.stop()


def test_missing_password_fails_clearly_without_crashing(
        monkeypatch, capsys):
    serve = StubServe(api_version="2.3.0", password="pw").start()
    monkeypatch.setattr(brain, "SERVER_PASSWORD", "")
    monkeypatch.setattr(brain, "OPENCODE_VERSION", "2.3.0")
    try:
        assert brain.Serve(serve.url, version="2.3.0").healthy() is False
        assert "AUTH_MISSING" in capsys.readouterr().out
        api = brain.Api(serve.url, version="2.3.0")
        with pytest.raises(oc_compat.AuthError) as err:
            api.create_session("nope")
        assert "OPENCODE_SERVER_PASSWORD" in str(err.value)
        with pytest.raises(RuntimeError) as err:
            brain.Serve.spawned()
        assert "OPENCODE_SERVER_PASSWORD" in str(err.value)
    finally:
        serve.stop()
