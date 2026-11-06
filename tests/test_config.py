import config
def test_lists():
    assert len(config.BROAD10) == 10 and config.BROAD10[:5] == config.CORE5
    assert len(config.FIXED_BROAD) == 29 and len(set(config.FIXED_BROAD)) == 29
    assert len(config.SH11) == 11 and all(c.endswith(".SH") for c in config.SH11)
def test_sectors():
    assert len(config.SECTORS) == 14
    assert [s["name"] for s in config.SECTORS if s["dynamic"]] == ["高位连板·情绪核心"]
    assert not any("恒生" in s["name"] for s in config.SECTORS)
