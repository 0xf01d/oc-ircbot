from collections import deque

import brain


def test_parse_event_msg():
    ev = brain.parse_event(
        "[12:34] MSG TRUSTED FROM=alice HOST=u@h TO=#test BODY=hello there")
    assert ev["kind"] == "MSG"
    assert ev["trust"] == "TRUSTED"
    assert ev["from"] == "alice"
    assert ev["host"] == "u@h"
    assert ev["to"] == "#test"
    assert ev["body"] == "hello there"


def test_parse_event_idle_channel():
    ev = brain.parse_event("[09:00] IDLE #olografix")
    assert ev["kind"] == "IDLE"
    assert ev["to"] == "#olografix"
    assert brain.prompts_of(ev) == "#olografix"


def test_parse_event_garbage():
    assert brain.parse_event("not an event line") is None


def test_parse_answer_strips_ansi_and_keeps_say():
    text = "\x1b[0m> build \u00b7 model\x1b[0m\nSAY #test hi there\nrandom\n"
    assert brain.parse_answer(text) == [("#test", "hi there")]


def test_parse_answer_ignores_non_say():
    assert brain.parse_answer("hello world\nSAY\nSAY  onlytarget") == []


def test_prompts_of_dm_buckets():
    ev = brain.parse_event("[12:00] MSG TRUSTED FROM=bob HOST=u@h TO=vjt-claude BODY=psst")
    assert brain.prompts_of(ev) == "#dm"


def test_build_prompt_lists_context_and_new_without_duplication():
    old = brain.parse_event("[12:00] MSG TRUSTED FROM=a HOST=h TO=#c BODY=old")
    new = brain.parse_event("[12:01] MSG TRUSTED FROM=b HOST=h TO=#c BODY=new")
    ring = {"#c": deque([old, new], maxlen=8)}
    p = brain.build_prompt([new], ring)
    old_line = "[12:00] <a> to #c (trusted): old"
    new_line = "[12:01] <b> to #c (trusted): new"
    assert old_line in p
    assert p.count(new_line) == 1
    assert "SAY <target>" in p


def test_brain_ring_buffer_is_bounded():
    b = brain.Brain()
    for i in range(100):
        b.feed(brain.parse_event(
            f"[12:00] MSG TRUSTED FROM=u HOST=h TO=#c BODY=m{i}"))
    assert len(b.ring["#c"]) <= brain.RING


def test_brain_ignores_own_nick():
    b = brain.Brain()
    b.feed(brain.parse_event(
        f"[12:00] MSG TRUSTED FROM={brain.NICK} HOST=h TO=#c BODY=echo"))
    assert not b.pending
    assert "#c" not in b.ring


def test_write_fifo_roundtrip(tmp_path):
    import os
    import threading
    fifo = str(tmp_path / "bot.send")
    os.mkfifo(fifo)
    got = []
    t = threading.Thread(target=lambda: got.append(open(fifo).readline()))
    t.start()
    n = brain.write_fifo([("#test", "hi")], path=fifo, deadline=5)
    t.join(timeout=5)
    assert n == 1
    assert got and got[0].strip() == "SAY #test hi"
