import numpy as np
import pandas as pd
import pytest

from metrics_etf import (
    aggregate,
    amount_groups,
    fingerprint_conclusion,
    monthly_totals,
    net_flow,
    ratio_footnote,
    round_summary,
    tail_ratio,
)


def test_net_flow_keeps_nan():
    share = pd.DataFrame({"A": [10.0, 10.5, np.nan]}, index=["d1", "d2", "d3"])
    close = pd.DataFrame({"A": [4.0, 4.0, 4.0]}, index=["d1", "d2", "d3"])
    out = net_flow(share, close)
    assert np.isnan(out.A["d1"]) and out.A["d2"] == pytest.approx(2.0) and np.isnan(out.A["d3"])


def test_net_flow_nan_close_and_prev_share():
    share = pd.DataFrame({"A": [10.0, 11.0, 12.0, 11.0]}, index=list("abcd"))
    close = pd.DataFrame({"A": [1.0, np.nan, 2.0, 2.0]}, index=list("abcd"))
    out = net_flow(share, close)
    assert np.isnan(out.A["b"])  # 收盘价缺
    assert out.A["c"] == pytest.approx(2.0)
    assert out.A["d"] == pytest.approx(-2.0)


def test_net_flow_aligns_columns_and_negative():
    share = pd.DataFrame({"A": [10.0, 9.0], "B": [5.0, 5.0]}, index=["d1", "d2"])
    close = pd.DataFrame({"A": [2.0, 2.0]}, index=["d1", "d2"])  # B 无收盘
    out = net_flow(share, close)
    assert out.A["d2"] == pytest.approx(-2.0)
    assert np.isnan(out.B["d2"])


def _five(rows):
    return pd.DataFrame(rows, columns=list("ABCDE"), index=[f"d{i}" for i in range(len(rows))])


def test_aggregate_disclosure_rule():
    n = np.nan
    net = _five([[1, 1, 1, n, n], [1, 2, 3, 4, n], [1, 1, 1, 1, 1], [n, n, n, n, n]])
    out = aggregate(net)
    assert np.isnan(out["d0"])          # 3/5 < 0.8
    assert out["d1"] == pytest.approx(10.0)  # 4/5 → 4 只之和
    assert out["d2"] == pytest.approx(5.0)
    assert np.isnan(out["d3"])          # 全空不是 0


def test_aggregate_keeps_negative_sum_and_custom_ratio():
    n = np.nan
    net = _five([[-1, -2, n, n, n]])
    assert np.isnan(aggregate(net).iloc[0])
    assert aggregate(net, min_ratio=0.4).iloc[0] == pytest.approx(-3.0)


def test_round_summary_attribution():
    r = round_summary(pd.Series([60.0, np.nan, 40.0], index=["20261103", "20261104", "20261105"]),
                      "20261103", (0.7, 0.9), 29)
    assert (r["total"], r["low"], r["high"], r["mid"]) == (100.0, 70.0, 90.0, 80.0)
    assert r["last3"][1] == {"date": "20261104", "value": None}
    assert r["last3"][0] == {"date": "20261105", "value": 40.0}
    assert r["last3"][2] == {"date": "20261103", "value": 60.0}
    assert r["count"] == 29 and r["start"] == "20261103" and r["end"] == "20261105"
    assert r["attribution"] == [0.7, 0.9]


def test_round_summary_end_is_last_non_nan_day():
    s = pd.Series([60.0, 40.0, np.nan], index=["20261103", "20261104", "20261105"])
    r = round_summary(s, "20261103", (0.7, 0.9), 29)
    assert r["end"] == "20261104" and r["total"] == 100.0
    assert r["last3"][0] == {"date": "20261105", "value": None}  # 最近三日仍如实列出待披露日


def test_round_summary_start_slices_and_all_nan():
    s = pd.Series([5.0, 1.0, 2.0, 3.0], index=["20261030", "20261102", "20261103", "20261104"])
    r = round_summary(s, "20261102", (0.7, 0.9), 3)
    assert r["total"] == pytest.approx(6.0) and r["start"] == "20261102"
    assert [x["date"] for x in r["last3"]] == ["20261104", "20261103", "20261102"]
    n = round_summary(pd.Series([np.nan, np.nan], index=["20261103", "20261104"]), "20261103", (0.7, 0.9), 2)
    assert n["total"] is None and n["low"] is None and n["high"] is None and n["mid"] is None and n["end"] is None
    e = round_summary(pd.Series([], dtype=float), "20261103", (0.7, 0.9), 0)
    assert e["total"] is None and e["last3"] == [] and e["end"] is None


