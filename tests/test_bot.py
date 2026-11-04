import socket
import threading
import time
from unittest import mock

import bot


def test_split_say_short_is_verbatim():
    with mock.patch.object(bot, "send_raw") as sr:
        bot.split_say("#c", "  keep   spacing  ")
    sr.assert_called_once_with("PRIVMSG #c :  keep   spacing  ", None)


def test_split_say_wraps_long_lines():
    text = " ".join(["word"] * 200)  # well past MAX_BODY
    with mock.patch.object(bot, "send_raw") as sr:
        bot.split_say("#c", text)
    assert sr.call_count > 1
    assert all(c.args[0].startswith("PRIVMSG #c :") for c in sr.call_args_list)


def test_process_cmd_say():
    with mock.patch.object(bot, "send_raw") as sr:
        bot.process_cmd("SAY #c hi there")
    sr.assert_called_once_with("PRIVMSG #c :hi there", None)


def test_process_cmd_act_and_notice():
    with mock.patch.object(bot, "send_raw") as sr:
        bot.process_cmd("ACT #c waves")
        bot.process_cmd("NOTICE nick hello")
    assert sr.call_args_list[0].args[0] == "PRIVMSG #c :\x01ACTION waves\x01"
    assert sr.call_args_list[1].args[0] == "NOTICE nick :hello"


def test_process_cmd_origin_tag_rides_the_line():
    # The vjt sidecar contract: `VERB:<tag>` stamps the origin into the log
    # and the wire, and must never degrade into an unknown verb (the simplify
    # pass turned every `SAY:impiccato` from the existing sidecars into
    # CMD_ERROR — that regression is what this test pins).
    with mock.patch.object(bot, "send_raw") as sr:
        bot.process_cmd("SAY:impiccato #c hi")
    assert sr.call_args.args == ("PRIVMSG #c :hi", "impiccato")


def test_process_cmd_bad_origin_tag_is_rejected_loudly():
    with mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit") as em:
        bot.process_cmd("SAY:bad.tag! #c hi")
    sr.assert_not_called()
    assert any(c.args[:2] == ("CMD_ERROR", "bad-origin-tag")
               for c in em.call_args_list)


def test_process_cmd_dcc_needs_nick_and_file():
    with mock.patch.object(bot, "send_raw"), \
            mock.patch.object(bot, "emit") as em:
        bot.process_cmd("DCC")
    assert any("DCC needs <nick> <file> [name]" in str(c.args)
               for c in em.call_args_list)


def test_dcc_send_transfers_the_file_over_loopback(tmp_path):
    # Real offer + real transfer on 127.0.0.1: the CTCP announces an ephemeral
    # port, the test plays the peer, and the bytes must arrive intact.
    f = tmp_path / "artifact.txt"
    payload = b"DCC payload \xe2\x9c\x93" * 5000  # > one 64 KiB-ish push
    f.write_bytes(payload)
    old = (bot.DCC_DIR, bot.DCC_BIND4, bot.DCC_ADDR4, bot.BIND)
    try:
        bot.DCC_DIR = str(tmp_path)
        bot.DCC_BIND4 = "127.0.0.1"
        bot.DCC_ADDR4 = "127.0.0.1"
        bot.BIND = "127.0.0.1"
        with mock.patch.object(bot, "send_raw") as sr:
            bot.dcc_send("peer", "artifact.txt")
        ctcp = sr.call_args.args[0]
        assert "\x01DCC SEND artifact.txt " in ctcp
        _pfx, port, size = ctcp.rstrip("\x01").split(" ")[-3:]
        s = __import__("socket").create_connection(("127.0.0.1", int(port)),
                                                   timeout=10)
        got = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
            s.sendall(len(got).to_bytes(4, "big"))   # DCC ACK
        s.close()
        assert got == payload
        assert int(size) == len(payload)
    finally:
        bot.DCC_DIR, bot.DCC_BIND4, bot.DCC_ADDR4, bot.BIND = old


def test_process_cmd_unknown_verb_emits_error():
    with mock.patch.object(bot, "send_raw"), \
            mock.patch.object(bot, "emit") as em:
        bot.process_cmd("BOGUS whatever")
    assert any(c.args[0] == "CMD_ERROR" for c in em.call_args_list)


