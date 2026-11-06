import json
import math

import numpy as np
import pandas as pd
import pytest

from metrics_liquidity import (
    fed_net_liquidity,
    filter_news,
    liquidity_vote,
    load_policy_ops,
    make_card,
    rate_expectation,
    sector_flow_rank,
)


def ser(d):
    return pd.Series(list(d.values()), index=pd.to_datetime(list(d.keys())), dtype=float)


# ---------- make_card ----------

def test_make_card_basic_diff_and_shape():
    s = pd.Series([1.0, 2.0, 4.0], index=["20261104", "20261105", "20261106"])
    c = make_card("turnover", "成交额", s, "亿元", note="n", tone="red")
    assert set(c) == {"key", "label", "value", "unit", "change", "change_unit", "date", "spark", "note", "tone"}
    assert c["key"] == "turnover" and c["label"] == "成交额" and c["unit"] == "亿元"
    assert c["value"] == 4.0 and c["change"] == 2.0 and c["change_unit"] == "亿元"
    assert c["date"] == "20261106"
    assert c["spark"] == [1.0, 2.0, 4.0]
    assert c["note"] == "n" and c["tone"] == "red"


def test_make_card_pct_change():
    s = pd.Series([100.0, 110.0], index=["20261105", "20261106"])
    c = make_card("k", "l", s, "x", change="pct")
    assert c["change"] == pytest.approx(10.0) and c["change_unit"] == "%"


def test_make_card_pct_with_zero_base_is_none():
    s = pd.Series([0.0, 5.0], index=["20261105", "20261106"])
    assert make_card("k", "l", s, "x", change="pct")["change"] is None


def test_make_card_skips_trailing_nan():
    s = pd.Series([1.0, 3.0, np.nan], index=["20261104", "20261105", "20261106"])
    c = make_card("k", "l", s, "u")
    assert c["value"] == 3.0 and c["date"] == "20261105" and c["change"] == 2.0
    assert c["spark"] == [1.0, 3.0]


def test_make_card_change_vs_previous_non_nan():
    s = pd.Series([10.0, np.nan, 13.0], index=["20261104", "20261105", "20261106"])
    assert make_card("k", "l", s, "u")["change"] == 3.0


def test_make_card_single_point_has_no_change():
    c = make_card("k", "l", pd.Series([5.0], index=["20261106"]), "u")
    assert c["value"] == 5.0 and c["change"] is None


def test_make_card_spark_truncated_to_spark_n():
    s = pd.Series(range(50), index=[f"2026{i:04d}" for i in range(50)], dtype=float)
    c = make_card("k", "l", s, "u", spark_n=30)
    assert len(c["spark"]) == 30 and c["spark"][-1] == 49.0 and c["spark"][0] == 20.0


def test_make_card_empty():
    c = make_card("k", "l", pd.Series([np.nan, np.nan], index=["20261105", "20261106"]), "u")
    assert c["value"] is None and c["note"] == "无数据"
    assert c["change"] is None and c["date"] is None and c["spark"] == []
    c2 = make_card("k", "l", pd.Series([], dtype=float), "u")
    assert c2["value"] is None and c2["note"] == "无数据"


def test_make_card_timestamp_index_date_is_yyyymmdd():
    c = make_card("k", "l", ser({"2026-10-28": 1.0, "2026-11-04": 2.0}), "u")
    assert c["date"] == "20261104"


def test_make_card_change_lag_4_weekly():
    s = ser({"2026-10-07": 100, "2026-10-14": 110, "2026-10-21": 120, "2026-10-28": 130, "2026-11-04": 150})
    c = make_card("fed", "净流动性", s, "十亿美元", change="diff", change_lag=4)
    assert c["change"] == 50 and c["value"] == 150


def test_make_card_change_lag_insufficient_points():
    s = ser({"2026-10-28": 130, "2026-11-04": 150})
    assert make_card("k", "l", s, "u", change_lag=4)["change"] is None


def test_make_card_json_friendly():
    s = pd.Series([np.float64(1.5), np.float64(2.5)], index=["20261105", "20261106"])
    c = make_card("k", "l", s, "u")
    json.dumps(c, allow_nan=False)
    assert type(c["value"]) is float and type(c["change"]) is float
    assert all(type(v) is float for v in c["spark"])


