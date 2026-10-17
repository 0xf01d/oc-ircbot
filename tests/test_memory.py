from datetime import date, timedelta

import memory_log
from memory_log import (ACTIVITY_ARCHIVE, ACTIVITY_LOG, MEMORY_INDEX,
                        Memory, today_heading)


def test_ensure_seeds_index_and_today(tmp_path):
    m = Memory(str(tmp_path))
    m.ensure()
    assert (tmp_path / MEMORY_INDEX).exists()
    body = (tmp_path / ACTIVITY_LOG).read_text()
    assert today_heading() in body
    # Idempotent: no duplicate headings.
    m.ensure()
    body2 = (tmp_path / ACTIVITY_LOG).read_text()
    assert body2.count(today_heading()) == 1


def test_append_activity_goes_under_today(tmp_path):
    m = Memory(str(tmp_path))
    m.append_activity("first")
    m.append_activity("second")
    text = m.read(ACTIVITY_LOG)
    assert "- first" in text and "- second" in text
    assert m.today_body().count("- ") == 2


def test_archive_older_than_moves_old_days(tmp_path):
    m = Memory(str(tmp_path))
    today = date(2026, 10, 15)
    m.ensure(today=today)
    old = (today - timedelta(days=20)).isoformat()
    recent = (today - timedelta(days=2)).isoformat()
    with open(m.path(ACTIVITY_LOG), "a") as f:
        f.write(f"## {old}\n\n- ancient\n\n## {recent}\n\n- fresh\n")
    gone, kept = m.archive_older_than(days=14, today=today)
    assert gone == 1
    body = m.read(ACTIVITY_LOG)
    assert "ancient" not in body and "fresh" in body
    assert old not in body
    assert recent in body
    assert "ancient" in m.read(ACTIVITY_ARCHIVE)
    # Today's heading always exists after a trim, even if nothing was left.
    assert f"## {today.isoformat()}" in body
    assert kept == 2                       # the recent block + today's block


def test_archive_keeps_everything_within_window(tmp_path):
    m = Memory(str(tmp_path))
    today = date(2026, 10, 15)
    m.ensure(today=today)
    d13 = (today - timedelta(days=13)).isoformat()
    with open(m.path(ACTIVITY_LOG), "a") as f:
        f.write(f"## {d13}\n\n- young enough\n")
    gone, _ = m.archive_older_than(days=14, today=today)
    assert gone == 0
    assert "young enough" in m.read(ACTIVITY_LOG)
    assert not (tmp_path / ACTIVITY_ARCHIVE).exists()


def test_today_body_stops_at_next_heading(tmp_path):
    m = Memory(str(tmp_path))
    today = date(2026, 10, 15)
    tomorrow = today + timedelta(days=1)
    with open(m.path(ACTIVITY_LOG), "w") as f:
        f.write(f"## {today}\n\n- t\n\n## {tomorrow}\n\n- tm\n")
    body = m.today_body(today=today)
    assert "- t" in body and "- tm" not in body


def test_log_compaction_records(tmp_path):
    m = Memory(str(tmp_path))
    m.log_compaction()
    assert "compacted" in m.read(ACTIVITY_LOG)


def test_index_text_reads_seed(tmp_path):
    m = Memory(str(tmp_path))
    m.ensure()
    assert "Memory index" in m.index_text()
