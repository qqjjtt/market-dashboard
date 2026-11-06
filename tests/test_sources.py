"""TushareSource 单元测试：全部使用假 pro 对象，不触网、不使用真实 token。"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from sources import PermissionDenied, SourceError, TushareSource


class FakePro:
    """api 名 -> DataFrame / 异常 / callable(**kw)。记录每次调用。"""

    def __init__(self, **apis):
        self.apis = apis
        self.calls = []

    def __getattr__(self, name):
        if name in ("apis", "calls"):
            raise AttributeError(name)

        def fn(**kw):
            self.calls.append((name, kw))
            v = self.apis[name]
            if isinstance(v, Exception):
                raise v
            if callable(v):
                return v(**kw)
            return v.copy()

        return fn


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(pro, **kw):
    slept = []
    src = TushareSource(pro, sleep=slept.append, clock=kw.pop("clock", Clock()), **kw)
    return src, slept


# ---------------------------------------------------------------- call()

def test_retry_backoff():
    state = {"n": 0}

    def flaky(**kw):
        state["n"] += 1
        if state["n"] <= 2:
            raise Exception("timeout")
        return pd.DataFrame({"a": [1]})

    src, slept = make(FakePro(daily=flaky))
    df = src.call("daily", trade_date="20261105")
    assert slept == [2, 4] and len(df) == 1


def test_retry_gives_up():
    src, slept = make(FakePro(daily=Exception("timeout")))
    with pytest.raises(SourceError):
        src.call("daily", trade_date="20261105")
    assert slept == [2, 4, 8]


def test_permission_no_retry():
    src, slept = make(FakePro(yc_cb=Exception("抱歉，您没有接口(yc_cb)访问权限，权限的具体详情访问：x")))
    with pytest.raises(PermissionDenied):
        src.call("yc_cb")
    assert slept == []


def test_rate_limit_is_retried_not_permission():
    msg = "抱歉，您访问接口(us_daily)频率超限(1次/分钟)，权限的具体详情访问：x"
    src, slept = make(FakePro(us_daily=Exception(msg)))
    with pytest.raises(SourceError):
        src.call("us_daily")
    assert slept == [2, 4, 8]


def test_none_result_becomes_empty_df():
    src, _ = make(FakePro(daily=lambda **kw: None))
    df = src.call("daily")
    assert isinstance(df, pd.DataFrame) and df.empty


def test_throttle():
    src, slept = make(FakePro(daily=pd.DataFrame({"a": [1]})), max_calls_per_min=2)
    src.call("daily")
    src.call("daily")
    assert slept == []
    src.call("daily")
    assert len(slept) == 1 and slept[0] == pytest.approx(60)


def test_throttle_window_slides():
    clock = Clock()
    src, slept = make(FakePro(daily=pd.DataFrame({"a": [1]})), max_calls_per_min=2, clock=clock)
    src.call("daily")
    src.call("daily")
    clock.t = 61
    src.call("daily")
    assert slept == []


# ---------------------------------------------------------------- 单位与列

def test_units():
    pro = FakePro(
        fund_share=pd.DataFrame({"trade_date": ["20261105"], "fd_share": [12345.0]}),
        fund_daily=pd.DataFrame({"trade_date": ["20261105"], "close": [4.5], "amount": [123456.0]}),
        margin=pd.DataFrame({"trade_date": ["20261105", "20261105"], "exchange_id": ["SSE", "SZSE"],
                             "rzrqye": [1.25e9, 1.25e9]}),
        moneyflow_mkt_dc=pd.DataFrame({"trade_date": ["20261105"], "net_amount": [-5.0e10]}),
        daily=pd.DataFrame({"ts_code": ["000001.SZ"], "pct_chg": [1.0], "amount": [123456.0]}),
    )
    src, _ = make(pro)
    assert src.etf_share("510300.SH", "20261101", "20261105").share.iloc[0] == pytest.approx(1.2345)
    assert src.etf_daily("510300.SH", "20261101", "20261105").amount.iloc[0] == pytest.approx(1.23456)
    m = src.margin("20261101", "20261105")
    assert len(m) == 1 and m.balance.iloc[0] == pytest.approx(25.0)
    assert src.market_flow("20261101", "20261105").net_amount.iloc[0] == pytest.approx(-500.0)
    assert src.stock_daily("20261105").amount.iloc[0] == pytest.approx(1.23456)


def test_trade_date_is_str():
    pro = FakePro(fund_share=pd.DataFrame({"trade_date": [20261105], "fd_share": [1.0]}))
    src, _ = make(pro)
    assert src.etf_share("510300.SH", "20261101", "20261105").trade_date.iloc[0] == "20261105"


def test_empty_returns_empty_df_with_columns():
    src, _ = make(FakePro(daily=pd.DataFrame(), margin=pd.DataFrame(), fund_share=pd.DataFrame()))
    d = src.stock_daily("20261105")
    assert d.empty and list(d.columns) == ["ts_code", "pct_chg", "amount"]
    assert src.margin("20261101", "20261105").empty
    assert list(src.etf_share("510300.SH", "20261101", "20261105").columns) == ["trade_date", "share"]


def test_trade_days():
    cal = pd.DataFrame({"cal_date": ["20261104", "20261102", "20261103", "20261105"]})
    pro = FakePro(trade_cal=cal)
    src, _ = make(pro)
    assert src.trade_days("20261105", 3) == ["20261103", "20261104", "20261105"]
    name, kw = pro.calls[0]
    assert kw["end_date"] == "20261105" and kw["is_open"] == 1


def test_etf_names():
    pro = FakePro(fund_basic=pd.DataFrame({"ts_code": ["510300.SH", "510500.SH"], "name": ["沪深300ETF", "中证500ETF"]}))
    src, _ = make(pro)
    assert src.etf_names(["510300.SH", "999999.SH"]) == {"510300.SH": "沪深300ETF"}


def test_sw_members_paginates():
    def page(**kw):
        off = kw.get("offset", 0)
        n = 3000 if off == 0 else 5
        return pd.DataFrame({"ts_code": [f"{i}" for i in range(n)], "l1_name": "a", "l2_name": "b",
                             "l3_name": "c", "extra": 1})

    pro = FakePro(index_member_all=page)
    src, _ = make(pro)
    df = src.sw_members()
    assert len(df) == 3005 and list(df.columns) == ["ts_code", "l1_name", "l2_name", "l3_name"]
    assert pro.calls[0][1]["is_new"] == "Y" and pro.calls[1][1]["offset"] == 3000


def test_sw_members_permission_returns_none():
    src, _ = make(FakePro(index_member_all=Exception("抱歉，您没有接口(index_member_all)访问权限")))
    assert src.sw_members() is None


def test_etf_minutes_permission_returns_none():
    src, _ = make(FakePro(stk_mins=Exception("抱歉，您没有接口(stk_mins)访问权限")))
    assert src.etf_minutes("510300.SH", "20261105") is None


def test_etf_minutes_window_and_columns():
    mins = pd.DataFrame({"trade_time": ["2026-11-05 09:31:00"], "amount": [1.0], "close": [1.0]})
    pro = FakePro(stk_mins=mins)
    src, _ = make(pro)
    df = src.etf_minutes("510300.SH", "20261105")
    assert list(df.columns) == ["trade_time", "amount"]
    kw = pro.calls[0][1]
    assert kw["freq"] == "1min" and kw["start_date"] == "2026-11-05 09:30:00" and kw["end_date"] == "2026-11-05 15:00:00"


def test_limit_counts():
    lim = pd.DataFrame({"ts_code": list("abcd"), "limit": ["U", "U", "D", "Z"]})
    src, _ = make(FakePro(limit_list_d=lim))
    assert src.limit_counts("20261105") == {"up": 2, "down": 1}
    src2, _ = make(FakePro(limit_list_d=pd.DataFrame()))
    assert src2.limit_counts("20261105") == {"up": None, "down": None}


def test_inst_net_buy_unit_yuan_and_dedupe():
    # 同一席位同时在买入榜/卖出榜（side 0/1）会重复出现，应只计一次；单位元 -> 亿元
    ti = pd.DataFrame({
        "ts_code": ["a", "a", "a", "b"],
        "exalter": ["机构专用", "机构专用", "某营业部", "机构专用"],
        "buy": [3e8, 3e8, 9e8, 1e8], "sell": [1e8, 1e8, 0.0, 0.0],
        "net_buy": [2e8, 2e8, 9e8, 1e8], "side": [0, 1, 0, 0],
    })
    src, _ = make(FakePro(top_inst=ti))
    assert src.inst_net_buy("20261105") == pytest.approx(3.0)
    src2, _ = make(FakePro(top_inst=pd.DataFrame()))
    assert src2.inst_net_buy("20261105") is None


def test_shibor_renames_1w():
    sh = pd.DataFrame({"date": ["20261105"], "on": [1.3], "1w": [1.4], "2w": [1.5]})
    src, _ = make(FakePro(shibor=sh))
    df = src.shibor("20261101", "20261105")
    assert list(df.columns) == ["date", "on", "w1"] and df.w1.iloc[0] == 1.4


def test_bond_fut_and_fx():
    pro = FakePro(
        fut_daily=pd.DataFrame({"ts_code": ["T.CFX"], "trade_date": ["20261105"], "close": [108.0]}),
        fx_daily=pd.DataFrame({"ts_code": ["UKOIL"], "trade_date": ["20261105"], "bid_close": [70.5]}),
    )
    src, _ = make(pro)
    assert src.bond_fut_10y("20261101", "20261105").close.iloc[0] == 108.0
    assert pro.calls[0][1]["ts_code"] == "T.CFX"
    fx = src.fx("UKOIL", "20261101", "20261105")
    assert list(fx.columns) == ["trade_date", "close"] and fx.close.iloc[0] == 70.5


def test_us_yields_and_m2():
    pro = FakePro(
        us_tycr=pd.DataFrame({"date": ["20261105"], "m1": [1], "y2": [3.9], "y10": [4.2]}),
        cn_m=pd.DataFrame({"month": ["202610"], "m2_yoy": [8.0], "m1": [1]}),
    )
    src, _ = make(pro)
    assert list(src.us_yields("20261101", "20261105").columns) == ["date", "y2", "y10"]
    assert list(src.m2("202610", "202610").columns) == ["month", "m2_yoy"]


# ---------------------------------------------------------------- major_news

FMT = "%Y-%m-%d %H:%M:%S"


def test_major_news_splits_full_window():
    seen = []

    def fake(start_date, end_date, fields=None):
        s, e = datetime.strptime(start_date, FMT), datetime.strptime(end_date, FMT)
        span = (e - s).total_seconds() / 60
        if span > 90:
            seen.append((span, 800))
            return pd.DataFrame({"title": [f"BIG{i}" for i in range(800)], "pub_time": start_date,
                                 "src": "x", "url": "u"})
        seen.append((span, 10))
        # 10 行，其中 3 个重复标题（与相邻窗口边界重叠时同样会出现）
        titles = [f"{start_date}-{i}" for i in range(7)] + ["dup"] * 3
        return pd.DataFrame({"title": titles, "pub_time": start_date, "src": "x", "url": "u"})

    src, _ = make(FakePro(major_news=fake))
    df = src.major_news("2026-11-06 00:00:00", "2026-11-06 06:00:00")
    assert not df.title.str.startswith("BIG").any()
    assert df.title.is_unique
    assert list(df.columns) == ["pub_time", "src", "title", "url"]
    assert all(n < 800 for span, n in seen if span <= 90)
    assert any(n == 800 for _, n in seen)  # 确实触发了二分


def test_major_news_stops_at_min_window():
    calls = []

    def always_full(start_date, end_date, fields=None):
        calls.append((start_date, end_date))
        return pd.DataFrame({"title": [f"{start_date}-{i}" for i in range(800)], "pub_time": start_date,
                             "src": "x", "url": "u"})

    src, _ = make(FakePro(major_news=always_full))
    df = src.major_news("2026-11-06 00:00:00", "2026-11-06 00:30:00")
    assert len(df) > 0
    for s, e in calls:
        assert (datetime.strptime(e, FMT) - datetime.strptime(s, FMT)) >= timedelta(minutes=0)
    assert len(calls) < 50  # 窗口 <= 10 分钟后不再二分


def test_major_news_empty():
    src, _ = make(FakePro(major_news=pd.DataFrame()))
    df = src.major_news("2026-11-06 00:00:00", "2026-11-06 03:00:00")
    assert df.empty and list(df.columns) == ["pub_time", "src", "title", "url"]


def test_from_env_requires_token(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(SourceError):
        TushareSource.from_env()


def test_results_sorted_ascending():
    pro = FakePro(fund_daily=pd.DataFrame({"trade_date": ["20261105", "20261103", "20261104"],
                                           "close": [3.0, 1.0, 2.0], "amount": [1.0, 1.0, 1.0]}))
    src, _ = make(pro)
    assert list(src.etf_daily("510300.SH", "20261101", "20261105").close) == [1.0, 2.0, 3.0]