def test_make_card_inf_treated_as_missing():
    s = pd.Series([1.0, np.inf], index=["20261105", "20261106"])
    c = make_card("k", "l", s, "u")
    assert c["value"] == 1.0


# ---------- liquidity_vote ----------

def test_vote_loose():
    v = liquidity_vote(1.15, 0.6, -10.0, -6.0)
    assert v["score"] == 2 and v["label"] == "偏松" and [s["dir"] for s in v["signals"]] == [1, 1, -1, 1]


def test_vote_missing_signal_not_counted():
    v = liquidity_vote(None, -0.6, -1.0, 0.0)
    assert v["signals"][0]["dir"] is None and v["score"] == -2 and v["label"] == "偏紧"


def test_vote_neutral_and_thresholds_strict():
    v = liquidity_vote(1.10, 0.5, 0.0, 4.9)  # 边界值全部为 0 票
    assert [s["dir"] for s in v["signals"]] == [0, 0, 0, 0]
    assert v["score"] == 0 and v["label"] == "中性"


def test_vote_shibor_boundary_inclusive():
    assert liquidity_vote(None, None, None, -5.0)["signals"][3]["dir"] == 1
    assert liquidity_vote(None, None, None, 5.0)["signals"][3]["dir"] == -1


def test_vote_all_missing_and_nan():
    v = liquidity_vote(None, float("nan"), None, None)
    assert v["score"] == 0 and v["label"] == "中性"
    assert all(s["dir"] is None for s in v["signals"])


def test_vote_signals_have_name_and_detail():
    v = liquidity_vote(1.2, None, 3.0, 0.0)
    assert len(v["signals"]) == 4
    for s in v["signals"]:
        assert set(s) == {"name", "dir", "detail"} and s["name"] and s["detail"]
    assert "缺失" in v["signals"][1]["detail"] or "无数据" in v["signals"][1]["detail"]


def test_vote_custom_cfg():
    cfg = dict(turnover_hi=1.5, turnover_lo=0.5, margin_pct=1.0, shibor_bp=10, loose=1, tight=-1)
    v = liquidity_vote(1.2, 0.6, None, None, cfg)
    assert [s["dir"] for s in v["signals"][:2]] == [0, 0]
    v2 = liquidity_vote(2.0, None, None, None, cfg)
    assert v2["score"] == 1 and v2["label"] == "偏松"


# ---------- fed_net_liquidity ----------

def test_fed_net_liquidity():
    s = fed_net_liquidity(ser({"2026-11-04": 6745635}), ser({"2026-11-04": 957175}), ser({"2026-11-04": 0.125}))
    assert s.iloc[-1] == pytest.approx(5788.335)


def test_fed_net_liquidity_rrp_asof_alignment():
    walcl = ser({"2026-10-28": 7_000_000, "2026-11-04": 7_100_000})
    tga = ser({"2026-10-28": 900_000, "2026-11-04": 1_000_000})
    rrp = ser({"2026-10-27": 10.0, "2026-10-28": 20.0, "2026-10-30": 30.0, "2026-11-04": 40.0, "2026-11-05": 99.0})
    s = fed_net_liquidity(walcl, tga, rrp)
    assert list(s.index) == list(walcl.index)
    assert s.iloc[0] == pytest.approx(7000 - 900 - 20)
    assert s.iloc[1] == pytest.approx(7100 - 1000 - 40)  # 不取 11-05 的未来值


def test_fed_net_liquidity_rrp_nan_uses_previous_valid():
    walcl = ser({"2026-11-04": 7_000_000})
    tga = ser({"2026-11-04": 1_000_000})
    rrp = ser({"2026-11-03": 5.0, "2026-11-04": np.nan})
    assert fed_net_liquidity(walcl, tga, rrp).iloc[0] == pytest.approx(6000 - 5)


def test_fed_net_liquidity_missing_inputs_give_nan_not_zero():
    walcl = ser({"2026-11-04": 7_000_000})
    empty = pd.Series([], dtype=float)
    s = fed_net_liquidity(walcl, empty, ser({"2026-11-04": 1.0}))
    assert len(s) == 1 and math.isnan(s.iloc[0])
    s2 = fed_net_liquidity(walcl, ser({"2026-11-04": 1_000_000}), empty)
    assert math.isnan(s2.iloc[0])
    assert fed_net_liquidity(empty, empty, empty).empty


# ---------- rate_expectation ----------

