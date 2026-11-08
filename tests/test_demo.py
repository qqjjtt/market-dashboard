"""DemoSource / DemoFred：接口与 TushareSource 完全一致，数据形状与真实源一致。"""
import inspect
import math

import pandas as pd

import config
from demo import DemoFred, DemoSource
from fred import FredSource
from sources import TushareSource


def public_methods(cls):
    return [(n, f) for n, f in inspect.getmembers(cls, inspect.isfunction)
            if not n.startswith("_") and n not in ("call", "from_env")]


def test_demo_matches_tushare_interface():
    pubs = public_methods(TushareSource)
    assert len(pubs) >= 20
    for name, fn in pubs:
        assert hasattr(DemoSource, name), name
        assert inspect.signature(getattr(DemoSource, name)) == inspect.signature(fn), name
    assert inspect.signature(DemoFred.series) == inspect.signature(FredSource.series)


def test_trade_days_weekdays_ascending():
    d = DemoSource()
    days = d.trade_days("20261106", 10)
    assert len(days) == 10 and days == sorted(days) and days[-1] == "20261106"
    assert all(pd.Timestamp(x).weekday() < 5 for x in days)
    # 周六结束 -> 最后一个交易日是周五
    assert d.trade_days("20261107", 3)[-1] == "20261106"


def test_deterministic_per_seed():
    a = DemoSource(seed=3).stock_daily("20261106")
    b = DemoSource(seed=3).stock_daily("20261106")
    c = DemoSource(seed=4).stock_daily("20261106")
    pd.testing.assert_frame_equal(a, b)
    assert not a["pct_chg"].equals(c["pct_chg"])


def test_stock_tables_shape():
    d = DemoSource()
    sb = d.stock_basic()
    assert list(sb.columns) == ["ts_code", "name", "industry"] and 250 <= len(sb) <= 400
    assert sb["ts_code"].is_unique
    suffix = sb["ts_code"].str[-3:]
    assert set(suffix) == {".SH", ".SZ"}
    prefixes = {c[:3] for c in sb["ts_code"]}
    assert {"600", "000", "300", "688"} <= prefixes
    assert sb["name"].str.contains("ST").any()
    sd = d.stock_daily("20261106")
    assert list(sd.columns) == ["ts_code", "pct_chg", "amount"]
    assert set(sd["ts_code"]) == set(sb["ts_code"])
    assert sd["amount"].between(0.001, 200).all()  # 亿元
    assert sd["pct_chg"].abs().max() < 21
    mf = d.moneyflow("20261106")
    cols = ["ts_code"] + [f"{x}_{lv}_amount" for lv in ("sm", "md", "lg", "elg") for x in ("buy", "sell")]
    assert list(mf.columns) == cols and (mf[cols[1:]] >= 0).all().all()


def test_sector_matching_finds_members():
    d = DemoSource()
    sb = d.stock_basic()
    sw = d.sw_members()
    assert list(sw.columns) == ["ts_code", "l1_name", "l2_name", "l3_name"]
    names = pd.concat([sw["l1_name"], sw["l2_name"], sw["l3_name"]])
    for sec in config.SECTORS:
        if sec["dynamic"]:
            continue
        assert any(names.str.contains(k, regex=False).any() for k in sec["sw"]), sec["name"]
        assert sb["industry"].isin(sec["basic"]).any(), sec["name"]


def test_etf_methods():
    d = DemoSource()
    days = d.trade_days("20261106", 40)
    code = "510300.SH"
    sh = d.etf_share(code, days[0], days[-1])
    assert list(sh.columns) == ["trade_date", "share"] and len(sh) == len(days)
    assert sh["trade_date"].tolist() == days and sh["share"].notna().all()
    assert sh["share"].between(0.1, 5000).all()  # 亿份
    # 深市最新一日留空
    sz = d.etf_share("159919.SZ", days[0], days[-1])
    assert sz["trade_date"].tolist() == days[:-1] and list(sz.columns) == ["trade_date", "share"]
    ed = d.etf_daily(code, days[0], days[-1])
    assert list(ed.columns) == ["trade_date", "close", "amount"] and len(ed) == len(days)
    assert (ed["close"] > 0).all()
    names = d.etf_names(["510300.SH", "159919.SZ"])
    assert set(names) == {"510300.SH", "159919.SZ"} and all(isinstance(v, str) for v in names.values())
    idx = d.index_daily("000300.SH", days[0], days[-1])
    assert list(idx.columns) == ["trade_date", "close", "pct_chg"] and len(idx) == len(days)


def test_etf_minutes():
    d = DemoSource()
    m = d.etf_minutes("510300.SH", "20261105", "20261106")
    assert list(m.columns) == ["trade_time", "amount"]
    assert m["trade_time"].is_monotonic_increasing
    assert m["trade_time"].iloc[0].startswith("2026-11-05 09:3")
    assert m["trade_time"].iloc[-1].startswith("2026-11-06 15:00")
    assert m["amount"].notna().all() and (m["amount"] > 0).all()
    assert m["trade_time"].str[11:16].between("09:30", "15:00").all()


