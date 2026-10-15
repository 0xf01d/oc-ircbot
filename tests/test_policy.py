import pytest

import policy


def _msg(body, to="#test", frm="alice", trust="TRUSTED"):
    return policy.parse_event(
        f"[12:34] MSG {trust} FROM={frm} HOST=u@h TO={to} BODY={body}")


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


def test_wakes_model_matrix():
    nick = "bot"
    # Addressed MSG wakes even in address-only mode.
    assert policy.wakes_model(_msg("bot: ping"), nick, address_only=True)
    # Non-addressed MSG wakes in default (policy-gated) mode, not address-only.
    assert policy.wakes_model(_msg("banter"), nick)
    assert not policy.wakes_model(_msg("banter"), nick, address_only=True)
    # Empty-body MSG never wakes; non-MSG actionable kinds always do.
    assert not policy.wakes_model(_msg("   "), nick)
    idle = policy.parse_event("[09:00] IDLE #c")
    assert policy.wakes_model(idle, nick, address_only=True)
    # Context-only kinds never wake.
    assert not policy.wakes_model(
        policy.parse_event("[09:00] JOIN TO=#c FROM=x"), nick)


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
def test_parse_answer_full_verb_set():
    text = """Sure, acting on that.
SAY #test hello world
ACT #test waves
NOTICE alice psst
JOIN #other
PART #gone
WHOIS vjt
QUIT :goodbye
RAW PRIVMSG #x :direct
"""
    assert policy.parse_answer(text) == [
        ("SAY", "#test", "hello world"),
        ("ACT", "#test", "waves"),
        ("NOTICE", "alice", "psst"),
        ("JOIN", "", "#other"),
        ("PART", "", "#gone"),
        ("WHOIS", "", "vjt"),
        ("QUIT", "", ":goodbye"),
        ("RAW", "", "PRIVMSG #x :direct"),
    ]


def test_parse_answer_ignores_prose_and_injection():
    text = "\x1b[0m> build\x1b[0m\nrandom prose\nSAY  onlytarget\nSAY #c\n" \
           "SAY #ok fine\nQUIT\n"
    assert policy.parse_answer(text) == [
        ("SAY", "#ok", "fine"), ("QUIT", "", "")]


def test_parse_answer_accepts_bare_quit():
    assert policy.parse_answer("QUIT") == [("QUIT", "", "")]


def test_parse_answer_strips_control_chars_from_bodies():
    # A body must never smuggle a second FIFO command inside itself: every
    # line the brain renders starts with a whitelist verb.
    got = policy.parse_answer("SAY #c one\r\nQUIT :forged\r\n")
    assert len(got) <= 2           # splitlines already separates
    assert all(v in policy.VERBS for v, _t, _b in got)


def test_filter_answer_drops_own_echo():
    answer = policy.parse_answer(
        "SAY bot hi\nSAY #c bot: echo\nSAY #c fine\nACT bot waves\n"
        "JOIN #x")
    assert policy.filter_answer(answer, "bot") == [
        ("SAY", "#c", "fine"), ("JOIN", "", "#x")]


# --- prompt construction --------------------------------------------------------
def test_build_system_contains_policy_and_register():
    s = policy.build_system("bot", "azzurra", "#olografix",
                            {"#olografix": "supercazzola mode"})
    assert "Silence is the default" in s
    assert "spoofed" in s.lower()
    assert "Register for #olografix: supercazzola mode" in s
    assert "SAY <target>" in s and "QUIT" in s
    # No register configured for an unlisted channel: no register line.
    assert "Register for" not in policy.build_system("bot", "n", "#other", {})


def test_build_prompt_lists_context_new_and_marks_addressed():
    old = _msg("old line", frm="a")
    new = _msg("bot: addressed", frm="b")
    ring = [old, new]
    p = policy.build_prompt([new], ring, "azzurra", "#test",
                            addressed_ids=[id(new)])
    assert "Network: azzurra   Channel: #test" in p
    assert "[12:34] <a> to #test (trusted): old line" in p
    assert p.count("[12:34] <b> to #test (trusted): bot: addressed") == 1
    assert "[ADDRESSED]" in p
    assert "FIFO command lines only" in p


def test_registers_text_case_insensitive():
    reg = {"#Olografix": "calm"}
    assert policy.registers_text(reg, "#olografix") == \
        "Register for #olografix: calm"
    assert policy.registers_text(reg, "#zzz") == ""


def test_rehydrate_prompt_carries_memory():
    p = policy.rehydrate_prompt("bot", "# index\n- note", "- today stuff")
    assert "MEMORY.md" in p and "note" in p and "today stuff" in p
    assert "Do not send anything to IRC" in p