def test_rate_expectation():
    assert rate_expectation(3.88, 96.113)["label"] == "预期按兵不动"
    r = rate_expectation(3.88, 96.30)
    assert r["diff_bp"] == pytest.approx(-18) and r["label"] == "市场预期降息"
    assert rate_expectation(None, 96.1)["label"] == "无数据"


def test_rate_expectation_hike_and_fields():
    r = rate_expectation(3.88, 96.0)  # implied 4.00, +12bp
    assert r["label"] == "预期加息" and r["diff_bp"] == pytest.approx(12)
    assert r["implied"] == pytest.approx(4.0) and r["effective"] == 3.88


def test_rate_expectation_band_boundary_and_custom_band():
    assert rate_expectation(4.0, 96.1)["label"] == "预期按兵不动"  # 恰 -10bp 不算降息
    assert rate_expectation(3.88, 96.113, band_bp=0.5)["label"] == "预期加息"


def test_rate_expectation_missing_variants():
    for args in [(3.88, None), (None, None), (float("nan"), 96.0), (3.88, float("nan"))]:
        r = rate_expectation(*args)
        assert r["label"] == "无数据" and r["diff_bp"] is None and r["implied"] is None


# ---------- sector_flow_rank ----------

def _flow_df():
    rows = []
    # 6 个交易日；A 每天 +1 （5日累计+5，当日 +1）；B 前 5 天 -2，最后一天 +10；C 最早一天 +100（落在 5 日窗口之外）
    dates = ["20261029", "20261030", "20261102", "20261103", "20261104", "20261105"]
    for d in dates:
        rows.append((d, "A", 1.0, 0.5))
    for d in dates[:-1]:
        rows.append((d, "B", -2.0, -0.3))
    rows.append(("20261105", "B", 10.0, 2.2))
    for d in dates:
        rows.append((d, "C", 100.0 if d == "20261029" else 0.0, 0.0))
    rows.append(("20261106", "A", 999.0, 9.9))  # 晚于 end，必须被忽略
    return pd.DataFrame(rows, columns=["trade_date", "industry", "net_amount", "pct_change"])


def test_sector_flow_rank_5d():
    r = sector_flow_rank(_flow_df(), "20261105", 5)
    # 窗口 = 20261030..20261105：A=5, B=-8+10=2, C=0
    assert [x["name"] for x in r["inflow"]] == ["A", "B", "C"]
    assert r["inflow"][0]["net"] == pytest.approx(5.0)
    assert [x["name"] for x in r["outflow"]] == ["C", "B", "A"]
    assert r["inflow"][0]["pct"] == pytest.approx(0.5)  # pct 取 end 当日
    assert r["days"] == 5
    assert r["inflow"][1]["pct"] == pytest.approx(2.2)


def test_sector_flow_rank_days_is_actual_number_summed():
    r = sector_flow_rank(_flow_df(), "20261030", 5)  # 只有 2 个交易日 ≤ 20261030
    assert r["days"] == 2
    assert sector_flow_rank(_flow_df(), "20261105", 1)["days"] == 1


def test_sector_flow_rank_1d_and_top():
    r = sector_flow_rank(_flow_df(), "20261105", 1, top=2)
    assert [x["name"] for x in r["inflow"]] == ["B", "A"]
    assert [x["name"] for x in r["outflow"]] == ["C", "A"]
    assert r["inflow"][0]["net"] == pytest.approx(10.0)


def test_sector_flow_rank_end_not_in_data_pct_none():
    r = sector_flow_rank(_flow_df(), "20261101", 2)  # 非交易日 → 窗口 1029~1030，pct 取不到
    assert all(x["pct"] is None for x in r["inflow"])
    assert r["inflow"][0]["name"] == "C"


def test_sector_flow_rank_empty_and_nan():
    empty = pd.DataFrame(columns=["trade_date", "industry", "net_amount", "pct_change"])
    assert sector_flow_rank(empty, "20261105", 5) == {"inflow": [], "outflow": []}
    df = pd.DataFrame([("20261105", "A", np.nan, 1.0), ("20261105", "B", 2.0, np.nan)],
                      columns=["trade_date", "industry", "net_amount", "pct_change"])
    r = sector_flow_rank(df, "20261105", 1)
    assert [x["name"] for x in r["inflow"]] == ["B"]  # NaN 净额的行业不参与排名，不当 0
    assert r["inflow"][0]["pct"] is None
    json.dumps(r, allow_nan=False)