def test_flow_and_sentiment():
    d = DemoSource()
    days = d.trade_days("20261106", 30)
    sf = d.sector_flow(days[0], days[-1])
    assert list(sf.columns) == ["trade_date", "industry", "net_amount", "pct_change"]
    assert sf["trade_date"].is_monotonic_increasing and len(sf) > 0
    mfl = d.market_flow(days[0], days[-1])
    assert list(mfl.columns) == ["trade_date", "net_amount"] and len(mfl) == len(days)
    mg = d.margin(days[0], days[-1])
    assert list(mg.columns) == ["trade_date", "balance"] and len(mg) == len(days)
    assert mg["balance"].between(10000, 40000).all()  # 亿元，约 1.8 万亿
    lc = d.limit_counts("20261106")
    assert set(lc) == {"up", "down"} and isinstance(lc["up"], int) and lc["up"] >= 0
    v = d.inst_net_buy("20261106")
    assert isinstance(v, float) and abs(v) < 50


def test_macro_and_cross_market():
    d = DemoSource()
    days = d.trade_days("20261106", 30)
    sh = d.shibor(days[0], days[-1])
    assert list(sh.columns) == ["date", "on", "w1", "y1"] and len(sh) == len(days)
    m2 = d.m2("202501", "202610")
    assert list(m2.columns) == ["month", "m1_yoy", "m2_yoy"] and m2["month"].is_monotonic_increasing
    assert m2["month"].iloc[-1] == "202610"
    for code, base in (("DR001.IB", 1.30), ("DR007.IB", 1.35), ("R007.IB", 1.39)):
        rp = d.repo(code, days[0], days[-1])
        assert list(rp.columns) == ["trade_date", "rate"] and len(rp) == len(days)
        assert abs(rp["rate"].mean() - base) < 0.5
    assert d.repo("XXX", days[0], days[-1]).empty
    sf = d.social_financing("202409", "202610")
    assert list(sf.columns) == ["month", "stk_endval"] and len(sf) == 26 and sf["stk_endval"].iloc[-1] > sf["stk_endval"].iloc[0]
    bf = d.bond_fut_10y(days[0], days[-1])
    assert list(bf.columns) == ["trade_date", "close"] and len(bf) == len(days)
    for code in ("UKOIL", "USDOLLAR", "FED30D", "USDCNH.FXCM"):
        fx = d.fx(code, days[0], days[-1])
        assert list(fx.columns) == ["trade_date", "close"] and len(fx) == len(days)
    gi = d.global_index(days[0], days[-1])
    assert list(gi.columns) == ["ts_code", "trade_date", "close", "pct_chg"]
    assert set(gi["ts_code"]) >= {c for c, _ in config.US_INDICES}
    uy = d.us_yields(days[0], days[-1])
    assert list(uy.columns) == ["date", "y2", "y10"] and len(uy) == len(days)
    assert uy["y10"].between(2, 7).all()


def test_major_news():
    d = DemoSource()
    n = d.major_news("2026-10-30 00:00:00", "2026-11-06 23:59:59")
    assert list(n.columns) == ["pub_time", "src", "title", "url"]
    assert n["pub_time"].is_monotonic_increasing and n["title"].is_unique
    assert n["url"].str.startswith("http").all()
    kws = config.NEWS_KW_DOMESTIC + config.NEWS_KW_EXTERNAL
    hit = n["title"].map(lambda t: any(k in t for k in kws))
    assert hit.sum() >= 3 and (~hit).sum() >= 3
    assert n["pub_time"].between("2026-10-30 00:00:00", "2026-11-06 23:59:59").all()


def test_demo_fred():
    f = DemoFred()
    expect = {"WALCL": (5e6, 8e6), "WTREGEN": (3e5, 1.5e6), "RRPONTSYD": (0, 6),
              "WRESBAL": (2e6, 4e6), "DFF": (3, 5), "SOFR": (3, 5.5), "IORB": (3, 5.5),
              "BAMLH0A0HYM2": (1.5, 7), "NFCI": (-1.5, 1.0), "DFII10": (0, 4)}
    assert set(expect) == set(config.FRED_SERIES)
    for sid, (lo, hi) in expect.items():
        s = f.series(sid, "2026-09-01")
        assert isinstance(s.index, pd.DatetimeIndex) and s.index.is_monotonic_increasing
        assert s.index[0] >= pd.Timestamp("2026-09-01") and len(s) >= 4
        assert s.between(lo, hi).all(), sid
    w = f.series("WALCL", "2026-09-01")
    assert all(ts.weekday() == 2 for ts in w.index)  # 周三
    assert f.series("NOPE", "2026-09-01") is None


