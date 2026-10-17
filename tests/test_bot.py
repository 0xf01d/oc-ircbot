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
