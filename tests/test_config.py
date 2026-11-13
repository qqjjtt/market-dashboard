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
    for cron in ("23 4 * * 1-5", "37,7 21-22 * * 0-4", "53 12 * * 1-5", "17,47 6-7 * * 1-5"):
        assert f'cron: "{cron}"' in text
    assert "push:" in text and "paths-ignore:" in text
    assert '"history/**"' in text and '"data/eco_cal.json"' in text
    assert "healthcheck.py site/index.html" in text and "--verify-live" in text
    assert "\n  verify:" in text
    assert "\n  gate:" in text and "--gate" in text and "needs.gate.outputs.run == 'true'" in text
    assert "!cancelled() && needs.build.result == 'success'" in text
    assert "continue-on-error: true  # 体检" in text
    add_lines = [l for l in text.splitlines() if "git add" in l]
    assert any(l.strip() == "git add history" for l in add_lines)
    assert 'for p in data/eco_cal.json db; do' in text    # 日历存档与历史库只在文件存在时才加入
    assert '- "db/**"' in text                            # 只提交 db 的存档不重建


def test_sector_db_config():
    assert config.SECTOR_DB == dict(days=250, chunk=30, recheck=20)
