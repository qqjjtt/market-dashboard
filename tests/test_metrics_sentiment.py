import numpy as np
import pandas as pd
import pytest

from metrics_sentiment import (
    concentration,
    concentration_wall,
    limit_flags,
    limit_threshold,
    match_sectors,
    six_dim,
    temperature_kpis,
    temperature_series,
)


def _day(rows):
    """rows: [(ts_code, pct_chg, amount亿)]"""
    return pd.DataFrame(rows, columns=["ts_code", "pct_chg", "amount"])


def test_limit_threshold():
    assert limit_threshold("300750.SZ", "宁德时代") == 20
    assert limit_threshold("301001.SZ", "某某") == 20
    assert limit_threshold("600001.SH", "*ST某某") == 5
    assert limit_threshold("300001.SZ", "ST某某") == 5  # ST 优先于 20%
    assert limit_threshold("688981.SH", "中芯国际") == 20
    assert limit_threshold("689009.SH", "九号公司") == 20
    assert limit_threshold("000001.SZ", "平安银行") == 10
    assert limit_threshold("000001.SZ", None) == 10


def test_limit_flags_excludes_bj_and_flags():
    d = _day([("000001.SZ", 9.9, 1), ("000002.SZ", -9.85, 1), ("300001.SZ", 10.0, 1),
              ("600001.SH", 4.8, 1), ("430001.BJ", 30.0, 1), ("000003.SZ", np.nan, 1)])
    names = {"600001.SH": "*ST某某"}
    out = limit_flags(d, names).set_index("ts_code")
    assert "430001.BJ" not in out.index
    assert out.loc["000001.SZ", "up_limit"] and not out.loc["000001.SZ", "down_limit"]
    assert out.loc["000002.SZ", "down_limit"] and not out.loc["000002.SZ", "up_limit"]
    assert not out.loc["300001.SZ", "up_limit"]  # 20% 板块 10% 不算涨停
    assert out.loc["600001.SH", "up_limit"]  # ST 4.8 >= 4.8
    assert not out.loc["000003.SZ", "up_limit"] and not out.loc["000003.SZ", "down_limit"]


def _hist(n=5, amount=100.0):
    """n 个历史交易日，每天总额 amount 亿；最后一天(第 n 天)涨停 A、B。"""
    days = {}
    for i in range(n):
        d = f"2026010{i + 1}"
        pcts = [10.0, 10.0, 0.0, 0.0] if i == n - 1 else [0.0, 0.0, 0.0, 0.0]
        days[d] = _day([(c, p, amount / 4) for c, p in zip(["A.SZ", "B.SZ", "C.SZ", "D.SZ"], pcts)])
    return days


def test_temperature_dims():
    days = _hist(5)
    # 第 6 天：4 只 pct [2, 2, 10, -1]，总额 110 亿；昨日涨停 A、B，A 今日涨、B 今日跌
    days["20260106"] = _day([("A.SZ", 2.0, 27.5), ("B.SZ", -1.0, 27.5), ("C.SZ", 10.0, 27.5), ("D.SZ", 2.0, 27.5)])
    s = temperature_series(days, {})
    assert list(s.columns) == ["up_ratio", "median", "limit", "volume", "profit", "score"]
    assert list(s.index) == ["20260106"]
    row = s.iloc[-1]
    assert row.up_ratio == 75 and row["median"] == 75 and row.limit == 100
    assert row.volume == pytest.approx(60) and row.profit == 50
    assert row.score == pytest.approx((75 + 75 + 100 + 60 + 50) / 5)


def test_limit_dim_both_zero_is_50():
    days = _hist(5)
    days["20260106"] = _day([("A.SZ", 1.0, 25), ("B.SZ", -1.0, 25), ("C.SZ", 0.5, 25), ("D.SZ", 0.0, 25)])
    row = temperature_series(days, {}).iloc[-1]
    assert row.limit == 50