# ---------- filter_news ----------

KD = ["互换便利", "再贷款", "央行", "降息"]
KE = ["美联储", "加息", "英国央行", "日本央行", "降息"]


def _news():
    rows = [
        ("2026-11-05 09:00:00", "10jqka", "央行：今日开展 5000 亿逆回购", "http://a/1"),
        ("2026-11-05 10:00:00", "10jqka", "华福证券中标央行第三批互换便利操作12亿元", "http://a/2"),
        ("2026-11-05 08:00:00", "cls", "央行：今日开展 5000 亿逆回购", "http://a/1b"),  # 重复标题，较旧
        ("2026-11-04 12:00:00", "cls", "英国央行三年多来首度加息", "http://a/3"),
        ("2026-11-04 13:00:00", "cls", "央行与美联储同日表态", None),
        ("2026-11-03 13:00:00", "cls", "美联储官员称不急于行动", ""),
        ("2026-11-03 14:00:00", "cls", "今日天气晴朗", "http://a/9"),
    ]
    return pd.DataFrame(rows, columns=["pub_time", "src", "title", "url"])


def test_filter_news():
    d = filter_news(_news(), KD, KE)
    assert d["domestic"][0]["flag"] is True  # 12亿元那条，时间最新
    assert "12亿元" in d["domestic"][0]["title"]
    assert any("英国央行" in n["title"] for n in d["external"])
    assert not any("美联储" in n["title"] and "央行与" in n["title"] for n in d["external"])
    assert any("央行与" in n["title"] for n in d["domestic"])  # 双命中 → 国内


def test_filter_news_dedup_sort_and_shape():
    d = filter_news(_news(), KD, KE)
    titles = [n["title"] for n in d["domestic"]]
    assert titles.count("央行：今日开展 5000 亿逆回购") == 1
    kept = [n for n in d["domestic"] if n["title"].startswith("央行：今日")][0]
    assert kept["url"] == "http://a/1"  # 保留较新的一条
    times = [n["time"] for n in d["domestic"]]
    assert times == sorted(times, reverse=True)
    assert set(d["domestic"][0]) == {"time", "src", "title", "url", "flag"}
    assert not any("天气" in n["title"] for n in d["domestic"] + d["external"])


def test_filter_news_empty_url_becomes_none():
    d = filter_news(_news(), KD, KE)
    ext_fed = [n for n in d["external"] if n["title"].startswith("美联储官员")][0]
    assert ext_fed["url"] is None
    dom = [n for n in d["domestic"] if "央行与" in n["title"]][0]
    assert dom["url"] is None


def test_filter_news_flag_requires_keyword_and_amount():
    rows = [
        ("2026-11-05 10:00:00", "x", "央行开展互换便利操作", "u"),            # 无金额
        ("2026-11-05 09:00:00", "x", "再贷款额度增加 3.5 万亿元", "u"),       # 有金额
        ("2026-11-05 08:00:00", "x", "央行今日净投放 800亿 逆回购", "u"),     # 有金额但无工具词
        ("2026-11-05 07:00:00", "x", "再贷款12 亿元落地", "u"),              # 数字与单位间空格
    ]
    df = pd.DataFrame(rows, columns=["pub_time", "src", "title", "url"])
    flags = {n["title"]: n["flag"] for n in filter_news(df, KD, KE)["domestic"]}
    assert flags["央行开展互换便利操作"] is False
    assert flags["再贷款额度增加 3.5 万亿元"] is True
    assert flags["央行今日净投放 800亿 逆回购"] is False
    assert flags["再贷款12 亿元落地"] is True


def test_filter_news_keywords_case_insensitive():
    df = pd.DataFrame([("2026-11-05 10:00:00", "x", "美国 cpi 数据超预期", "u")],
                      columns=["pub_time", "src", "title", "url"])
    assert len(filter_news(df, ["LPR"], ["CPI"])["external"]) == 1


def test_filter_news_untrusted_title_passthrough():
    t = "央行 {0} %s <script>alert(1)</script> {x.__class__}"
    df = pd.DataFrame([("2026-11-05 10:00:00", "x", t, "u")], columns=["pub_time", "src", "title", "url"])
    assert filter_news(df, KD, KE)["domestic"][0]["title"] == t


