import pytest

import policy


def _msg(body, to="#test", frm="alice", trust="TRUSTED", host="u@h"):
    return policy.parse_event(
        f"[12:34] MSG {trust} FROM={frm} HOST={host} TO={to} BODY={body}")


# --- event parsing ----------------------------------------------------------
def test_parse_event_msg():
    ev = _msg("hello there")
    assert ev["kind"] == "MSG"
    assert ev["trust"] == "TRUSTED"
    assert ev["from"] == "alice"
    assert ev["to"] == "#test"
    assert ev["body"] == "hello there"


def test_parse_event_idle_channel():
    ev = policy.parse_event("[09:00] IDLE #olografix")
    assert ev["kind"] == "IDLE"
    assert ev["to"] == "#olografix"
    assert policy.channel_of(ev) == "#olografix"


def test_parse_event_garbage():
    assert policy.parse_event("not an event line") is None


def test_channel_of_dm_buckets():
    ev = _msg("psst", to="vjt-claude")
    assert policy.channel_of(ev) == "#dm"


# --- mute filter (chan_filter.awk port) -------------------------------------
def test_muted_channel_events_never_reach_anything():
    ev = _msg("hello", to="#sniffo")
    assert policy.is_muted(ev, ("#sniffo",))
    assert not policy.is_muted(ev, ())
    # Case-insensitive, and the gate holds for addressed messages too:
    # "neanche se interpellato" — a mute is a mute.
    assert policy.is_muted(_msg("vjt-claude: hello", to="#Sniffo"),
                           ("#sniffo",))


# --- addressing gate ---------------------------------------------------------
def test_addressed_prefix_and_word():
    assert policy.is_addressed(_msg("vjt-claude: hi"), "vjt-claude")
    assert policy.is_addressed(_msg("hey vjt-claude you there?"), "vjt-claude")
    assert policy.is_addressed(_msg("VJT-CLAUDE: caps"), "vjt-claude")


def test_not_addressed_substring_or_self():
    assert not policy.is_addressed(_msg("vjt-claudex hi"), "vjt-claude")
    assert not policy.is_addressed(_msg("just chatting"), "vjt-claude")
    assert not policy.is_addressed(
        _msg("vjt-claude: echo", frm="VJT-CLAUDE"), "vjt-claude")


# --- wake gate: ALL events wake the model -------------------------------------
def test_every_kind_wakes_the_model():
    # vjt's Monitor injected everything — joins, parts, idles, notices are
    # social signal. No kind-gating anywhere anymore.
    nick = "bot"
    kinds = [
        "[09:00] MSG TRUSTED FROM=a HOST=h TO=#c BODY=banter",
        "[09:00] JOIN bob HOST=h #c",
        "[09:00] PART bob HOST=h #c",
        "[09:00] QUIT bob HOST=h :bye",
        "[09:00] NICK_CHANGE bob HOST=h bob2",
        "[09:00] IDLE #c",
        "[09:00] KICK op HOST=h #c victim :flood",
        "[09:00] INVITE op HOST=h #c trusted",
        "[09:00] CTCP FROM=evil HOST=h TO=bot BODY=PING",
        "[09:00] NOTICE FROM=n HOST=h TO=#c BODY=hi",
    ]
    for line in kinds:
        assert policy.wakes_model(policy.parse_event(line), nick), line


def test_empty_msg_and_address_only_are_the_only_gates():
    nick = "bot"
    # A whitespace-only MSG is not an event.
    assert not policy.wakes_model(_msg("   "), nick)
    # BRAIN_ADDRESS_ONLY keeps only the addressed ones (explicit config,
    # like BRAIN_MUTE — a door at intake, not a silent drop downstream).
    assert not policy.wakes_model(_msg("banter"), nick, address_only=True)
    assert policy.wakes_model(_msg("bot: ping"), nick, address_only=True)


# --- refusal patterns ---------------------------------------------------------
def test_refusal_hits_default_patterns():
    assert policy.refusal_hits("I can't help with that request.")
    assert policy.refusal_hits("This violates my content policy.")
    assert policy.refusal_hits("I won't comply.") is not []  # regex presence
    assert policy.refusal_hits("Sure, here is the answer.") == []


def test_refusal_custom_patterns_override():
    pats = (r"custom-marker",)
    assert policy.refusal_hits("custom-marker seen", pats)
    assert policy.refusal_hits("I can't help with that.", pats) == []


# --- FIFO verb grammar (gap 9: the full set, not only SAY) ---------------------
def test_parse_answer_full_verb_set_with_routing():
    text = """Sure, acting on that.
SAY azzurra|#test hello world
ACT libera|#test waves
NOTICE azzurra|alice psst
JOIN libera|#other
PART azzurra|#gone
WHOIS azzurra|vjt
QUIT azzurra|:goodbye
"""
    assert policy.parse_answer(text) == [
        ("SAY", "azzurra|#test", "hello world"),
        ("ACT", "libera|#test", "waves"),
        ("NOTICE", "azzurra|alice", "psst"),
        ("JOIN", "", "libera|#other"),
        ("PART", "", "azzurra|#gone"),
        ("WHOIS", "", "azzurra|vjt"),
        ("QUIT", "", "azzurra|:goodbye"),
    ]


def test_parse_answer_never_accepts_raw_from_the_model():
    # RAW is the bot's sidecar verb, never the model's: "orders only from
    # TRUSTED users" lives in the prompt, so the arbitrary-line verb cannot
    # live in the grammar — one injected `RAW QUIT :bye` must not execute.
    assert policy.parse_answer("SAY azzurra|#c hi\nRAW QUIT :bye") == \
        [("SAY", "azzurra|#c", "hi")]
    assert "RAW" not in policy.VERBS