def test_volume_needs_5_days():
    days = _hist(5)
    days["20260106"] = _day([("A.SZ", 1.0, 25), ("B.SZ", 1.0, 25), ("C.SZ", 0.5, 25), ("D.SZ", 0.0, 25)])
    s4 = temperature_series({k: days[k] for k in sorted(days)[:4]}, {})
    assert len(s4) == 0
    s5 = temperature_series({k: days[k] for k in sorted(days)[:5]}, {})
    assert len(s5) == 0  # 第 5 天只有 4 天历史
    s6 = temperature_series(days, {})
    assert list(s6.index) == ["20260106"]


def test_volume_uses_at_most_ma_days_and_clips():
    days = {}
    for i in range(30):
        days[f"d{i:02d}"] = _day([("A.SZ", 0.0, 100.0 if i < 10 else 50.0)])
    days["d30"] = _day([("A.SZ", 0.0, 50.0)])
    s = temperature_series(days, {}, ma=20)
    # d30 前 20 日(d10..d29)均额 50 -> 今日 50 -> 量能 50
    assert s.loc["d30", "volume"] == pytest.approx(50)
    # d20 前 20 日(d00..d19)均 75 -> 50/75-1 = -33.3 -> 16.67
    assert s.loc["d20", "volume"] == pytest.approx(50 + (50 / 75 - 1) * 100)
    days["d31"] = _day([("A.SZ", 0.0, 500.0)])
    assert temperature_series(days, {}, ma=20).loc["d31", "volume"] == 100  # clip


def test_profit_nan_when_no_yesterday_limit_and_score_skips_it():
    days = _hist(5)
    days["20260105"] = _day([("A.SZ", 0, 25), ("B.SZ", 0, 25), ("C.SZ", 0, 25), ("D.SZ", 0, 25)])
    days["20260106"] = _day([("A.SZ", 1.0, 25), ("B.SZ", 1.0, 25), ("C.SZ", 1.0, 25), ("D.SZ", 1.0, 25)])
    row = temperature_series(days, {}).iloc[-1]
    assert np.isnan(row.profit)
    assert row.score == pytest.approx(np.mean([row.up_ratio, row["median"], row.limit, row.volume]))


def test_bj_excluded_from_series():
    days = _hist(5)
    days["20260106"] = _day([("A.SZ", 2.0, 25), ("B.SZ", 2.0, 25), ("C.SZ", 2.0, 25), ("D.SZ", 2.0, 25),
                             ("430001.BJ", -9.0, 500)])
    row = temperature_series(days, {}).iloc[-1]
    assert row.up_ratio == 100 and row.volume == pytest.approx(50)


def _mf(rows):
    cols = ["ts_code", "buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
            "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount"]
    return pd.DataFrame(rows, columns=cols)


def test_concentration_is_amount_weighted():
    # A: sm+md 60 / 总 100；B: 10 / 总 300 -> 70/400 = 17.5（简单平均会是 31.67）
    mf = _mf([("A.SZ", 10, 20, 15, 15, 10, 10, 10, 10), ("B.SZ", 2, 3, 2, 3, 100, 100, 45, 45)])
    assert concentration(mf) == pytest.approx(17.5)
    assert concentration(mf, {"A.SZ"}) == pytest.approx(60)
    assert concentration(mf, set()) is None
    assert concentration(mf, {"ZZ.SZ"}) is None
    assert concentration(mf.iloc[0:0]) is None


def test_concentration_ignores_bj_and_nan_rows():
    mf = _mf([("A.SZ", 10, 20, 15, 15, 10, 10, 10, 10), ("X.BJ", 100, 0, 0, 0, 0, 0, 0, 0),
              ("B.SZ", np.nan, 3, 2, 3, 100, 100, 45, 45)])
    assert concentration(mf) == pytest.approx(60)


