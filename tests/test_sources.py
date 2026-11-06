"""TushareSource 单元测试：全部使用假 pro 对象，不触网、不使用真实 token。"""
from datetime import datetime, timedelta

import logging

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
    assert src.etf_minutes("510300.SH", "20261105", "20261105") is None


def test_etf_minutes_window_and_columns():
    mins = pd.DataFrame({"trade_time": ["2026-11-05 09:31:00"], "amount": [1.0], "close": [1.0]})
    pro = FakePro(stk_mins=mins)
    src, _ = make(pro)
    df = src.etf_minutes("510300.SH", "20261104", "20261105")
    assert list(df.columns) == ["trade_time", "amount"]
    assert len(pro.calls) == 1  # 一次调用覆盖整个区间
    kw = pro.calls[0][1]
    assert kw["freq"] == "1min" and kw["start_date"] == "2026-11-04 09:30:00" and kw["end_date"] == "2026-11-05 15:00:00"


def test_etf_minutes_ascending():
    mins = pd.DataFrame({"trade_time": ["2026-11-05 09:32:00", "2026-11-05 09:31:00"], "amount": [2.0, 1.0]})
    src, _ = make(FakePro(stk_mins=mins))
    assert list(src.etf_minutes("510300.SH", "20261105", "20261105").amount) == [1.0, 2.0]


def test_api_min_interval_stk_mins():
    pro = FakePro(stk_mins=pd.DataFrame({"trade_time": ["t"], "amount": [1.0]}),
                  daily=pd.DataFrame({"a": [1]}))
    src, slept = make(pro)
    src.etf_minutes("510300.SH", "20261105", "20261105")
    assert slept == []
    src.call("daily")
    src.call("daily")
    assert slept == []  # 其他接口不受间隔限制
    src.etf_minutes("510159.SZ", "20261105", "20261105")
    assert len(slept) == 1 and slept[0] == pytest.approx(61)


def test_api_min_interval_elapsed_no_wait():
    clock = Clock()
    pro = FakePro(stk_mins=pd.DataFrame({"trade_time": ["t"], "amount": [1.0]}))
    src, slept = make(pro, clock=clock)
    src.etf_minutes("510300.SH", "20261105", "20261105")
    clock.t = 30
    src.etf_minutes("510300.SH", "20261105", "20261105")
    assert slept == [pytest.approx(31)]


def test_stk_mins_rate_limit_returns_none_without_retry(caplog):
    pro = FakePro(stk_mins=Exception("抱歉，您访问接口(stk_mins)频率超限(1次/分钟)，权限的具体详情"))
    src, slept = make(pro)
    with caplog.at_level(logging.WARNING, logger="sources"):
        assert src.etf_minutes("510300.SH", "20261105", "20261105") is None
    assert slept == [] and len(pro.calls) == 1
    assert any("频率超限" in r.getMessage() for r in caplog.records)


def test_other_api_rate_limit_still_retried():
    src, slept = make(FakePro(daily=Exception("抱歉，您访问接口(daily)频率超限(1次/分钟)")))
    with pytest.raises(SourceError):
        src.call("daily")
    assert slept == [2, 4, 8]


def test_retry_warning_logs_type_only(caplog):
    secret = "token=SECRET123 boom"
    src, _ = make(FakePro(daily=Exception(secret)))
    with caplog.at_level(logging.WARNING, logger="sources"):
        with pytest.raises(SourceError):
            src.call("daily")
    retry_logs = [r.getMessage() for r in caplog.records if "重试" in r.getMessage()]
    assert len(retry_logs) == 3
    assert all("Exception" in m and "SECRET123" not in m for m in retry_logs)


def test_missing_columns_warns_and_returns_empty(caplog):
    src, _ = make(FakePro(daily=pd.DataFrame({"ts_code": ["a"], "foo": [1]})))
    with caplog.at_level(logging.WARNING, logger="sources"):
        d = src.stock_daily("20261105")
    assert d.empty and list(d.columns) == ["ts_code", "pct_chg", "amount"]
    assert any("缺少预期列" in r.getMessage() for r in caplog.records)


def test_margin_drops_partial_day_and_warns(caplog):
    df = pd.DataFrame({
        "trade_date": ["20261104"] * 3 + ["20261105"] * 2,
        "exchange_id": ["SSE", "SZSE", "BSE", "SSE", "SZSE"],
        "rzrqye": [1e10, 1e10, 1e9, 1e10, 1e10],
    })
    src, _ = make(FakePro(margin=df))
    with caplog.at_level(logging.WARNING, logger="sources"):
        m = src.margin("20261104", "20261105")
    assert list(m.trade_date) == ["20261104"] and m.balance.iloc[0] == pytest.approx(210.0)
    assert any("margin" in r.getMessage() and "20261105" in r.getMessage() for r in caplog.records)


def test_margin_all_nan_day_dropped_not_zero():
    nan = float("nan")
    df = pd.DataFrame({
        "trade_date": ["20261104"] * 2 + ["20261105"] * 2,
        "exchange_id": ["SSE", "SZSE"] * 2,
        "rzrqye": [1e10, 1e10, nan, nan],
    })
    src, _ = make(FakePro(margin=df))
    m = src.margin("20261104", "20261105")
    assert list(m.trade_date) == ["20261104"]


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


def _news_pro(rows):
    """模拟 major_news：窗口内按时间倒序返回最新的 800 行（与真实接口行为一致）。"""
    state = {"calls": []}

    def fake(start_date, end_date, fields=None):
        state["calls"].append((start_date, end_date))
        sel = [r for r in rows if start_date <= r[0] <= end_date]
        sel.sort(key=lambda r: r[0], reverse=True)
        sel = sel[:800]
        return pd.DataFrame({"pub_time": [r[0] for r in sel], "title": [r[1] for r in sel],
                             "src": "x", "url": "u"})

    return FakePro(major_news=fake), state