def test_monthly_totals_labels():
    s = pd.Series([1.0, np.nan, 5.0], index=["20261030", "20261031", "20261102"])
    out = monthly_totals(s, 6)
    assert [o["label"] for o in out] == ["10月", "11月"]
    assert [o["days"] for o in out] == [2, 1]
    assert out[0]["value"] == pytest.approx(1.0) and out[1]["value"] == pytest.approx(5.0)


def test_monthly_totals_limits_months_and_none_for_all_nan():
    idx = ["20260901", "20261001", "20261002", "20261101"]
    s = pd.Series([1.0, np.nan, np.nan, 2.0], index=idx)
    out = monthly_totals(s, 2)
    assert [o["label"] for o in out] == ["10月", "11月"]
    assert out[0]["value"] is None and out[0]["days"] == 2


def test_amount_groups_basic():
    amt = pd.DataFrame({"A": [100.0, 60.0], "B": [100.0, 70.0]}, index=["20261104", "20261105"])
    g = amount_groups(amt, "20261105", "20261104", {"组": ["A", "B"]})[0]
    assert g["today"] == pytest.approx(130.0) and g["yday"] == pytest.approx(200.0)
    assert g["chg_pct"] == -35 and g["ratio"] == 65 and g["n"] == 2
    assert isinstance(g["chg_pct"], int)


def test_amount_groups_zero_yday():
    amt = pd.DataFrame({"A": [0.0, 7.0]}, index=["20261104", "20261105"])
    g = amount_groups(amt, "20261105", "20261104", {"组": ["A"]})[0]
    assert g["chg_pct"] is None and g["ratio"] is None and g["today"] == 7.0


def test_amount_groups_nan_yday_and_missing_cols():
    amt = pd.DataFrame({"A": [np.nan, 7.0]}, index=["20261104", "20261105"])
    out = amount_groups(amt, "20261105", "20261104", {"g1": ["A"], "g2": ["ZZ"]})
    assert out[0]["yday"] is None and out[0]["chg_pct"] is None
    assert out[1]["today"] is None and out[1]["ratio"] is None and out[1]["n"] == 0


def test_tail_ratio():
    m = pd.DataFrame({"trade_time": ["2026-11-05 14:29:00", "2026-11-05 14:31:00", "2026-11-05 14:59:00"],
                      "amount": [100.0, 50.0, 50.0]})
    assert tail_ratio(m) == pytest.approx(50.0)


def test_tail_ratio_edges():
    assert tail_ratio(None) is None
    assert tail_ratio(pd.DataFrame({"trade_time": [], "amount": []})) is None
    m = pd.DataFrame({"trade_time": ["2026-11-05 14:30:00", "2026-11-05 09:31:00"], "amount": [30.0, 70.0]})
    assert tail_ratio(m) == pytest.approx(0.0)  # 14:30 本身不算尾盘
    z = pd.DataFrame({"trade_time": ["2026-11-05 14:31:00"], "amount": [0.0]})
    assert tail_ratio(z) is None
    m2 = pd.DataFrame({"trade_time": ["2026-11-05 14:31:00", "2026-11-05 10:00:00"], "amount": [1.0, 3.0]})
    assert tail_ratio(m2, start="09:30") == pytest.approx(100.0)


def test_conclusion_case1():
    assert fingerprint_conclusion(-25, 5) == "宽基通道明显降温、行业 ETF 反而 +5%——托底的钱在收手，进攻的钱在换标的。但成交降温 ≠ 净赎回，方向要等份额回填才能定。"


def test_conclusion_other_cases():
    assert "资金向宽基集中" in fingerprint_conclusion(15, -3)
    assert "整体活跃度上升" in fingerprint_conclusion(12, 11)
    assert "整体降温" in fingerprint_conclusion(-12, -11)
    assert "变化不大" in fingerprint_conclusion(3, 2)
    assert "数据不足" in fingerprint_conclusion(None, 5)
    assert "数据不足" in fingerprint_conclusion(5, None)


def test_conclusion_boundaries_and_signs():
    assert "变化不大" in fingerprint_conclusion(-10, 5)   # 恰为 −10 不算 < −10
    assert "变化不大" in fingerprint_conclusion(-25, 0)   # 行业 0 不算 > 0
    assert "−3%" in fingerprint_conclusion(15, -3)         # 负号用 −


