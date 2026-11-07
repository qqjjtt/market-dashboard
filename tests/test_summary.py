from summary import build_conclusions, national_team_verdict


def test_team_verdict():
    assert national_team_verdict([100, -20, 50])[0].startswith("仍在")
    assert national_team_verdict([-100, -20, 50])[0].startswith("疑似撤出")
    assert national_team_verdict([100, -150, 20])[0] == "放缓"


def test_team_verdict_tones_and_empty():
    assert national_team_verdict([10, 5, -1])[1] == "red"
    assert national_team_verdict([-10, -5, 1])[1] == "green"
    assert national_team_verdict([])[1] == "gray"


VOTE = {"score": 2, "label": "偏松", "signals": [
    {"name": "成交额", "dir": 1, "detail": "x"}, {"name": "两融余额", "dir": 0, "detail": "x"},
    {"name": "主力净流入", "dir": -1, "detail": "x"}, {"name": "Shibor 1W", "dir": None, "detail": "数据缺失，不计票"}]}
RATE = {"effective": 3.88, "implied": 3.6, "diff_bp": -28.0, "label": "市场预期降息"}
FED = {"key": "fed_net", "value": 5800.0, "change": 42.4, "change_unit": "十亿美元"}
SECTOR = {"inflow": [{"name": n, "net": 5.0, "pct": 1.0} for n in "ABCD"],
          "outflow": [{"name": n, "net": -5.0, "pct": -1.0} for n in "WXYZ"]}
KPIS = {"today": 71.2, "delta_text": "明显升温"}


def conclusions(**over):
    kw = dict(vote=VOTE, rate_expect=RATE, fed_card=FED, sector_day=SECTOR, team_last3=[30.0, 20.0, -5.0],
              team_asof="20261105", round_total=88.0, temp_kpis=KPIS, market_conc=65.3, market_conc_yday=64.1)
    kw.update(over)
    return build_conclusions(**kw)


def by_key(cs):
    return {c["key"]: c for c in cs}


def test_four_cards_shape():
    cs = conclusions()
    assert [c["key"] for c in cs] == ["liquidity", "sector", "team", "retail"]
    for c in cs:
        assert set(c) == {"key", "title", "text", "sub", "tone"}
        assert c["tone"] in {"red", "orange", "blue", "green", "gray"}


def test_liquidity_card():
    c = by_key(conclusions())["liquidity"]
    assert "偏松" in c["text"] and c["tone"] == "red"
    assert "Shibor 1W" in c["sub"] and "不计票" in c["sub"]
    assert "外围：美元净流动性 4 周 ↑ 42 十亿" in c["sub"] and "市场预期降息" in c["sub"]


def test_liquidity_fed_missing():
    c = by_key(conclusions(fed_card={"key": "fed_net", "value": None, "change": None}))["liquidity"]
    assert "外围：美元净流动性数据缺失" in c["sub"]


def test_sector_card():
    c = by_key(conclusions())["sector"]
    assert c["text"] == "流入：A、B、C ｜ 流出：W、X、Y"
    c = by_key(conclusions(sector_day={"inflow": [], "outflow": []}))["sector"]
    assert c["tone"] == "gray"


def test_team_card():
    c = by_key(conclusions())["team"]
    assert c["text"].startswith("仍在（近 3 日 +45.0 亿）") and c["tone"] == "red"
    assert "截至 11/5" in c["sub"] and "本轮累计 +88.0 亿" in c["sub"]
    c = by_key(conclusions(team_last3=[]))["team"]
    assert c["tone"] == "gray"
    c = by_key(conclusions(team_last3=[-30.0, -20.0, 5.0], round_total=None))["team"]
    assert c["text"].startswith("疑似撤出（−45.0 亿）") and "本轮累计" not in c["sub"]


def test_retail_card():
    c = by_key(conclusions())["retail"]
    assert c["text"] == "体温 71.2（温暖）· 浓度 65.3%（+1.2pp）" and c["tone"] == "red"
    c = by_key(conclusions(temp_kpis={"today": 35.0}, market_conc=None))["retail"]
    assert "浓度 —" in c["text"] and c["tone"] == "blue"
    c = by_key(conclusions(temp_kpis={}))["retail"]
    assert c["tone"] == "gray" and "体温暂无数据" in c["text"]


def test_staleness_labels_only_when_panel_date_differs():
    same = by_key(conclusions(trade_date="20261106", sector_date="20261106", conc_date="20261106"))
    assert "截至" not in same["sector"]["sub"] and "浓度截至" not in same["retail"]["text"]
    old = by_key(conclusions(trade_date="20261106", sector_date="20261105", conc_date="20261104"))
    assert "（截至 11/5）" in old["sector"]["sub"]
    assert "浓度截至 11/4" in old["retail"]["text"]
    # 日期缺失（面板无数据）→ 不乱标
    none = by_key(conclusions(trade_date="20261106", sector_date=None, conc_date=None))
    assert "截至" not in none["sector"]["sub"] and "浓度截至" not in none["retail"]["text"]
    # 旧调用方式（不传日期）保持原样
    assert by_key(conclusions())["retail"]["text"] == "体温 71.2（温暖）· 浓度 65.3%（+1.2pp）"


def test_team_card_watch_spikes():
    spikes = [{"name": "科创50ETF", "by": ["成交额"]}, {"name": "创业板ETF", "by": ["净申赎"]},
              {"name": "可转债ETF", "by": ["成交额", "净申赎"]}]
    c = by_key(conclusions(watch_spikes=spikes))["team"]
    assert c["text"].startswith("仍在（近 3 日 +45.0 亿）")
    assert c["sub"].endswith(" · 观察池天量：科创50ETF、创业板ETF 等 3 只")
    c = by_key(conclusions(watch_spikes=spikes[:1]))["team"]
    assert c["sub"].endswith(" · 观察池天量：科创50ETF")
    c = by_key(conclusions(watch_spikes=[]))["team"]
    assert "观察池" not in c["sub"]
    c = by_key(conclusions(team_last3=[], watch_spikes=spikes[:1]))["team"]
    assert c["tone"] == "gray" and "观察池天量：科创50ETF" in c["sub"]


def _li(value, zone, reason="外部在收紧：美元走强"):
    return {"value": value, "zone": zone, "reason": reason}


def test_liquidity_card_from_index_tight_is_green():
    c = by_key(conclusions(liquidity_index=_li(37, "偏紧")))["liquidity"]
    assert c["text"] == "流动性偏紧（37）" and c["sub"] == "外部在收紧：美元走强" and c["tone"] == "green"


def test_liquidity_card_from_index_tones():
    tones = {z: by_key(conclusions(liquidity_index=_li(v, z)))["liquidity"]["tone"]
             for v, z in ((10, "极度紧张"), (50, "中性"), (60, "偏松"), (90, "极度宽松"))}
    assert tones == {"极度紧张": "green", "中性": "blue", "偏松": "red", "极度宽松": "red"}


def test_liquidity_card_index_empty_is_gray():
    c = by_key(conclusions(liquidity_index=_li(None, None, "数据不足")))["liquidity"]
    assert c["text"] == "流动性数据不足" and c["tone"] == "gray"


def test_liquidity_card_without_index_keeps_vote():
    c = by_key(conclusions())["liquidity"]
    assert "偏松" in c["text"] and "合计" in c["text"] and c["tone"] == "red"