def _rows(n, day="2026-11-05"):
    base = datetime.strptime(f"{day} 00:00:00", FMT)
    return [((base + timedelta(seconds=10 * i)).strftime(FMT), f"T{i}") for i in range(n)]


def test_major_news_pages_backwards_until_window_exhausted():
    rows = _rows(2000)
    pro, st = _news_pro(rows)
    src, _ = make(pro)
    df = src.major_news("2026-11-05 00:00:00", "2026-11-05 23:59:59")
    assert len(df) == 2000 and df.title.is_unique
    assert len(st["calls"]) == 3  # 800 + 800 + 400，没有按固定时间窗口空转
    assert list(df.columns) == ["pub_time", "src", "title", "url"]
    assert df.pub_time.is_monotonic_increasing


def test_major_news_single_call_when_under_cap():
    pro, st = _news_pro(_rows(100))
    src, _ = make(pro)
    df = src.major_news("2026-11-05 00:00:00", "2026-11-05 23:59:59")
    assert len(df) == 100 and len(st["calls"]) == 1


def test_major_news_call_budget_caps_calls_and_warns(caplog):
    pro, st = _news_pro(_rows(4000))
    src, _ = make(pro)
    with caplog.at_level(logging.WARNING, logger="sources"):
        df = src.major_news("2026-11-05 00:00:00", "2026-11-05 23:59:59", max_calls=2)
    assert len(st["calls"]) == 2 and len(df) == 1599  # 两页共享一条边界行，去重后少一条
    # 保留的是最新的行，且告警说明覆盖到哪里
    assert df.pub_time.max() == _rows(4000)[-1][0]
    assert any("仅覆盖" in r.getMessage() for r in caplog.records)


def test_major_news_same_second_page_still_progresses():
    rows = [("2026-11-05 10:00:00", f"S{i}") for i in range(800)] + [("2026-11-05 09:00:00", "early")]
    pro, st = _news_pro(rows)
    src, _ = make(pro)
    df = src.major_news("2026-11-05 00:00:00", "2026-11-05 23:59:59", max_calls=5)
    assert "early" in set(df.title) and len(st["calls"]) <= 3


def test_major_news_quota_error_returns_partial_without_retry(caplog):
    rows = _rows(2000)
    inner, st = _news_pro(rows)

    def fake(start_date, end_date, fields=None):
        if len(st["calls"]) >= 1:
            st["calls"].append("blocked")
            raise Exception("抱歉，您访问接口(major_news)频率超限(30次/小时)，具体频次详情：https://tushare.pro/document/1?doc_id=108。")
        return inner.apis["major_news"](start_date, end_date, fields)

    src, slept = make(FakePro(major_news=fake))
    with caplog.at_level(logging.WARNING, logger="sources"):
        df = src.major_news("2026-11-05 00:00:00", "2026-11-05 23:59:59")
    assert len(df) == 800 and slept == []  # 第二页被限额拦下，不重试
    assert len(st["calls"]) == 2
    assert any("配额" in r.getMessage() for r in caplog.records)


def test_major_news_quota_error_before_any_data_returns_empty():
    pro = FakePro(major_news=Exception("抱歉，您访问接口(major_news)频率超限(40次/天)，具体频次详情"))
    src, slept = make(pro)
    df = src.major_news("2026-11-05 00:00:00", "2026-11-05 23:59:59")
    assert df.empty and list(df.columns) == ["pub_time", "src", "title", "url"] and slept == []


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


# ---------------------------------------------------------------- 配额类频控不重试 / fx 残缺日线

@pytest.mark.parametrize("quota", ["40次/天", "30次/小时"])
def test_daily_or_hourly_quota_is_not_retried_for_any_api(quota):
    from sources import RateLimited
    pro = FakePro(daily=Exception(f"抱歉，您访问接口(daily)频率超限({quota})，具体频次详情"))
    src, slept = make(pro)
    with pytest.raises(RateLimited):
        src.call("daily")
    assert slept == [] and len(pro.calls) == 1


def _fx_frame(ticks, last_dates=None):
    dates = last_dates or [f"2026110{i}" for i in range(1, len(ticks) + 1)]
    return pd.DataFrame({"ts_code": "UKOIL", "trade_date": dates, "bid_close": [100.0 + i for i in range(len(ticks))],
                         "tick_qty": ticks})


def test_fx_drops_stub_bar_and_warns_when_latest(caplog):
    df = _fx_frame([380000, 390000, 400000, 3])
    src, _ = make(FakePro(fx_daily=df))
    with caplog.at_level(logging.WARNING, logger="sources"):
        out = src.fx("UKOIL", "20261101", "20261105")
    assert list(out.trade_date) == ["20261101", "20261102", "20261103"]
    assert list(out.columns) == ["trade_date", "close"]
    assert any("不完整" in r.getMessage() and "UKOIL" in r.getMessage() for r in caplog.records)


def test_fx_drops_old_stub_silently(caplog):
    df = _fx_frame([380000, 4, 400000, 390000])  # 中间一根（如周末）残缺，最新一根正常
    src, _ = make(FakePro(fx_daily=df))
    with caplog.at_level(logging.WARNING, logger="sources"):
        out = src.fx("UKOIL", "20261101", "20261105")
    assert list(out.trade_date) == ["20261101", "20261103", "20261104"]
    assert not caplog.records


def test_fx_without_tick_qty_keeps_all_rows():
    df = _fx_frame([1, 1, 1]).drop(columns="tick_qty")
    src, _ = make(FakePro(fx_daily=df))
    assert len(src.fx("UKOIL", "20261101", "20261105")) == 3
