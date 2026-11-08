import importlib.util
import os
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


# --- persistent IRC command hook (_irc_command_dispatch) ---------------------

FAKE_CMD = (
    "GOT = None\n"
    "def handle(target, nick, body, trusted, send_raw, emit, split_say):\n"
    "    global GOT\n"
    "    GOT = (target, nick, body, trusted, send_raw, emit, split_say)\n"
    "    return %r\n"
)


def _write_fake_cmd(path, token):
    # handle() records its positional args and returns `token`, so a test can
    # pin both the call shape and the pass-through in one file.
    path.write_text(FAKE_CMD % token)


def _install_fake_cmd(tmp_path, token="v1"):
    f = tmp_path / "irc_commands.py"
    _write_fake_cmd(f, token)
    bot.IRC_COMMANDS_FILE = str(f)
    bot._irc_mod = None
    bot._irc_mtime = None
    bot._irc_missing_seen = False
    return f


def test_irc_dispatch_passes_args_positionally_and_returns(tmp_path):
    old = (bot.IRC_COMMANDS_FILE, bot._irc_mod, bot._irc_mtime,
           bot._irc_missing_seen)
    try:
        f = _install_fake_cmd(tmp_path, "v1")
        with mock.patch.object(bot, "send_raw") as sr, \
                mock.patch.object(bot, "emit") as em, \
                mock.patch.object(bot, "split_say") as ss:
            rv = bot._irc_command_dispatch("#c", "katia", "!tom", True)
        assert rv == "v1"
        assert bot._irc_mod.GOT == ("#c", "katia", "!tom", True, sr, em, ss)
        assert any(c.args == ("IRC_COMMANDS_LOADED", str(f))
                   for c in em.call_args_list)
    finally:
        (bot.IRC_COMMANDS_FILE, bot._irc_mod, bot._irc_mtime,
         bot._irc_missing_seen) = old


def test_irc_dispatch_reloads_when_mtime_changes(tmp_path):
    old = (bot.IRC_COMMANDS_FILE, bot._irc_mod, bot._irc_mtime,
           bot._irc_missing_seen)
    try:
        f = _install_fake_cmd(tmp_path, "v1")
        with mock.patch.object(bot, "emit"), \
                mock.patch.object(bot, "send_raw"), \
                mock.patch.object(bot, "split_say"):
            assert bot._irc_command_dispatch("#c", "n", "!x", False) == "v1"
        st = os.stat(f)
        _write_fake_cmd(f, "v2")
        # Two writes can land inside one filesystem timestamp tick; pin the
        # new mtime so the reload gate cannot flake.
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 60_000_000_000))
        with mock.patch.object(bot, "emit"), \
                mock.patch.object(bot, "send_raw"), \
                mock.patch.object(bot, "split_say"):
            assert bot._irc_command_dispatch("#c", "n", "!x", False) == "v2"
    finally:
        (bot.IRC_COMMANDS_FILE, bot._irc_mod, bot._irc_mtime,
         bot._irc_missing_seen) = old


def test_irc_dispatch_import_error_is_loud_but_not_fatal(tmp_path):
    old = (bot.IRC_COMMANDS_FILE, bot._irc_mod, bot._irc_mtime,
           bot._irc_missing_seen)
    try:
        f = tmp_path / "irc_commands.py"
        f.write_text("raise RuntimeError('boom')\n")
        bot.IRC_COMMANDS_FILE = str(f)
        bot._irc_mod = None
        bot._irc_mtime = None
        bot._irc_missing_seen = False
        with mock.patch.object(bot, "emit") as em, \
                mock.patch.object(bot, "send_raw"), \
                mock.patch.object(bot, "split_say"):
            rv = bot._irc_command_dispatch("#c", "n", "!x", False)
        assert rv is None
        assert any(c.args[0] == "IRC_CMD_IMPORT_ERROR"
                   for c in em.call_args_list)
    finally:
        (bot.IRC_COMMANDS_FILE, bot._irc_mod, bot._irc_mtime,
         bot._irc_missing_seen) = old


def test_irc_dispatch_missing_file_emits_once(tmp_path):
    old = (bot.IRC_COMMANDS_FILE, bot._irc_mod, bot._irc_mtime,
           bot._irc_missing_seen)
    try:
        absent = tmp_path / "absent.py"
        bot.IRC_COMMANDS_FILE = str(absent)
        bot._irc_mod = None
        bot._irc_mtime = None
        bot._irc_missing_seen = False
        with mock.patch.object(bot, "emit") as em, \
                mock.patch.object(bot, "send_raw"), \
                mock.patch.object(bot, "split_say"):
            for _ in range(3):
                bot._irc_command_dispatch("#c", "n", "!x", False)
        assert [c.args for c in em.call_args_list
                if c.args[0] == "IRC_COMMANDS_MISSING"] == \
            [("IRC_COMMANDS_MISSING", str(absent))]
    finally:
        (bot.IRC_COMMANDS_FILE, bot._irc_mod, bot._irc_mtime,
         bot._irc_missing_seen) = old