def test_match_sectors_strips_roman():
    basic = pd.DataFrame({"ts_code": ["600519.SH", "000001.SZ", "430001.BJ"],
                          "name": ["茅台", "平安银行", "北交"],
                          "industry": ["白酒", "银行", "软件服务"]})
    sw = pd.DataFrame({"ts_code": ["600519.SH", "000001.SZ", "430001.BJ"],
                       "l1_name": ["食品饮料", "银行", "计算机"],
                       "l2_name": ["白酒Ⅱ", "全国性股份制银行Ⅱ", "软件开发"],
                       "l3_name": ["白酒Ⅲ", "股份制银行Ⅲ", "垂直应用软件"]})
    m, label = match_sectors(basic, sw, {"600519.SH", "430001.BJ", "000001.SZ"})
    assert label == "申万行业"
    assert m["消费白酒·零售"] == {"600519.SH"}
    assert m["红利权重·银行"] == {"000001.SZ"}  # L1 "银行" 命中
    assert m["AI软件·游戏·传媒"] == set()  # BJ 剔除
    assert m["高位连板·情绪核心"] == {"600519.SH", "000001.SZ"}
    m2, label2 = match_sectors(basic, None, set())
    assert label2 == "stock_basic.industry"
    assert m2["消费白酒·零售"] == {"600519.SH"} and m2["红利权重·银行"] == {"000001.SZ"}
    assert m2["高位连板·情绪核心"] == set()


def test_wall_colors_and_new():
    codes = {f"S{i}.SZ" for i in range(6)}
    rows = []
    for i in range(6):
        # 第 i 只：sm+md = 10*(i+1)，大单 = 100 - 10*(i+1)
        rows.append((f"S{i}.SZ", 10 * (i + 1), 0, 0, 0, 100 - 10 * (i + 1), 0, 0, 0))
    mf = _mf(rows)
    st = {f"板块{i}": {f"S{i}.SZ"} for i in range(6)}
    wall = concentration_wall(mf, None, st, None)
    assert [r["name"] for r in wall] == [f"板块{i}" for i in (5, 4, 3, 2, 1, 0)]
    assert [r["color"] for r in wall] == ["red", "red", "orange", "orange", "blue", "blue"]
    assert all(r["yday"] is None and r["chg"] is None and r["n"] == 1 for r in wall)
    assert wall[0]["today"] == pytest.approx(60)
    assert set(wall[0]) >= {"icon", "name", "n", "today", "yday", "chg", "color"}
    wall2 = concentration_wall(mf, mf, st, {"板块5": {"S0.SZ"}})
    top = wall2[0]
    assert top["yday"] == pytest.approx(10) and top["chg"] == pytest.approx(50)
    assert wall2[1]["yday"] is None and wall2[1]["chg"] is None


def test_wall_none_today_last_gray():
    mf = _mf([("S0.SZ", 10, 0, 0, 0, 90, 0, 0, 0), ("S1.SZ", 40, 0, 0, 0, 60, 0, 0, 0)])
    st = {"空": set(), "低": {"S0.SZ"}, "高": {"S1.SZ"}}
    wall = concentration_wall(mf, mf, st, st)
    assert [r["name"] for r in wall] == ["高", "低", "空"]
    assert wall[2]["color"] == "gray" and wall[2]["today"] is None and wall[2]["chg"] is None
    assert wall[2]["n"] == 0
    assert wall[0]["color"] == "red" and wall[1]["color"] != "gray"


def test_six_dim():
    assert six_dim(71.2, 65.2) == pytest.approx(70.2)
    assert six_dim(None, 65.0) is None and six_dim(60.0, None) is None


def test_temperature_kpis():
    idx = [f"d{i:02d}" for i in range(40)]
    scores = [50.0] * 39 + [70.0]
    s = pd.DataFrame({"score": scores}, index=idx)
    k = temperature_kpis(s, window=30)
    assert k["today"] == 70 and k["yday"] == 50 and k["delta"] == 20 and k["delta_text"] == "明显升温"
    assert k["n"] == 30 and k["rank"] == 1 and k["max"] == 70 and k["max_date"] == "d39"
    assert k["mean"] == pytest.approx((50 * 29 + 70) / 30)
    assert k["dev"] == pytest.approx(70 - k["mean"]) and k["dev_text"] == "明显偏热"
    assert k["pct_above"] == pytest.approx(29 / 30 * 100)


