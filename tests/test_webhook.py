"""webhook.py tests — a real ThreadingHTTPServer on loopback: token auth,
delivery as a policy-parseable MSG event, hot-reloaded config, and the
never-kill-the-bot guarantees."""
import json
import os
import time
import urllib.error
import urllib.request

import pytest

import policy
import webhook


def _post(url, data, token=None):
    headers = {}
    if token is not None:
        headers["X-Webhook-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture()
def server(tmp_path):
    events = str(tmp_path / "events.freebnc.log")
    cfg = str(tmp_path / "webhooks.json")
    with open(cfg, "w") as f:
        json.dump({"listen": "127.0.0.1:0",
                   "hooks": {"alert": {"token": "s3cr3t", "net": "freebnc",
                                       "target": "#hermes",
                                       "from": "webhook",
                                       "trust": "TRUSTED"}}}, f)
    srv = webhook.start(config_path=cfg, net_events=lambda _: events)
    assert srv is not None
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield {"base": base, "events": events, "cfg": cfg}
    finally:
        srv.shutdown()
        srv.server_close()


def _lines(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


def test_healthz(server):
    with urllib.request.urlopen(server["base"] + "/healthz",
                                timeout=10) as r:
        assert r.status == 200
        assert r.read() == b"ok"


def test_unknown_paths_are_404(server):
    try:
        urllib.request.urlopen(server["base"] + "/anything", timeout=10)
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_good_token_delivers_one_parseable_event(server):
    code, _ = _post(server["base"] + "/hook/alert",
                    json.dumps({"text": "disk almost full"}).encode(),
                    token="s3cr3t")
    assert code == 200
    deadline = time.monotonic() + 5
    while not _lines(server["events"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    lines = _lines(server["events"])
    assert len(lines) == 1
    ev = policy.parse_event(lines[0])
    assert ev["kind"] == "MSG"
    assert ev["trust"] == "TRUSTED"
    assert ev["to"] == "#hermes"
    assert ev["from"] == "webhook"
    assert ev["host"] == "webhook"
    assert ev["body"] == "disk almost full"


def test_bad_token_is_403_and_writes_nothing(server):
    code, _ = _post(server["base"] + "/hook/alert", b'{"text": "x"}',
                    token="wrong")
    assert code == 403
    time.sleep(0.2)
    assert _lines(server["events"]) == []


def test_missing_token_is_403(server):
    code, _ = _post(server["base"] + "/hook/alert", b'{"text": "x"}')
    assert code == 403
    time.sleep(0.2)
    assert _lines(server["events"]) == []


def test_unknown_hook_is_flat_403(server):
    # Same 403 as a bad token: no oracle for probing hook names.
    code, _ = _post(server["base"] + "/hook/nope", b'{"text": "x"}',
                    token="s3cr3t")
    assert code == 403
    time.sleep(0.2)
    assert _lines(server["events"]) == []


def test_raw_text_body_is_delivered(server):
    code, _ = _post(server["base"] + "/hook/alert", b"plain text body",
                    token="s3cr3t")
    assert code == 200
    deadline = time.monotonic() + 5
    while not _lines(server["events"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    ev = policy.parse_event(_lines(server["events"])[0])
    assert ev["body"] == "plain text body"


def test_newlines_collapse_to_one_irc_line(server):
    code, _ = _post(server["base"] + "/hook/alert",
                    b'{"text": "one\\ntwo\\r\\nthree"}', token="s3cr3t")
    assert code == 200
    deadline = time.monotonic() + 5
    while not _lines(server["events"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    lines = _lines(server["events"])
    assert len(lines) == 1
    ev = policy.parse_event(lines[0])
    assert ev["body"] == "one two three"


def test_oversized_text_is_truncated_at_4000_bytes(server):
    long = "x" * 5000
    code, _ = _post(server["base"] + "/hook/alert",
                    json.dumps({"text": long}).encode(), token="s3cr3t")
    assert code == 200
    deadline = time.monotonic() + 5
    while not _lines(server["events"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    ev = policy.parse_event(_lines(server["events"])[0])
    assert len(ev["body"]) == 4000


def test_utf8_truncation_never_splits_a_character(server):
    text = "ä" * 3000                        # 6000 bytes of two-byte chars
    code, _ = _post(server["base"] + "/hook/alert",
                    json.dumps({"text": text}).encode(), token="s3cr3t")
    assert code == 200
    deadline = time.monotonic() + 5
    while not _lines(server["events"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    ev = policy.parse_event(_lines(server["events"])[0])
    assert ev["body"].encode("utf-8") == ("ä" * 2000).encode("utf-8")


def test_config_hot_reloads_on_mtime(server, tmp_path):
    cfg = json.load(open(server["cfg"]))
    cfg["hooks"]["second"] = {"token": "t2", "net": "freebnc",
                              "target": "#ops", "from": "cron",
                              "trust": "TRUSTED"}
    with open(server["cfg"], "w") as f:
        json.dump(cfg, f)
    t = os.stat(server["cfg"]).st_mtime_ns - 1_000_000_000
    os.utime(server["cfg"], ns=(t, t))       # 1s into the past: distinct
                                             # under any timestamp granularity
    code, _ = _post(server["base"] + "/hook/second",
                    json.dumps({"text": "from the new hook"}).encode(),
                    token="t2")
    assert code == 200
    deadline = time.monotonic() + 5
    lines = []
    while time.monotonic() < deadline:
        lines = [ln for ln in _lines(server["events"]) if "cron" in ln]
        if lines:
            break
        time.sleep(0.05)
    ev = policy.parse_event(lines[0])
    assert ev["body"] == "from the new hook"
    assert ev["to"] == "#ops"


# --- lifecycle -----------------------------------------------------------------
def test_no_listen_address_leaves_the_listener_off(tmp_path):
    cfg = str(tmp_path / "webhooks.json")
    with open(cfg, "w") as f:
        json.dump({"hooks": {}}, f)
    assert webhook.start(config_path=cfg, net_events=lambda _: "/x") is None


def test_missing_config_leaves_the_listener_off(tmp_path):
    assert webhook.start(config_path=str(tmp_path / "nope.json"),
                         net_events=lambda _: "/x") is None


def test_bot_webhook_env_zero_disables(tmp_path):
    cfg = str(tmp_path / "webhooks.json")
    with open(cfg, "w") as f:
        json.dump({"listen": "127.0.0.1:0"}, f)
    assert webhook.start(config_path=cfg, net_events=lambda _: "/x",
                         enabled=False) is None


def test_bind_failure_is_logged_not_raised(tmp_path):
    # Two servers cannot share a port: the second start() must return None
    # and log, never raise into the bot's main().
    cfg = str(tmp_path / "webhooks.json")
    with open(cfg, "w") as f:
        json.dump({"listen": "127.0.0.1:0"}, f)
    first = webhook.start(config_path=cfg, net_events=lambda _: "/x")
    try:
        port = first.server_address[1]
        cfg2 = str(tmp_path / "w2.json")
        with open(cfg2, "w") as f:
            json.dump({"listen": f"127.0.0.1:{port}"}, f)
        logs = []
        assert webhook.start(config_path=cfg2, net_events=lambda _: "/x",
                             log=lambda *a: logs.append(a)) is None
        assert any(a[0] == "WEBHOOK_BIND_ERROR" for a in logs)
    finally:
        first.shutdown()
        first.server_close()


def test_clean_text_unit():
    assert webhook.clean_text("a\nb\r\nc\rd") == "a b c d"
    assert webhook.clean_text(None) == ""
    assert len(webhook.clean_text("y" * 9000)) == 4000