def test_trust_listing_and_host_glob():
    old = bot.trust_rules
    try:
        bot.trust_rules = [("vjt", "*.openssl.it")]
        assert bot.is_trust_listed("VJT")
        assert bot.host_matches("vjt", "user.openssl.it")
        assert not bot.host_matches("vjt", "evil.example.com")
        assert not bot.is_trust_listed("stranger")
    finally:
        bot.trust_rules = old


def test_trust_check_requires_whois_verification():
    old_rules, old_verified = bot.trust_rules, set(bot.verified)
    try:
        bot.trust_rules = [("vjt", "*.openssl.it")]
        bot.verified = set()
        with mock.patch.object(bot, "send_raw"), \
                mock.patch.object(bot, "emit"):
            trusted, reason = bot.trust_check("vjt", "u.openssl.it")
        assert not trusted and reason == "pending-whois"
        bot.verified = {"vjt"}
        with mock.patch.object(bot, "send_raw"), \
                mock.patch.object(bot, "emit"):
            trusted, reason = bot.trust_check("vjt", "u.openssl.it")
        assert trusted and reason == "ok"
    finally:
        bot.trust_rules, bot.verified = old_rules, old_verified


def test_dm_blocked_only_for_unlisted():
    old = bot.trust_rules
    try:
        bot.trust_rules = [("vjt", "*.openssl.it")]
        assert bot.dm_blocked("stranger")
        assert not bot.dm_blocked("vjt")
        assert not bot.dm_blocked(bot.NICK)
        assert not bot.dm_blocked("NickServ")
    finally:
        bot.trust_rules = old


# --- outbound rate limit (issue #7: Excess Flood) ---------------------------
# Since the sender-thread rework, send_raw only enqueues: one sender thread
# owns the socket write side and drains a priority lane (RATE_BYPASS verbs)
# ahead of a bounded, bucket-paced normal lane. These tests run the REAL
# sender against a mock/socketpair and assert on the wire writes.

def _fresh_bucket(rate, burst):
    old = (bot.SEND_RATE, bot.SEND_BURST, bot._rate_tokens, bot._rate_ts)
    bot.SEND_RATE = float(rate)
    bot.SEND_BURST = float(burst)
    bot._rate_tokens = float(burst)
    bot._rate_ts = time.monotonic()
    return old


def _restore_bucket(old):
    bot.SEND_RATE, bot.SEND_BURST, bot._rate_tokens, bot._rate_ts = old


def _reset_queues():
    with bot._q_cond:
        bot._q_pri.clear()
        bot._q_norm.clear()
        bot._q_dropped = 0
        bot._send_stop = None


class _Sender:
    """Runs a real sender thread against the supplied socket; restores
    bot.sock and stops the thread (give-up deadline = now) on exit."""

    def __init__(self, sock):
        self.sock = sock
        self.old_sock = bot.sock
        bot.sock = sock
        _reset_queues()
        bot.start_sender()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        with bot._q_cond:
            bot._send_stop = time.monotonic()
            bot._q_cond.notify_all()
        bot.sender_thread.join(5)
        assert not bot.sender_thread.is_alive()
        bot._send_stop = None
        bot.sock = self.old_sock
        _reset_queues()


def _stamps(sock):
    """Make sendall record the wire-write times."""
    stamps = []
    sock.sendall = mock.Mock(side_effect=lambda b: stamps.append(time.monotonic()))
    return stamps


def _wait_sendall(sock, n, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock.sendall.call_count >= n:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"sender wrote {sock.sendall.call_count}/{n} lines in {timeout}s")


def test_send_raw_paces_normal_verbs_at_the_bucket_rate():
    old = _fresh_bucket(rate=20, burst=2)
    try:
        s = mock.Mock()
        stamps = _stamps(s)
        with _Sender(s), mock.patch.object(bot, "emit"):
            for _ in range(7):
                bot.send_raw("PRIVMSG #c :hi")
            _wait_sendall(s, 7)
        assert s.sendall.call_count == 7     # pacing never drops a line
        # 2 free from the burst, 5 paced at 20/s -> ~0.25s first-to-last.
        assert stamps[-1] - stamps[0] >= (7 - 2) / 20 - 0.05
        assert stamps[-1] - stamps[0] <= 7 / 20 + 0.5
    finally:
        _restore_bucket(old)