def test_filter_news_empty():
    empty = pd.DataFrame(columns=["pub_time", "src", "title", "url"])
    assert filter_news(empty, KD, KE) == {"domestic": [], "external": []}
    assert filter_news(pd.DataFrame(), KD, KE) == {"domestic": [], "external": []}


# ---------- load_policy_ops ----------

def test_load_policy_ops_header_only(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text("date,tool,amount_yi,cum_yi,url,note\n", encoding="utf-8")
    assert load_policy_ops(str(p)) == []


def test_load_policy_ops_rows(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text(
        "date,tool,amount_yi,cum_yi,url,note\n"
        "2026-10-20,证券基金保险公司互换便利,500,1000.5,http://x/1,首批\n"
        "2026-10-27,回购增持再贷款,,,http://x/2,\n",
        encoding="utf-8")
    rows = load_policy_ops(str(p))
    assert rows[0] == {"date": "2026-10-20", "tool": "证券基金保险公司互换便利", "amount": 500.0,
                       "cum": 1000.5, "url": "http://x/1", "note": "首批"}
    assert rows[1]["amount"] is None and rows[1]["cum"] is None and rows[1]["note"] is None
    assert type(rows[0]["amount"]) is float


def test_load_policy_ops_bom_and_blank_lines(tmp_path):
    p = tmp_path / "p.csv"
    p.write_bytes("﻿date,tool,amount_yi,cum_yi,url,note\n\n2026-10-20,T,1,2,u,n\n,,,,,\n".encode("utf-8"))
    rows = load_policy_ops(str(p))
    assert len(rows) == 1 and rows[0]["date"] == "2026-10-20"


def test_load_policy_ops_bad_number_becomes_none_with_warning(tmp_path, caplog):
    p = tmp_path / "p.csv"
    p.write_text("date,tool,amount_yi,cum_yi,url,note\n"
                 "2026-10-20,T,12亿,nan,u,n\n2026-10-21,T2,3,4,u,n\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        rows = load_policy_ops(str(p))
    assert len(rows) == 2
    assert rows[0]["amount"] is None and rows[0]["cum"] is None and rows[0]["tool"] == "T"
    assert rows[1]["amount"] == 3.0 and rows[1]["cum"] == 4.0
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(msgs) == 2 and all("p.csv:2" in m for m in msgs)


def test_load_policy_ops_missing_file_returns_empty_with_warning(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        assert load_policy_ops(str(tmp_path / "nope.csv")) == []
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_filter_news_drops_null_or_empty_pub_time():
    rows = [
        (None, "x", "央行有消息A", "u"),
        (float("nan"), "x", "央行有消息B", "u"),
        ("", "x", "央行有消息C", "u"),
        ("  ", "x", "央行有消息D", "u"),
        ("2026-11-05 10:00:00", "x", "央行有消息E", "u"),
    ]
    df = pd.DataFrame(rows, columns=["pub_time", "src", "title", "url"])
    d = filter_news(df, KD, KE)
    assert [n["title"] for n in d["domestic"]] == ["央行有消息E"]
    assert all(n["time"] not in ("nan", "None", "") for n in d["domestic"] + d["external"])


def test_load_policy_ops_gbk_file_returns_empty_with_warning(tmp_path, caplog):
    p = tmp_path / "gbk.csv"
    p.write_bytes("date,tool,amount_yi,cum_yi,url,note\n2026-10-20,证券基金保险公司互换便利,500,1000,u,首批\n".encode("gbk"))
    with caplog.at_level("WARNING"):
        assert load_policy_ops(str(p)) == []
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(msgs) == 1 and "gbk.csv" in msgs[0] and "UnicodeDecodeError" in msgs[0]


def test_filter_news_keeps_timestamp_pub_time_as_string():
    rows = [
        (pd.Timestamp("2026-11-05 10:00:00"), "x", "央行有消息T", "u"),
        ("2026-11-04 09:00:00", "x", "央行有消息S", "u"),
        (pd.NaT, "x", "央行有消息N", "u"),
    ]
    df = pd.DataFrame(rows, columns=["pub_time", "src", "title", "url"])
    d = filter_news(df, KD, KE)
    assert [n["title"] for n in d["domestic"]] == ["央行有消息T", "央行有消息S"]
    assert d["domestic"][0]["time"] == "2026-11-05 10:00:00" and isinstance(d["domestic"][0]["time"], str)
