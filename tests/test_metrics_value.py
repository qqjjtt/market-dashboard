import numpy as np
import pandas as pd
import pytest

import config
import metrics_value as mv

CFG = config.VALUE


def test_percentile_rank():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, np.nan])
    assert mv.percentile_rank(s, 4.0) == 100.0
    assert mv.percentile_rank(s, 1.0) == 25.0
    assert mv.percentile_rank(s, 0.5) == 0.0
    assert mv.percentile_rank(s, 2.5) == 50.0
    assert mv.percentile_rank(pd.Series([np.nan]), 1.0) is None
    assert mv.percentile_rank(pd.Series(dtype=float), 1.0) is None


def _frame(dates, pbs):
    return pd.DataFrame({"trade_date": dates, "pb": pbs})


def test_pb_rows_full_and_listed_since_and_missing():
    names = [("A.SH", "甲"), ("B.SH", "乙"), ("C.SH", "丙"), ("D.SH", "丁")]
    full = _frame(["20160104", "20180102", "20261105", "20261106"], [1.0, 3.0, 2.0, 2.0])
    short = _frame(["20200723", "20261106"], [4.0, 2.0])
    frames = {"A.SH": full, "B.SH": short, "C.SH": _frame([], []), "D.SH": None}
    rows = mv.pb_rows(frames, names, "20161106")
    assert [r["code"] for r in rows] == ["A.SH", "B.SH", "C.SH", "D.SH"]
    a, b, c, d = rows
    # 窗口（20161106 起）：[3.0, 2.0, 2.0]，最新 2.0 → 3 个里 <=2.0 的有 2 个
    assert a["pb"] == 2.0 and a["pct"] == pytest.approx(66.7) and a["since"] == "20180102" and a["full"] is False
    assert b["pct"] == 50.0 and b["since"] == "20200723" and b["full"] is False
    for r in (c, d):
        assert r["pb"] is None and r["pct"] is None and r["since"] is None and r["full"] is False
    ok = mv.pb_rows({"A.SH": _frame(["20161110", "20261106"], [1.5, 1.2])}, [("A.SH", "甲")], "20161106")[0]
    assert ok["full"] is True and ok["since"] == "20161110" and ok["pb"] == 1.2 and ok["pct"] == 50.0


def test_level():
    assert mv.level(None, 30, 70) is None
    assert mv.level(29.9, 30, 70) == "偏低"
    assert mv.level(30, 30, 70) == "中等"
    assert mv.level(70, 30, 70) == "中等"
    assert mv.level(70.1, 30, 70) == "偏高"
    assert mv.level(80, 30, 70, labels=("偏轻", "中等", "偏重")) == "偏重"


def test_board_of():
    assert mv.board_of("688001.SH") == "科创板" and mv.board_of("689009.SH") == "科创板"
    assert mv.board_of("300750.SZ") == "创业板" and mv.board_of("301001.SZ") == "创业板"
    assert mv.board_of("302132.SZ") == "创业板"
    assert mv.board_of("600519.SH") == "沪深主板" and mv.board_of("000001.SZ") == "沪深主板"
    assert mv.board_of("002001.SZ") == "沪深主板"
    assert mv.board_of("830001.BJ") is None and mv.board_of("430047.BJ") is None
    assert mv.board_of(None) is None


def test_chips_summary_weighting_and_deep_share():
    win = pd.DataFrame({"ts_code": ["600001.SH", "600002.SH", "300001.SZ", "688001.SH", "830001.BJ"],
                        "winner_rate": [80.0, 40.0, 5.0, 60.0, 1.0]})
    mvdf = pd.DataFrame({"ts_code": ["600001.SH", "600002.SH", "300001.SZ", "688001.SH", "830001.BJ"],
                         "circ_mv": [100.0, 300.0, 100.0, 100.0, 100.0]})
    r = mv.chips_summary(win, mvdf, 10)
    # 加权获利盘 (80*100+40*300+5*100+60*100)/600 = 44.1667 → 套牢 55.8
    assert r["trapped"] == 55.8
    boards = {b["name"]: b["trapped"] for b in r["boards"]}
    assert [b["name"] for b in r["boards"]] == ["沪深主板", "创业板", "科创板"]
    assert boards == {"沪深主板": 50.0, "创业板": 95.0, "科创板": 40.0}
    assert r["deep_share"] == 25.0  # 剔北交所后 4 只里 1 只 <10


