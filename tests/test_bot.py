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

def _fresh_bucket(rate, burst):
    old = (bot.SEND_RATE, bot.SEND_BURST, bot._rate_tokens, bot._rate_ts)
    bot.SEND_RATE = float(rate)
    bot.SEND_BURST = float(burst)
    bot._rate_tokens = float(burst)
    bot._rate_ts = time.monotonic()
    return old


def _restore_bucket(old):
    bot.SEND_RATE, bot.SEND_BURST, bot._rate_tokens, bot._rate_ts = old


def test_send_raw_paces_normal_verbs_at_the_bucket_rate():
    old = _fresh_bucket(rate=20, burst=2)
    try:
        with mock.patch.object(bot, "sock") as s:
            t0 = time.monotonic()
            for _ in range(7):
                bot.send_raw("PRIVMSG #c :hi")
            elapsed = time.monotonic() - t0
        assert s.sendall.call_count == 7     # pacing never drops a line
        # 2 free from the burst, 5 paced at 20/s -> ~0.25s, bounded by N/rate.
        assert elapsed >= (7 - 2) / 20 - 0.05
        assert elapsed <= 7 / 20 + 0.5
    finally:
        _restore_bucket(old)


def test_send_raw_latency_critical_verbs_bypass_the_bucket():
    old = _fresh_bucket(rate=5, burst=1)
    try:
        with mock.patch.object(bot, "sock"):
            t0 = time.monotonic()
            for line in ("PONG :keepalive", "QUIT :bye", "CAP LS 302",
                         "PASS pw", "NICK ocbot", "USER c 0 * :real"):
                bot.send_raw(line)
            elapsed = time.monotonic() - t0
        # Paced, six sends on a 5/s bucket with burst 1 would cost ~1s; the
        # bypass sends them back to back.
        assert elapsed < 0.15
    finally:
        _restore_bucket(old)


def test_bucket_blocks_then_refills():
    old = _fresh_bucket(rate=50, burst=1)
    try:
        with mock.patch.object(bot, "sock"), \
                mock.patch.object(bot, "emit") as em:
            bot.send_raw("PRIVMSG #c :a")            # drains the bucket
            t0 = time.monotonic()
            bot.send_raw("PRIVMSG #c :b")            # must wait ~1/50 s
            assert time.monotonic() - t0 >= 0.01
            assert any(c.args[0] == "RATE_WAIT" for c in em.call_args_list)
            time.sleep(0.06)                         # >= one token at 50/s
            t0 = time.monotonic()
            bot.send_raw("PRIVMSG #c :c")            # refilled: immediate
            assert time.monotonic() - t0 < 0.03
    finally:
        _restore_bucket(old)


def test_concurrent_senders_do_not_overshoot_the_burst():
    old = _fresh_bucket(rate=20, burst=4)
    try:
        barrier = threading.Barrier(8, timeout=5)
        with mock.patch.object(bot, "sock") as s:
            def _one(i):
                barrier.wait()
                bot.send_raw(f"PRIVMSG #c :m{i}")
            threads = [threading.Thread(target=_one, args=(i,))
                       for i in range(8)]
            t0 = time.monotonic()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.monotonic() - t0
        assert s.sendall.call_count == 8
        # 4 burst-free + 4 paced at 20/s: finishing under ~0.15s would mean
        # the bucket let more than the burst through under contention.
        assert elapsed >= (8 - 4) / 20 - 0.05
    finally:
        _restore_bucket(old)


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
            with mock.patch.object(bot, "sock"):
                # Env value KNOWN: the value replace fires...
                bot.NICKSERV_PASS = "hunter2"
                bot.send_raw("PRIVMSG NickServ :IDENTIFY hunter2")
                bot.send_raw("PRIVMSG NickServ :GHOST ocbot hunter2")
                # ...and with the env value MISSING, the pattern mask must
                # still catch it (stale .env, rotated secret via FIFO RAW).
                bot.NICKSERV_PASS = ""
                bot.send_raw("PRIVMSG NickServ :IDENTIFY hunter2")
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