@pytest.mark.parametrize("delta,text", [(1.9, "几乎没动"), (-1.9, "几乎没动"), (2, "明显升温"), (-2, "明显降温")])
def test_kpi_delta_text(delta, text):
    s = pd.DataFrame({"score": [50.0, 50.0 + delta]}, index=["a", "b"])
    assert temperature_kpis(s)["delta_text"] == text


@pytest.mark.parametrize("dev,text", [(20, "明显偏热"), (10, "偏热"), (0, "接近均值"), (-10, "偏冷"), (-20, "明显偏冷")])
def test_kpi_dev_text(dev, text):
    # 构造使 today - mean == dev：先 n-1 天为 base，今日 base+x，mean=base+x/n → dev = x(1-1/n)
    n = 10
    x = dev * n / (n - 1)
    s = pd.DataFrame({"score": [50.0] * (n - 1) + [50.0 + x]}, index=[f"d{i}" for i in range(n)])
    assert temperature_kpis(s)["dev_text"] == text


def test_kpi_short_series():
    s = pd.DataFrame({"score": [55.0]}, index=["a"])
    k = temperature_kpis(s)
    assert k["today"] == 55 and k["yday"] is None and k["delta"] is None and k["delta_text"] is None
    assert temperature_kpis(s.iloc[0:0]) == {}


def test_match_sectors_ignores_l1():
    basic = pd.DataFrame({"ts_code": ["PV.SZ", "UT.SH"], "name": ["光伏", "电力"], "industry": ["电气设备", "火力发电"]})
    sw = pd.DataFrame({"ts_code": ["PV.SZ", "UT.SH"],
                       "l1_name": ["电力设备", "公用事业"],
                       "l2_name": ["光伏设备", "电力"],
                       "l3_name": ["光伏电池组件", "火力发电"]})
    m, label = match_sectors(basic, sw, set())
    assert label == "申万行业"
    assert "PV.SZ" in m["光伏设备·组件"] and "PV.SZ" not in m["红利权重·银行"]
    assert "UT.SH" in m["红利权重·银行"]


def test_match_sectors_exact_name_not_substring():
    """真实数据：「白酒」曾子串命中 L2「非白酒」，「电池」命中「光伏电池组件」「电池化学品」。"""
    basic = pd.DataFrame({"ts_code": ["MT.SH", "BEER.SH", "PVM.SZ", "LI.SZ", "CHEM.SZ", "BK.SH", "CCB.SH"],
                          "name": ["茅台", "啤酒", "光伏组件", "锂电", "电池化学品", "城商行", "招行"],
                          "industry": ["", "", "", "", "", "", ""]})
    sw = pd.DataFrame({"ts_code": basic["ts_code"],
                       "l1_name": ["食品饮料", "食品饮料", "电力设备", "电力设备", "基础化工", "银行", "银行"],
                       "l2_name": ["白酒Ⅱ", "非白酒", "光伏设备", "电池", "电子化学品Ⅱ", "城商行Ⅱ", "股份制银行Ⅱ"],
                       "l3_name": ["白酒Ⅲ", "啤酒", "光伏电池组件", "锂电池", "电池化学品", "城商行Ⅲ", "股份制银行Ⅲ"]})
    m, _ = match_sectors(basic, sw, set())
    assert m["消费白酒·零售"] == {"MT.SH"}                      # 非白酒 / 啤酒 不再并入
    assert m["锂电·新能源"] == {"LI.SZ"}                         # 光伏电池组件、电池化学品 不并入
    assert m["红利权重·银行"] == {"BK.SH", "CCB.SH"}              # 城商行已在板块定义里


def test_config_sw_keywords_all_exist_as_real_l2_l3_names():
    """每个静态板块的 sw 关键词都必须是申万 L2/L3 的真实名称（电信服务、银行曾为 0 命中）。"""
    import config
    kws = {k for s in config.SECTORS if not s["dynamic"] for k in s["sw"]}
    assert "电信服务" not in kws and "银行" not in kws
    assert {"电信运营商", "国有大型银行", "股份制银行", "城商行", "农商行"} <= kws
