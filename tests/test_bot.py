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


# `311 <ournick> <nick> <user> <host> * :realname` — IRCnet's only WHOIS
# reply (production: 80 x 311, 0 x 307/330).
IRCNET_311 = (":openirc.snt.utwente.nl 311 mammt katia ~katia "
              "katia.users.ircnet.com * :katia")


def test_311_verifies_pending_nick_when_flag_on():
    old = (bot.trust_rules, set(bot.verified), set(bot.whois_pending),
           bot.TRUST_VIA_311)
    try:
        bot.trust_rules = [("katia", "*.users.ircnet.com")]
        bot.verified = set()
        bot.whois_pending = {"katia"}
        bot.TRUST_VIA_311 = True
        with mock.patch.object(bot, "log"), \
                mock.patch.object(bot, "send_raw"), \
                mock.patch.object(bot, "emit") as em:
            bot.handle_server_line(IRCNET_311)
        assert "katia" in bot.verified
        assert "katia" not in bot.whois_pending
        assert any(c.args == ("VERIFIED", "katia", "311")
                   for c in em.call_args_list)
    finally:
        (bot.trust_rules, bot.verified, bot.whois_pending,
         bot.TRUST_VIA_311) = old


def test_311_host_mismatch_denies():
    old = (bot.trust_rules, set(bot.verified), set(bot.whois_pending),
           bot.TRUST_VIA_311)
    try:
        bot.trust_rules = [("katia", "*.users.ircnet.com")]
        bot.verified = set()
        bot.whois_pending = {"katia"}
        bot.TRUST_VIA_311 = True
        with mock.patch.object(bot, "log"), \
                mock.patch.object(bot, "send_raw"), \
                mock.patch.object(bot, "emit") as em:
            bot.handle_server_line(
                ":openirc.snt.utwente.nl 311 mammt katia ~katia "
                "evil.example.com * :katia")
        assert "katia" not in bot.verified
        assert "katia" in bot.whois_pending    # 318 still owns the cleanup
        assert any(c.args == ("TRUST_DENIED", "katia", "evil.example.com",
                              "311-host-mismatch")
                   for c in em.call_args_list)
    finally:
        (bot.trust_rules, bot.verified, bot.whois_pending,
         bot.TRUST_VIA_311) = old


def test_311_unsolicited_verifies_nothing():
    old = (bot.trust_rules, set(bot.verified), set(bot.whois_pending),
           bot.TRUST_VIA_311)
    try:
        bot.trust_rules = [("katia", "*.users.ircnet.com")]
        bot.verified = set()
        bot.whois_pending = set()      # we never WHOIS'd: not proof
        bot.TRUST_VIA_311 = True
        with mock.patch.object(bot, "log"), \
                mock.patch.object(bot, "send_raw"), \
                mock.patch.object(bot, "emit") as em:
            bot.handle_server_line(IRCNET_311)
        assert "katia" not in bot.verified
        assert not any(c.args[0] in ("VERIFIED", "TRUST_DENIED")
                       for c in em.call_args_list)
    finally:
        (bot.trust_rules, bot.verified, bot.whois_pending,
         bot.TRUST_VIA_311) = old


def test_311_flag_off_verifies_nothing():
    old = (bot.trust_rules, set(bot.verified), set(bot.whois_pending),
           bot.TRUST_VIA_311)
    try:
        bot.trust_rules = [("katia", "*.users.ircnet.com")]
        bot.verified = set()
        bot.whois_pending = {"katia"}
        bot.TRUST_VIA_311 = False      # the default: strict 307/330 only
        with mock.patch.object(bot, "log"), \
                mock.patch.object(bot, "send_raw"), \
                mock.patch.object(bot, "emit") as em:
            bot.handle_server_line(IRCNET_311)
        assert "katia" not in bot.verified
        assert "katia" in bot.whois_pending
        assert not any(c.args[0] in ("VERIFIED", "TRUST_DENIED")
                       for c in em.call_args_list)
    finally:
        (bot.trust_rules, bot.verified, bot.whois_pending,
         bot.TRUST_VIA_311) = old


def test_keepalive_tick_pings_after_silence():
    old = (bot.KEEPALIVE_SECS, bot.last_recv, bot.last_keepalive)
    try:
        bot.KEEPALIVE_SECS = 30.0
        bot.last_recv = 0.0            # silent since forever
        bot.last_keepalive = 0.0
        with mock.patch.object(bot, "send_raw") as sr, \
                mock.patch.object(bot, "emit") as em, \
                mock.patch.object(bot.time, "monotonic", return_value=100.0):
            bot.keepalive_tick()
        sr.assert_called_once_with("PING :keepalive")
        assert any(c.args[0] == "KEEPALIVE_SENT" for c in em.call_args_list)
        assert bot.last_keepalive == 100.0
    finally:
        bot.KEEPALIVE_SECS, bot.last_recv, bot.last_keepalive = old


def test_keepalive_tick_quiet_on_fresh_link():
    old = (bot.KEEPALIVE_SECS, bot.last_recv, bot.last_keepalive)
    try:
        bot.KEEPALIVE_SECS = 30.0
        bot.last_recv = 100.0          # now - last_recv = 0: nothing due
        bot.last_keepalive = 0.0
        with mock.patch.object(bot, "send_raw") as sr, \
                mock.patch.object(bot, "emit"), \
                mock.patch.object(bot.time, "monotonic", return_value=100.0):
            bot.keepalive_tick()
        sr.assert_not_called()
    finally:
        bot.KEEPALIVE_SECS, bot.last_recv, bot.last_keepalive = old


def test_keepalive_tick_disabled_at_zero():
    old = (bot.KEEPALIVE_SECS, bot.last_recv, bot.last_keepalive)
    try:
        bot.KEEPALIVE_SECS = 0.0       # BOT_KEEPALIVE=0
        bot.last_recv = 0.0            # ancient — would fire if enabled
        bot.last_keepalive = 0.0
        with mock.patch.object(bot, "send_raw") as sr, \
                mock.patch.object(bot, "emit"), \
                mock.patch.object(bot.time, "monotonic", return_value=1000.0):
            bot.keepalive_tick()
        sr.assert_not_called()
    finally:
        bot.KEEPALIVE_SECS, bot.last_recv, bot.last_keepalive = old
