import watchdog


def test_is_stalled_missing_and_old():
    assert watchdog.is_stalled(None)
    assert watchdog.is_stalled(100.0, stall=30.0)
    assert not watchdog.is_stalled(5.0, stall=30.0)


def test_heartbeat_age_missing_file(monkeypatch):
    monkeypatch.setattr(watchdog, "HEARTBEAT", "/nonexistent/brain.heartbeat")
    assert watchdog.heartbeat_age() is None


def test_heartbeat_age_reads_mtime(tmp_path, monkeypatch):
    hb = tmp_path / "brain.heartbeat"
    hb.write_text("1 1\n")
    monkeypatch.setattr(watchdog, "HEARTBEAT", str(hb))
    age = watchdog.heartbeat_age()
    assert age is not None and age < 5