def test_parse_answer_ignores_prose_and_injection():
    text = "\x1b[0m> build\x1b[0m\nrandom prose\nSAY  onlytarget\nSAY azzurra|#c\n" \
           "SAY azzurra|#ok fine\nQUIT\n"
    assert policy.parse_answer(text) == [
        ("SAY", "azzurra|#ok", "fine"), ("QUIT", "", "")]


def test_parse_answer_strips_control_chars_from_bodies():
    # A body must never smuggle a second FIFO command inside itself: every
    # line the brain renders starts with a whitelist verb.
    got = policy.parse_answer("SAY azzurra|#c one\r\nQUIT :forged\r\n")
    assert len(got) <= 2           # splitlines already separates
    assert all(v in policy.VERBS for v, _t, _b in got)


def test_filter_answer_drops_own_echo():
    # The routed form (post net| split) is what the gate sees.
    answer = [("SAY", "bot", "hi"), ("SAY", "#c", "bot: echo"),
              ("SAY", "#c", "fine"), ("ACT", "bot", "waves"), ("JOIN", "", "#c")]
    assert policy.filter_answer(answer, "bot") == [
        ("SAY", "#c", "fine"), ("JOIN", "", "#c")]


def test_own_echo_is_checked_per_network_nick():
    # The same nick word is our echo on net A and a harmless target on net B.
    assert policy.is_own_echo("SAY", "ocbot", "hi", "ocbot")
    assert not policy.is_own_echo("SAY", "ocbot", "hi", "othernick")
    assert policy.is_own_echo("SAY", "#c", "ocbot: echo", "ocbot")
    assert not policy.is_own_echo("JOIN", "", "#c", "ocbot")


# --- prompt construction --------------------------------------------------------
def test_build_system_describes_one_agent_on_many_networks():
    s = policy.build_system(
        [("azzurra", "vjt-claude"), ("libera", "ocbot")],
        registers={"#olografix": "supercazzola mode"})
    assert "ONE resident IRC agent" in s
    assert "vjt-claude on azzurra" in s and "ocbot on libera" in s
    assert "no separate sessions per channel" in s
    assert "Silence is the default" in s
    assert "spoofed" in s.lower()
    assert "- #olografix: supercazzola mode" in s
    # The routing contract is spelled out with the net| separator.
    assert "SAY azzurra|#channel text" in s
    assert "QUIT azzurra|reason" in s
    assert "known net| prefix is dropped" in s


def test_build_system_without_registers():
    assert "Channel registers" not in policy.build_system([("n", "bot")])


def test_fmt_event_vjt_prefix_and_host():
    # The vjt shape: [HH:MM] net|target <nick>: text — and HOST= never
    # stripped (the anti-spoof rule: the Monitor must never be host-blind).
    ev = _msg("hi bot")
    ev["net"] = "azzurra"
    assert policy.fmt_event(ev) == \
        "[12:34] azzurra|#test <alice>: hi bot (trusted; HOST=u@h)"
    dm = _msg("psst", to="vjt-claude", frm="alice")
    dm["net"] = "irc"
    assert policy.fmt_event(dm) == "[12:34] irc|alice <alice>: psst (trusted; HOST=u@h)"
    untr = _msg("yo", trust="UNTRUSTED", host="")
    untr["net"] = "irc"
    assert policy.fmt_event(untr) == "[12:34] irc|#test <alice>: yo (untrusted)"


def test_fmt_event_marks_addressed_and_renders_every_kind():
    ev = _msg("bot: ping", frm="b")
    assert policy.fmt_event(ev, addressed=True).endswith("ADDRESSED)")
    idle = policy.parse_event("[09:00] IDLE #olografix")
    idle["net"] = "azzurra"
    assert policy.fmt_event(idle) == "[09:00] azzurra|#olografix (idle)"
    join = policy.parse_event("[09:01] JOIN bob HOST=b@h #c")
    join["net"] = "libera"
    assert policy.fmt_event(join) == "[09:01] libera|#c bob #c (HOST=b@h)"


def test_build_prompt_lists_events_without_rehashing_context():
    # One conversation, append-only: the prompt is the new lines, nothing
    # re-presented from earlier turns.
    a, b = _msg("one", frm="a"), _msg("bot: two", frm="b")
    a["net"] = b["net"] = "irc"
    p = policy.build_prompt([a, b], addressed_ids=[id(b)])
    assert "[12:34] irc|#test <a>: one (trusted; HOST=u@h)" in p
    assert "[12:34] irc|#test <b>: bot: two (trusted; HOST=u@h; ADDRESSED)" in p
    assert "Network:" not in p and "Recent context" not in p
    assert "FIFO command lines only" in p


def test_build_prompt_interleaves_networks_in_one_conversation():
    # The whole point of the single session: channels from different
    # networks arrive as one continuous, prefixed stream.
    a = _msg("ciao", to="#olografix")
    a["net"] = "azzurra"
    b = _msg("hello", to="#help", frm="bob")
    b["net"] = "libera"
    p = policy.build_prompt([a, b])
    assert "[12:34] azzurra|#olografix <alice>: ciao" in p
    assert "[12:34] libera|#help <bob>: hello" in p


def test_rehydrate_prompt_carries_memory():
    p = policy.rehydrate_prompt("bot", "# index\n- note", "- today stuff")
    assert "MEMORY.md" in p and "note" in p and "today stuff" in p
    assert "Do not send anything to IRC" in p