def test_send_raw_latency_critical_verbs_bypass_the_bucket():
    old = _fresh_bucket(rate=5, burst=1)
    try:
        s = mock.Mock()
        stamps = _stamps(s)
        with _Sender(s), mock.patch.object(bot, "emit"):
            t0 = time.monotonic()
            for line in ("PONG :keepalive", "QUIT :bye", "CAP LS 302",
                         "PASS pw", "NICK ocbot", "USER c 0 * :real"):
                bot.send_raw(line)
            _wait_sendall(s, 6)
            elapsed = time.monotonic() - t0
        # Paced, six sends on a 5/s bucket with burst 1 would cost ~1s; the
        # priority lane writes them back to back (and never paces them).
        assert elapsed < 0.15
    finally:
        _restore_bucket(old)


def test_bucket_blocks_then_refills():
    old = _fresh_bucket(rate=50, burst=1)
    try:
        s = mock.Mock()
        stamps = _stamps(s)
        with _Sender(s), mock.patch.object(bot, "emit") as em:
            bot.send_raw("PRIVMSG #c :a")            # drains the bucket
            bot.send_raw("PRIVMSG #c :b")            # waits ~1/50 s
            bot.send_raw("PRIVMSG #c :c")            # rides the refill
            _wait_sendall(s, 3)
        assert s.sendall.call_count == 3
        assert stamps[1] - stamps[0] >= 0.01
        assert stamps[2] - stamps[1] < 0.05
        assert any(c.args[0] == "RATE_WAIT" for c in em.call_args_list)
    finally:
        _restore_bucket(old)


