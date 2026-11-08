import config
def test_lists():
    assert len(config.BROAD10) == 10 and config.BROAD10[:5] == config.CORE5
    assert config.HUIJIN_ETF == ["510300.SH", "510310.SH", "510330.SH", "159919.SZ", "510050.SH", "510500.SH", "512100.SH"]
    assert config.TEAM_NAME == "汇金重仓 7 只"
def test_sectors():
    assert len(config.SECTORS) == 15
    assert "农业·种业" in [s["name"] for s in config.SECTORS]
    assert [s["name"] for s in config.SECTORS if s["dynamic"]] == ["高位连板·情绪核心"]
    assert not any("恒生" in s["name"] for s in config.SECTORS)


def test_workflow_schedule():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert 'cron: "30 13 * * 1-5"' in text
    assert 'cron: "0 1 * * 1-5"' in text
    add_lines = [l for l in text.splitlines() if "git add" in l]
    assert any("history" in l and "data/eco_cal.json" in l for l in add_lines)