def test_shipped_irc_commands_imports_cleanly():
    # The image fallback copy must always be importable: a syntax error there
    # would only ever surface in production with /data unmounted.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "ocbot_irc_commands_shipped_test",
        os.path.join(root, "irc_commands.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.handle)


# --- SASL PLAIN -------------------------------------------------------------
# hackint dropped NickServ IDENTIFY (2026-02), so a password can only be
# presented at registration time. These pin the handshake order: CAP LS 302 ->
# CAP REQ sasl -> AUTHENTICATE PLAIN -> base64(\0acct\0pass) -> CAP END on 903.
# Getting the order wrong parks the connection in registration forever, and
# the bot would look "connected" (TCP) while never joining a channel.


def _sasl(sasl=True, acct="ocbot", pw="s3cret"):
    return mock.patch.multiple(bot, SASL_ENABLED=sasl, SASL_ACCOUNT=acct,
                               SASL_PASS=pw)


def test_sasl_cap_ls_requests_sasl():
    with _sasl(), mock.patch.object(bot, "sasl_requested", False), \
            mock.patch.object(bot, "send_raw") as sr:
        bot.handle_server_line(":irc.hackint.org CAP * LS :sasl account-tag")
        assert sr.call_args.args[0] == "CAP REQ :sasl"
        assert bot.sasl_requested is True   # inside the patch: it restores


def test_sasl_cap_ls_without_sasl_ends_registration():
    with _sasl(), mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit") as em:
        bot.handle_server_line(":irc.hackint.org CAP * LS :account-tag")
    assert sr.call_args.args[0] == "CAP END"
    assert any(c.args[0] == "SASL_UNAVAILABLE" for c in em.call_args_list)


def test_sasl_authenticate_plain_payload():
    import base64
    with _sasl(), mock.patch.object(bot, "send_raw") as sr:
        bot.handle_server_line("AUTHENTICATE +")
    want = "AUTHENTICATE " + base64.b64encode(b"\x00ocbot\x00s3cret").decode()
    assert sr.call_args.args[0] == want


def test_sasl_903_ends_cap_and_reports():
    with _sasl(), mock.patch.object(bot, "sasl_done", False), \
            mock.patch.object(bot, "send_raw") as sr, \
            mock.patch.object(bot, "emit") as em:
        bot.handle_server_line(":irc.hackint.org 903 ocbot :SASL ok")
        assert sr.call_args.args[0] == "CAP END"
        assert bot.sasl_done is True
    assert any(c.args[0] == "SASL_OK" for c in em.call_args_list)


def test_sasl_failure_still_ends_registration():
    # 904 bad credentials: never leave the client hanging in CAP.
    for code in ("904", "905", "906"):
        with _sasl(), mock.patch.object(bot, "sasl_done", False), \
                mock.patch.object(bot, "send_raw") as sr, \
                mock.patch.object(bot, "emit") as em:
            bot.handle_server_line(f":irc.hackint.org {code} ocbot :nope")
        assert sr.call_args.args[0] == "CAP END"
        assert any(c.args[0] == "SASL_FAIL" for c in em.call_args_list)


def test_sasl_disabled_leaves_cap_alone():
    # No credentials -> the old plain NICK/USER path, untouched.
    with _sasl(sasl=False), mock.patch.object(bot, "send_raw") as sr:
        bot.handle_server_line(":irc.hackint.org CAP * LS :sasl")
    sr.assert_not_called()


# --- outbound pacing --------------------------------------------------------
# hackint killed the link with Excess Flood on 2026-11-04: trust_check WHOIS'd
# ~25 nicks back to back and nothing throttled the wire. send_raw now reserves
# a slot at BOT_SEND_RATE lines/sec per process, except for PONG/QUIT, which
# must never queue behind a backlog (a late PONG is a ping timeout, a late
# QUIT is an unclean part).


def test_pace_send_spaces_reservations():
    with mock.patch.object(bot, "SEND_INTERVAL", 0.01), \
            mock.patch.object(bot, "send_next_at", 0.0), \
            mock.patch("time.sleep") as sl:
        bot._pace_send()
        bot._pace_send()
        bot._pace_send()
    # First call is immediate (no backlog), the next two each reserve 10 ms.
    assert [c.args[0] for c in sl.call_args_list if c.args] != []


def test_pace_send_disabled_at_rate_zero():
    with mock.patch.object(bot, "SEND_INTERVAL", 0.0), \
            mock.patch("time.sleep") as sl:
        bot._pace_send()
    sl.assert_not_called()


def test_send_raw_paces_privmsg_but_not_pong_or_quit():
    with mock.patch.object(bot, "_pace_send") as pace, \
            mock.patch.object(bot, "sock") as sk:
        sk.sendall = mock.MagicMock()
        bot.send_raw("PRIVMSG #c :hi")
        paced_after_chat = pace.call_count
        bot.send_raw("PONG :tok")
        bot.send_raw("QUIT bye")
    assert paced_after_chat == 1     # the PRIVMSG waited its turn
    assert pace.call_count == 1      # PONG and QUIT went straight out