def _g(name, ratio):
    return {"key": name, "name": name, "ratio": ratio}


def test_ratio_footnote():
    gs = [_g("核心五通道", 60), _g("宽基十只", 70), _g("行业对照六只", 105), _g("全部十六只", 80)]
    assert ratio_footnote(gs) == "只有行业对照六只越过了 100%（+5%），其余全部低于 100%"
    gs = [_g("核心五通道", 60), _g("宽基十只", 70), _g("行业对照六只", 99), _g("全部十六只", 80)]
    assert ratio_footnote(gs) == "四组全部低于 100%"


def test_ratio_footnote_multi_none_and_all_over():
    gs = [_g("甲", 120), _g("乙", 110), _g("丙", 50), _g("丁", None)]
    assert ratio_footnote(gs) == "只有甲、乙越过了 100%（+20%、+10%），其余全部低于 100%"
    assert "全部" in ratio_footnote([_g("甲", 120), _g("乙", 110)]) and "越过" in ratio_footnote([_g("甲", 120), _g("乙", 110)])
    assert ratio_footnote([_g("甲", None)]) == ""


# ---------------------------------------------------------------- 观察池天量

from metrics_etf import volume_spikes


def _days(n):
    return [f"2026{m:02d}{d:02d}" for m in range(7, 12) for d in range(1, 29)][:n]


def _spike_frames(n=70):
    days = _days(n)
    amount = pd.DataFrame({"A": [10.0] * n, "B": [10.0] * n}, index=days)
    net = pd.DataFrame({"A": [1.0, -1.0] * (n // 2), "B": [1.0, -1.0] * (n // 2)}, index=days)
    return days, amount, net


def test_volume_spikes_quiet_day_is_empty():
    days, amount, net = _spike_frames()
    assert volume_spikes(amount, net, days[-1], {}) == []


def test_volume_spike_by_amount():
    days, amount, net = _spike_frames()
    amount.loc[days[-1], "A"] = 35.0  # 3.5 倍
    out = volume_spikes(amount, net, days[-1], {"A": "甲ETF"})
    assert len(out) == 1
    s = out[0]
    assert s["code"] == "A" and s["name"] == "甲ETF" and s["by"] == ["成交额"]
    assert s["amount"] == pytest.approx(35.0) and s["amount_ratio"] == pytest.approx(3.5)


def test_volume_spike_by_net_flow_needs_both_multiple_and_floor():
    days, amount, net = _spike_frames()
    net.loc[days[-1], "A"] = 9.0  # 9 倍但不足 10 亿 → 不算
    net.loc[days[-1], "B"] = -12.0  # 12 倍且 ≥10 亿，净赎回也算
    out = volume_spikes(amount, net, days[-1], {})
    assert [s["code"] for s in out] == ["B"]
    assert out[0]["by"] == ["净申赎"] and out[0]["net"] == pytest.approx(-12.0)
    assert out[0]["net_date"] == days[-1] and out[0]["flow_ratio"] == pytest.approx(12.0)


def test_volume_spike_uses_previous_day_net_when_today_not_disclosed():
    days, amount, net = _spike_frames()
    net.loc[days[-2], "B"] = 30.0
    net.loc[days[-1], "B"] = np.nan  # 深市当日未披露
    out = volume_spikes(amount, net, days[-1], {})
    assert out[0]["code"] == "B" and out[0]["net_date"] == days[-2]


def test_volume_spike_ignores_stale_net_and_thin_history():
    days, amount, net = _spike_frames()
    net.loc[days[-3], "A"] = 30.0  # 两天前的大额，不再提示
    net.loc[days[-2]:, "A"] = np.nan
    assert volume_spikes(amount, net, days[-1], {}) == []
    short_days, short_amt, short_net = _spike_frames(8)
    short_amt.loc[short_days[-1], "A"] = 100.0  # 历史不足 → 不判
    assert volume_spikes(short_amt, short_net, short_days[-1], {}) == []


def test_volume_spikes_sorted_by_strength_and_missing_today():
    days, amount, net = _spike_frames()
    amount.loc[days[-1], "A"] = 40.0
    amount.loc[days[-1], "B"] = 60.0
    out = volume_spikes(amount, net, days[-1], {})
    assert [s["code"] for s in out] == ["B", "A"]
    assert volume_spikes(amount, net, "20991231", {}) == []