def test_chips_summary_missing():
    r = mv.chips_summary(None, None, 10)
    assert r["trapped"] is None and r["deep_share"] is None
    assert [b["trapped"] for b in r["boards"]] == [None, None, None]
    win = pd.DataFrame({"ts_code": ["600001.SH"], "winner_rate": [50.0]})
    r = mv.chips_summary(win, None, 10)
    assert r["trapped"] is None and r["deep_share"] == 0.0


def _mk(n, rzye=None, rzmre=None, fm=None, amt=None, start="2026-01-01"):
    d = [x.strftime("%Y%m%d") for x in pd.bdate_range(start, periods=n)]
    m = pd.DataFrame({"trade_date": d, "rzye": rzye if rzye is not None else [25000.0] * n,
                      "rzmre": rzmre if rzmre is not None else [1500.0] * n})
    s = pd.DataFrame({"trade_date": d, "float_mv": fm if fm is not None else [900000.0] * n,
                      "amount": amt if amt is not None else [16000.0] * n})
    return m, s


def test_margin_metrics_values():
    n = 300
    rzye = list(np.linspace(20000, 30000, n))
    rzye[-1] = 27000.0
    m, s = _mk(n, rzye=rzye)
    r = mv.margin_metrics(m, s, CFG)
    assert r["date"] == m["trade_date"].iloc[-1]
    assert r["rzye"] == 27000 and r["lev"] == pytest.approx(3.0)
    assert r["buy_ratio"] == pytest.approx(9.38, abs=0.01)  # 1500/16000*100
    assert r["drawdown"] == pytest.approx(round((27000 / max(rzye[-250:]) - 1) * 100, 1))
    assert r["net5"] == round(27000 - rzye[-6])
    assert len(r["lev_hist"]["dates"]) == 250 == len(r["lev_hist"]["values"])
    assert r["lev_hist"]["values"][-1] == 3.0


def test_margin_metrics_verdicts():
    n = 100
    # 干净：杠杆率与买入占比都处于历史低位
    rzye = list(np.linspace(30000, 20000, n))
    rzmre = list(np.linspace(2000, 1000, n))
    m, s = _mk(n, rzye=rzye, rzmre=rzmre)
    r = mv.margin_metrics(m, s, CFG)
    assert r["lev_pct"] < 40 and r["buy_pct"] < 40 and r["verdict"] == "干净"
    # 拥挤
    m, s = _mk(n, rzye=rzye[::-1], rzmre=rzmre[::-1])
    r = mv.margin_metrics(m, s, CFG)
    assert r["lev_pct"] > 70 and r["buy_pct"] > 70 and r["verdict"] == "拥挤"
    # 中性：杠杆率高、买入占比低
    m, s = _mk(n, rzye=rzye[::-1], rzmre=rzmre)
    r = mv.margin_metrics(m, s, CFG)
    assert r["verdict"] == "中性"


def test_margin_metrics_insufficient_and_empty():
    m, s = _mk(3)
    r = mv.margin_metrics(m, s, CFG)  # 3 个观测：5 日均值与 5 日变化都算不出
    assert r["lev"] is not None and r["buy_ratio"] is None and r["buy_pct"] is None
    assert r["net5"] is None and r["verdict"] is None
    for bad in (None, pd.DataFrame()):
        e = mv.margin_metrics(bad, bad, CFG)
        assert e["verdict"] is None and e["lev"] is None and e["lev_hist"] == {"dates": [], "values": []}


def test_margin_metrics_dates_not_aligned():
    m, s = _mk(30)
    s = s.iloc[:20].copy()  # 市场数据缺最近 10 天
    r = mv.margin_metrics(m, s, CFG)
    assert r["date"] == s["trade_date"].iloc[-1]
    s2 = s.copy()
    s2["trade_date"] = "19990101"
    r = mv.margin_metrics(m, s2, CFG)
    assert r["date"] is None and r["verdict"] is None