def test_concurrent_senders_do_not_overshoot_the_burst():
    old = _fresh_bucket(rate=20, burst=4)
    try:
        s = mock.Mock()
        stamps = _stamps(s)
        barrier = threading.Barrier(8, timeout=5)
        with _Sender(s), mock.patch.object(bot, "emit"):
            def _one(i):
                barrier.wait()
                bot.send_raw(f"PRIVMSG #c :m{i}")
            threads = [threading.Thread(target=_one, args=(i,))
                       for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            _wait_sendall(s, 8)
        assert s.sendall.call_count == 8
        # 4 burst-free + 4 paced at 20/s: wire writes all inside ~0.15s would
        # mean the bucket let more than the burst through under contention.
        assert stamps[-1] - stamps[0] >= (8 - 4) / 20 - 0.05
    finally:
        _restore_bucket(old)


# --- sender thread: the reader must never block (PR review regression) ------

def test_sender_thread_does_not_block_the_reader(tmp_path):
    # THE regression from the PR review: handle_server_line runs on the
    # reader thread, and the old in-line send_raw slept on the bucket there —
    # 56 nicks froze sock.recv() for 12s, past the ping timeout. Now a
    # backlog the sender needs >5s to clear (burst 4 + 10 paced at 2/s) must
    # not hold the reader for more than a blink, and the PONG must reach the
    # socket while the backlog is still queued.
    old = _fresh_bucket(rate=2, burst=4)
    old_log, old_events = bot.LOG, bot.EVENTS_FILE
    bot.LOG = str(tmp_path / "wire.log")
    bot.EVENTS_FILE = str(tmp_path / "events.log")
    try:
        a, b = socket.socketpair()
        b.settimeout(5)
        with _Sender(a), mock.patch.object(bot, "emit"):
            for i in range(14):
                bot.send_raw(f"PRIVMSG #c :backlog-{i}")
            time.sleep(0.3)      # sender is now slicing on a parked line
            t0 = time.monotonic()
            bot.handle_server_line("PING :keepalive")
            call = time.monotonic() - t0
            got = b""
            while b"PONG :keepalive" not in got:
                got += b.recv(4096)
                assert time.monotonic() - t0 < 0.25, got
        assert call < 0.25
    finally:
        a.close()
        b.close()
        bot.LOG, bot.EVENTS_FILE = old_log, old_events
        _restore_bucket(old)


def test_send_raw_returns_immediately():
    # The caller's contract: with a dry bucket, enqueue-and-return. The old
    # code slept right here, freezing the reader and the FIFO thread.
    old = _fresh_bucket(rate=0.5, burst=0)
    try:
        _reset_queues()
        with mock.patch.object(bot, "sock") as s:
            t0 = time.monotonic()
            for i in range(20):
                bot.send_raw(f"PRIVMSG #c :m{i}")
            elapsed = time.monotonic() - t0
        assert elapsed < 0.1
        assert not s.sendall.called            # never touched the socket
        with bot._q_cond:
            assert len(bot._q_norm) == 20      # all queued, none dropped
    finally:
        _restore_bucket(old)
        _reset_queues()


def test_priority_verbs_jump_the_backlog(tmp_path):
    # 100 paced PRIVMSGs (burst 1, ~50s of backlog), then QUIT and PONG: both
    # priority verbs must hit the socket long before the backlog drains.
    old = _fresh_bucket(rate=2, burst=1)
    try:
        _reset_queues()
        a, b = socket.socketpair()
        b.settimeout(10)
        got = b""
        try:
            with _Sender(a), mock.patch.object(bot, "emit"):
                for i in range(100):
                    bot.send_raw(f"PRIVMSG #c :m{i}")
                time.sleep(0.3)      # sender is now slicing on a parked line
                bot.send_raw("QUIT :bye")
                bot.send_raw("PONG :x")
                while b"PONG :x" not in got or b"QUIT :bye" not in got:
                    chunk = b.recv(4096)
                    if not chunk:
                        break
                    got += chunk
        finally:
            a.close()
            b.close()
        assert b"PONG :x" in got and b"QUIT :bye" in got
        # Only the single burst token could have left with them.
        assert got.count(b"PRIVMSG #c :m") < 5
    finally:
        _restore_bucket(old)
        _reset_queues()


def test_queue_bound_drops_oldest_chat_line():
    # Overflow sacrifices the OLDEST chat line (PRIVMSG/NOTICE) and counts
    # it; JOIN/WHOIS/MODE survive. A full lane holding only protocol lines
    # exceeds the bound rather than dropping one.
    old_q = bot.SEND_QUEUE
    bot.SEND_QUEUE = 4
    try:
        _reset_queues()
        with mock.patch.object(bot, "emit") as em:
            for m in "abc":
                bot.send_raw(f"PRIVMSG #c :{m}")
            bot.send_raw("JOIN #c")
            bot.send_raw("PRIVMSG #c :d")        # full -> drops "a"
            bot.send_raw("WHOIS vjt")            # -> drops "b"
            bot.send_raw("MODE #c +m")           # -> drops "c"
            bot.send_raw("PRIVMSG #c :e")        # -> drops "d"
        with bot._q_cond:
            lines = [l for l, _ in bot._q_norm]
        assert lines == ["JOIN #c", "WHOIS vjt", "MODE #c +m", "PRIVMSG #c :e"]
        drops = [c for c in em.call_args_list if c.args[0] == "QUEUE_DROP"]
        assert [c.args[1] for c in drops] == [1, 2, 3, 4]
        _reset_queues()
        with mock.patch.object(bot, "emit") as em:
            for line in ("JOIN #a", "PART #a", "WHOIS x", "MODE #a +m"):
                bot.send_raw(line)
            bot.send_raw("JOIN #b")              # full, no chat to sacrifice
        with bot._q_cond:
            assert [l for l, _ in bot._q_norm] == [
                "JOIN #a", "PART #a", "WHOIS x", "MODE #a +m", "JOIN #b"]
        assert not any(c.args[0] == "QUEUE_DROP" for c in em.call_args_list)
    finally:
        bot.SEND_QUEUE = old_q
        _reset_queues()


def test_quit_is_flushed_on_shutdown():
    # QUIT rides the priority lane and shutdown_outbound always flushes that
    # lane in full before closing; the paced backlog gets only the bounded
    # drain window, then is given up on.
    old = _fresh_bucket(rate=1, burst=1)
    old_drain = bot.SENDER_DRAIN_SECS
    bot.SENDER_DRAIN_SECS = 0.3
    try:
        _reset_queues()
        a, b = socket.socketpair()
        b.settimeout(10)
        got = b""
        try:
            with _Sender(a), mock.patch.object(bot, "emit"):
                for i in range(5):
                    bot.send_raw(f"PRIVMSG #c :m{i}")    # ~5s of paced backlog
                bot.send_raw("QUIT :bye")
                bot.shutdown_outbound()
            while True:
                chunk = b.recv(4096)
                if not chunk:
                    break                                # sock closed: EOF
                got += chunk
        except socket.timeout:
            pass
        finally:
            a.close()
            b.close()
        assert b"QUIT :bye" in got
        assert b"PRIVMSG #c :m4" not in got              # gave up, not drained
    finally:
        bot.SENDER_DRAIN_SECS = old_drain
        _restore_bucket(old)
        _reset_queues()


# --- IRCv3 caps and account discovery ---------------------------------------

def test_registration_sends_cap_ls_before_nick_user():
    with mock.patch.object(bot, "send_raw") as sr:
        bot.send_registration()
    lines = [c.args[0] for c in sr.call_args_list]
    assert lines[0] == "CAP LS 302"
    assert lines[1] == f"NICK {bot.NICK}"
    assert lines[2].startswith("USER ")


def test_001_requests_the_account_caps_and_ends_negotiation():
    old_pass, old_fired = bot.NICKSERV_PASS, bot.startup_fired
    bot.NICKSERV_PASS = "pw"    # keep the no-pass startup thread out of it
    try:
        with mock.patch.object(bot, "send_raw") as sr, \
                mock.patch.object(bot, "emit"):
            bot.handle_server_line(":mock 001 ocbot :welcome")
        lines = [c.args[0] for c in sr.call_args_list]
        req = next(l for l in lines if l.startswith("CAP REQ :"))
        for cap in bot.CAP_REQ:
            assert cap in req
        assert "CAP END" in lines
    finally:
        bot.NICKSERV_PASS, bot.startup_fired = old_pass, old_fired


def test_cap_ack_records_caps_and_nak_keeps_the_connection():
    old_caps = set(bot.caps_ack)
    try:
        with mock.patch.object(bot, "emit") as em:
            bot.handle_server_line(
                ":mock CAP ocbot ACK :extended-join userhost-in-names")
            assert {"extended-join", "userhost-in-names"} <= bot.caps_ack
            bot.handle_server_line(":mock CAP ocbot NAK :multi-prefix")
            assert "multi-prefix" not in bot.caps_ack
            assert any(c.args[:2] == ("CAP_NAK", "multi-prefix")
                       for c in em.call_args_list)
        # A NAK must not abort: the next line still processes normally.
        with mock.patch.object(bot, "emit") as em:
            bot.handle_server_line(":nick!u@h PRIVMSG #c :still here")
        assert any(c.args[0] == "MSG" for c in em.call_args_list)
    finally:
        bot.caps_ack = old_caps


def test_extended_join_yields_the_account_without_whois():
    old = (set(bot.caps_ack), dict(bot.accounts), set(bot.verified))
    try:
        bot.caps_ack = {"extended-join", "account-tag"}
        bot.verified = set()
        with mock.patch.object(bot, "send_raw") as sr, \
                mock.patch.object(bot, "emit"):
            bot.handle_server_line(
                ":vjt!v@u.openssl.it JOIN #milliways vjt :Valerio")
            assert bot.accounts.get("vjt") == "vjt"
            assert "vjt" in bot.verified
            assert sr.call_count == 0
            # A logged-out join (`*` account) must clear the stale knowledge.
            bot.handle_server_line(":vjt!v@u.openssl.it JOIN #milliways * :V")
            assert "vjt" not in bot.accounts
            assert "vjt" not in bot.verified
        assert "vjt" not in bot.accounts
        assert "vjt" not in bot.verified
    finally:
        bot.caps_ack, bot.accounts, bot.verified = old


def test_userhost_in_names_353_records_hosts():
    old = dict(bot.known_hosts)
    try:
        with mock.patch.object(bot, "emit"):
            bot.handle_server_line(
                ":mock 353 ocbot = #milliways "
                ":@vjt!v@openssl.it +alice!a@h2 bob")
        assert bot.known_hosts.get("vjt") == "openssl.it"
        assert bot.known_hosts.get("alice") == "h2"
        assert "bob" not in bot.known_hosts    # bare entry, cap not ACKed
    finally:
        bot.known_hosts = old


def test_account_notify_updates_and_resets_live():
    old = (dict(bot.accounts), set(bot.verified))
    try:
        bot.accounts, bot.verified = {}, set()
        with mock.patch.object(bot, "emit"):
            bot.handle_server_line(":vjt!v@openssl.it ACCOUNT vjt-new")
            assert bot.accounts.get("vjt") == "vjt-new"
            assert "vjt" in bot.verified
            bot.handle_server_line(":vjt!v@openssl.it ACCOUNT *")
        assert "vjt" not in bot.accounts
        assert "vjt" not in bot.verified
    finally:
        bot.accounts, bot.verified = old


def test_trusted_nick_with_known_account_needs_zero_whois():
    old = (bot.trust_rules, set(bot.verified), dict(bot.accounts),
           set(bot.caps_ack), set(bot.whois_pending))
    try:
        bot.trust_rules = [("vjt", "*.openssl.it")]
        bot.caps_ack = {"account-tag", "extended-join"}
        bot.verified, bot.accounts, bot.whois_pending = set(), {}, set()
        with mock.patch.object(bot, "send_raw") as sr, \
                mock.patch.object(bot, "emit") as em:
            # DM carrying the account-tag: trust must resolve inline.
            bot.handle_server_line(
                "@account=vjt :vjt!v@u.openssl.it PRIVMSG ocbot :ciao")
        assert any(c.args[0] == "MSG" and "TRUSTED" in c.args[1]
                   for c in em.call_args_list)
        assert not any(c.args[0].startswith("WHOIS")
                       for c in sr.call_args_list)
        assert sr.call_count == 0
    finally:
        (bot.trust_rules, bot.verified, bot.accounts,
         bot.caps_ack, bot.whois_pending) = old


# --- secret masking at the log write points ---------------------------------

def test_log_masks_identify_and_ghost_even_without_the_env_value(tmp_path):
    old_log, old_pass, old_env = bot.LOG, bot.NICKSERV_PASS, bot.EVENTS_FILE
    bot.LOG = str(tmp_path / "wire.log")
    bot.EVENTS_FILE = str(tmp_path / "events.log")
    try:
        old_bucket = _fresh_bucket(rate=1000, burst=100)
        try:
            snd = mock.Mock()
            with _Sender(snd), mock.patch.object(bot, "emit"):
                # Env value KNOWN: the value replace fires...
                bot.NICKSERV_PASS = "hunter2"
                bot.send_raw("PRIVMSG NickServ :IDENTIFY hunter2")
                bot.send_raw("PRIVMSG NickServ :GHOST ocbot hunter2")
                # ...and with the env value MISSING, the pattern mask must
                # still catch it (stale .env, rotated secret via FIFO RAW).
                bot.NICKSERV_PASS = ""
                bot.send_raw("PRIVMSG NickServ :IDENTIFY hunter2")
                _wait_sendall(snd, 3)    # the ">" log line is written by the sender
        finally:
            _restore_bucket(old_bucket)
        body = (tmp_path / "wire.log").read_text()
        assert "hunter2" not in body
        assert "IDENTIFY ********" in body
        assert "GHOST ocbot ********" in body
        bot.emit("NS_TEST", "PRIVMSG NickServ :IDENTIFY hunter2")
        events = (tmp_path / "events.log").read_text()
        assert "hunter2" not in events
    finally:
        bot.LOG, bot.NICKSERV_PASS, bot.EVENTS_FILE = old_log, old_pass, old_env