def test_demo_fut_mapping_changes_quarterly():
    d = DemoSource()
    fm = d.fut_mapping("T.CFX", "20240101", "20261106")
    assert list(fm.columns) == ["trade_date", "mapping_ts_code"] and fm["trade_date"].is_monotonic_increasing
    codes = list(fm["mapping_ts_code"])
    changes = [i for i in range(1, len(codes)) if codes[i] != codes[i - 1]]
    assert len(changes) >= 8 and all(c.startswith("T") and c.endswith(".CFX") for c in codes)
    assert fm["trade_date"].iloc[changes[0]][4:6] in ("03", "06", "09", "12")
    sc = d.fut_mapping("SC.INE", "20260101", "20261106")
    assert sc["mapping_ts_code"].iloc[0].startswith("SC") and sc["mapping_ts_code"].iloc[0].endswith(".INE")


def test_demo_trade_days_between():
    d = DemoSource()
    days = d.trade_days_between("20261116", "20261127")
    assert days[0] == "20261116" and days[-1] == "20261127" and len(days) == 10


def test_demo_hot_sources():
    d = DemoSource()
    cl = d.sw_classify()
    assert list(cl.columns) == ["index_code", "industry_name", "level", "parent_code"]
    fixed = {n for s in config.SECTORS if not s["dynamic"] for n in s["sw"]}
    names = cl["industry_name"].str.replace("[ⅡⅢ]", "", regex=True)
    extra = cl[cl["level"].isin(["L2", "L3"]) & ~names.isin(fixed)]
    assert len(extra) >= 6
    sw = d.sw_members()
    mem = set(sw["l2_name"].str.replace("Ⅱ", "")) | set(sw["l3_name"].str.replace("Ⅲ", ""))
    assert sum(n in mem for n in names[extra.index]) >= 6  # 额外行业有成分股
    r = d.sw_daily_range("20261001", "20261106")
    assert list(r.columns) == ["ts_code", "trade_date", "name", "pct_change", "amount", "float_mv"]
    assert set(cl["index_code"]) == set(r["ts_code"])
    w, m = d.winner_rate("20261106"), d.circ_mv("20261106")
    assert w["winner_rate"].between(20, 90).all() and (m["circ_mv"] > 0).all() and len(w) == len(m)
    assert d.winner_rate("20261107").empty and d.circ_mv("20261107").empty  # 周六


def test_value_chips_methods():
    d = DemoSource()
    pb = d.index_pb("000300.SH", "20240101", "20261106")
    assert list(pb.columns) == ["trade_date", "pb"] and pb["pb"].between(0.3, 8).all() and len(pb) > 500
    st = d.market_stats("20260101", "20261106")
    assert list(st.columns) == ["trade_date", "float_mv", "amount"] and (st["float_mv"] > 0).all()
    md = d.margin_detail("20260101", "20261106")
    assert list(md.columns) == ["trade_date", "rzye", "rzmre"] and len(md) == len(st)
    assert d.index_pb("000300.SH", "20261107", "20261108").empty


def test_demo_eco_cal_has_whitelist_and_noise():
    import econ_calendar
    df = DemoSource().eco_cal("20261101", "20261130", "中国")
    assert list(df.columns) == ["date", "time", "country", "event", "value", "pre_value", "fore_value"]
    assert set(df["country"]) == {"中国"}
    hits = [e for e in df["event"] if econ_calendar.match_item(e)]
    assert any("CPI" in e for e in hits) and any("PPI" in e for e in hits) and any("M2" in e for e in hits)
    assert "中国外汇储备(美元)(十月)" in set(df["event"])
    assert "02:38" in set(df["time"])
    assert list(df.sort_values(["date", "time"], kind="stable").index) == list(df.index)
    cpi = df[df["event"].str.contains("CPI年率")].iloc[0]
    assert cpi["date"] == "20261109" and pd.isna(cpi["value"])


def test_demo_eco_cal_respects_row_limit():
    d = DemoSource()
    for a, b in [("20240101", "20261231"), ("20260101", "20261231")]:
        assert len(d.eco_cal(a, b, "中国")) <= 100 and len(d.eco_cal(a, b, "美国")) <= 100
    assert len(d.eco_cal("20261102", "20261108", "美国")) >= 1
    assert d.eco_cal("20261101", "20261130", "日本").empty
    assert list(d.eco_cal("20261101", "20261130", "日本").columns) == \
        ["date", "time", "country", "event", "value", "pre_value", "fore_value"]
    past = d.eco_cal("20261001", "20261031", "美国")
    assert past["value"].notna().all()


def test_demo_cn_cpi_ppi():
    d = DemoSource()
    assert list(d.cn_cpi("202601", "202603").columns) == ["month", "nt_yoy"] and len(d.cn_cpi("202601", "202603")) == 3
    assert list(d.cn_ppi("202601", "202603").columns) == ["month", "ppi_yoy"]


def test_demo_fred_macro_series_monthly():
    for sid in config.FRED_MACRO_SERIES:
        s = DemoFred().series(sid, "2024-01-01")
        assert s is not None and len(s) > 12 and all(d.day == 1 for d in s.index)


def test_demo_release_dates():
    d = DemoFred()
    for rid in config.FRED_RELEASES.values():
        r = d.release_dates(rid, "2026-11-08")
        assert r and r == sorted(r) and all(x >= "20261108" and len(x) == 8 for x in r)
    assert d.release_dates(999, "2026-11-08") is None
