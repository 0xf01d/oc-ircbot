import leak_guard


def _guard(tmp_path, reserved_text):
    rf = tmp_path / "bot.reserved"
    rf.write_text(reserved_text)
    return leak_guard.LeakGuard("host", str(rf), str(tmp_path / "store.tsv"))


def test_no_reserved_channels_allows_everything(tmp_path):
    g = _guard(tmp_path, "# bot.reserved\n# none\n")
    assert g.check("#open", "anything at all, a very long line indeed") is None


def test_reserved_content_is_blocked_outbound(tmp_path):
    g = _guard(tmp_path, "#brain-private\n")
    secret = "the quick brown fox jumps over the lazy dog 0123456789"
    g.record("#brain-private", secret)
    assert g.check("#open", secret) is not None
    assert g.check("#other-open", f"quoting: {secret}") is not None


def test_sending_to_reserved_or_dm_is_allowed(tmp_path):
    g = _guard(tmp_path, "#brain-private\n")
    secret = "the quick brown fox jumps over the lazy dog 0123456789"
    g.record("#brain-private", secret)
    assert g.check("#brain-private", secret) is None
    assert g.check("#open", secret, dm_ok=True) is None


def test_short_overlap_does_not_trip(tmp_path):
    g = _guard(tmp_path, "#brain-private\n")
    g.record("#brain-private", "hello world how are you today my friend")
    assert g.check("#open", "hello world") is None
