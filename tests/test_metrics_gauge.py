import math

import numpy as np
import pandas as pd
import pytest

import config
import metrics_gauge as mg
import metrics_liquidity as ml

CFG = config.GAUGE
SPECS = config.GAUGE_COMPONENTS


def _days(n, start="2025-01-01"):
    return [d.strftime("%Y%m%d") for d in pd.bdate_range(start, periods=n)]


def _ser(vals, days=None):
    days = days or _days(len(vals))
    return pd.Series(vals, index=days, dtype=float)


def _spec(key):
    return next(s for s in SPECS if s["key"] == key)


def test_change_n_kinds():
    x = _ser([4.00, 4.02, 4.05, 4.10, 4.15, 4.18])
    assert mg.change_n(x, "pct_level", 5).iloc[-1] == pytest.approx(18.0)
    assert math.isnan(mg.change_n(x, "pct_level", 5).iloc[0])
    b = _ser([1, 2, 3, 4, 5, 9])
    assert mg.change_n(b, "bp_level", 5).iloc[-1] == pytest.approx(8.0)
    p = _ser([100, 101, 102, 103, 104, 110])
    assert mg.change_n(p, "price", 5).iloc[-1] == pytest.approx(math.log(110 / 100))


def test_z_floor_and_min_obs():
    chg = _ser([1.0] * 130)
    z = mg.z_series(chg, 250, 120, 2.0)
    assert math.isnan(z.iloc[119])          # 之前只有 119 个样本
    assert z.iloc[120] == pytest.approx(0.5)  # 常数 -> 标准差 0 -> 用下限 2
    z2 = mg.z_series(_ser([1.0] * 130)[:120], 250, 120, 2.0)
    assert z2.isna().all()
    # 之前不含自身：突变当天用的是突变前的标准差
    v = [0.0, 1.0] * 70 + [50.0]
    z3 = mg.z_series(_ser(v), 250, 120, 0.0001)
    s = pd.Series(v[:-1]).std(ddof=1)
    assert z3.iloc[-1] == pytest.approx(50.0 / s)


def test_score_clip_and_dir():
    assert mg.score(3, -1) == 0
    assert mg.score(-6, -1) == 100
    assert mg.score(0.6, +1) == pytest.approx(60)
    assert mg.score(1, 0) is None
    assert mg.score(None, 1) is None
    assert mg.score(float("nan"), 1) is None


def test_zone_edges():
    assert mg.zone(25) == "偏紧"
    assert mg.zone(24.99) == "极度紧张"
    assert mg.zone(100) == "极度宽松"
    assert mg.zone(0) == "极度紧张"
    assert mg.zone(55) == "偏松"
    assert mg.zone(None) is None


def test_break_mask_positions():
    days = _days(20)
    chg = _ser(list(range(20)), days)
    out = mg.break_mask(chg, [days[10]], 5)
    assert out.iloc[10:15].isna().all()
    assert out.iloc[:10].notna().all() and out.iloc[15:].notna().all()
    # break 日不在索引里 -> 第一个 >= b 的索引日
    gap = [d for d in days if d != days[10]]
    chg2 = _ser(list(range(19)), gap)
    out3 = mg.break_mask(chg2, [days[10]], 5)
    assert out3.iloc[10:15].isna().all()


def _frames_for_step(with_breaks):
    days = _days(320)
    step = days[200]
    dr001 = _ser([1.30] * 320, days)
    sp = ml.policy_spread_bp(dr001, [("20240101", 1.40, "7天逆回购"), (step, 1.25, "隔夜逆回购")])
    brk = [step] if with_breaks else []
    frames = {
        "dr001_spread": mg.component_frame(sp, _spec("dr001_spread"), brk, CFG),
        "r007_dr007": mg.component_frame(_ser([5.0] * 320, days), _spec("r007_dr007"), [], CFG),
        "bond_fut": mg.component_frame(_ser([100.0] * 320, days), _spec("bond_fut"), [], CFG),
    }
    return days, frames


def test_breaks_suppress_step():
    days, frames = _frames_for_step(True)
    specs = [s for s in SPECS if s["group"] == "domestic"]
    pre = mg.index_on(days[199], frames, specs, CFG)
    mid = mg.index_on(days[202], frames, specs, CFG)   # 台阶窗口内
    post = mg.index_on(days[210], frames, specs, CFG)  # 窗口之后
    assert pre["domestic"] == pytest.approx(mid["domestic"], abs=0.5)
    assert pre["domestic"] == pytest.approx(post["domestic"], abs=0.5)
    # 不剔除时会被当成大幅收紧
    d2, f2 = _frames_for_step(False)
    bad = mg.index_on(d2[202], f2, specs, CFG)
    assert bad["domestic"] < pre["domestic"] - 5


def test_stale_component_dropped():
    days = _days(300)
    spec = _spec("r007_dr007")
    full = mg.component_frame(_ser(list(np.sin(np.arange(300)) * 3), days), spec, [], CFG)
    stale = full.iloc[:292]  # 最后一行早于 days[299] 多于 7 天
    fr = {"r007_dr007": stale}
    out = mg.index_on(days[299], fr, [spec], CFG)
    assert "r007_dr007" not in out["items"]
    assert out["missing"]["domestic"] == 1
    out2 = mg.index_on(stale.index[-1], fr, [spec], CFG)
    assert "r007_dr007" in out2["items"]


def _const_frames(specs, day_count=200):
    days = _days(day_count)
    fr = {}
    for s in specs:
        x = _ser([5.0 if s["kind"] != "price" else 100.0] * day_count, days)
        fr[s["key"]] = mg.component_frame(x, s, [], CFG)
    return days, fr


def test_index_groups():
    dom = [s for s in SPECS if s["group"] == "domestic"]
    ext = [s for s in SPECS if s["group"] == "external"]
    days, fr = _const_frames(dom + ext[:2])
    out = mg.index_on(days[-1], fr, dom + ext, CFG)
    assert out["external"] is None
    assert out["domestic"] == pytest.approx(50)
    assert out["value"] == out["domestic"]
    assert out["missing"] == {"domestic": 0, "external": 4}
    # 两组都只有 1 项 -> 空
    days, fr = _const_frames([dom[0], ext[0]])
    out = mg.index_on(days[-1], fr, dom + ext, CFG)
    assert out["value"] is None and out["domestic"] is None and out["external"] is None


def test_index_both_groups_half_half():
    dom = [s for s in SPECS if s["group"] == "domestic"]
    ext = [s for s in SPECS if s["group"] == "external"]
    days, fr = _const_frames(dom + ext)
    out = mg.index_on(days[-1], fr, dom + ext, CFG)
    assert out["value"] == pytest.approx(50)
    assert set(out["items"]) == {s["key"] for s in dom + ext}


def test_index_missing_items_note():
    dom = [s for s in SPECS if s["group"] == "domestic"]
    ext = [s for s in SPECS if s["group"] == "external"]
    days, fr = _const_frames(dom + ext[:4])
    out = mg.index_on(days[-1], fr, dom + ext, CFG)
    assert out["missing"]["external"] == 2
    text, reason = mg.verdict(out["value"], [], out["items"], dom + ext, out["missing"])
    assert reason.endswith("（外部缺 2 项）")
    assert text == f"{mg.zone(out['value'])}。{reason}"


def test_verdict_insufficient_group_and_none():
    dom = [s for s in SPECS if s["group"] == "domestic"]
    ext = [s for s in SPECS if s["group"] == "external"]
    days, fr = _const_frames(dom)
    out = mg.index_on(days[-1], fr, dom + ext, CFG)
    _, reason = mg.verdict(out["value"], [], out["items"], dom + ext, out["missing"])
    assert reason.endswith("（外部数据不足）")
    assert mg.verdict(None, [], {}, SPECS, {"domestic": 0, "external": 0}) == ("数据不足", "数据不足")


def test_combos_threshold():
    specs = config.GAUGE_COMBOS
    out = mg.combos({"us10y": 1.2, "usd": 1.0, "cnh": 0.5}, specs, CFG)
    ext = next(c for c in out if c["key"] == "external_tight")
    assert ext["met"] == 2 and ext["on"] is True and ext["of"] == 3
    assert [c["ok"] for c in ext["conds"]] == [True, True, False]
    assert ext["conds"][0]["name"] == "美债收益率上行"
    out = mg.combos({"us10y": 1.2, "usd": 0.2, "cnh": None}, specs, CFG)
    ext = next(c for c in out if c["key"] == "external_tight")
    assert ext["met"] == 1 and ext["on"] is False
    # 方向 -1：国债期货下跌
    out = mg.combos({"bond_fut": -1.5, "dr001_spread": 1.1}, specs, CFG)
    dom = next(c for c in out if c["key"] == "domestic_tight")
    assert dom["met"] == 2 and dom["on"] is True
    assert len(out) == 3


def test_verdict_reason_priority():
    cs = mg.combos({"us10y": 1.5, "usd": 1.2, "cnh": 1.1}, config.GAUGE_COMBOS, CFG)
    items = {s["key"]: {"score": 30} for s in SPECS if s["dir"] != 0}
    text, reason = mg.verdict(35.0, cs, items, SPECS, {"domestic": 0, "external": 0})
    assert reason.startswith("外部在收紧：")
    for n in ("美债收益率上行", "美元走强", "人民币走弱"):
        assert n in reason
    assert text == "偏紧。" + reason
    calm = {s["key"]: {"score": 45} for s in SPECS if s["dir"] != 0}
    cs0 = mg.combos({}, config.GAUGE_COMBOS, CFG)
    text, reason = mg.verdict(48.0, cs0, calm, SPECS, {"domestic": 0, "external": 0})
    assert reason == "各项都在常态范围内"
    # 无组合触发但有偏离 -> 点名
    dev = dict(calm)
    dev["r007_dr007"] = {"score": 15}
    _, reason = mg.verdict(40.0, cs0, dev, SPECS, {"domestic": 0, "external": 0})
    assert reason.startswith("主要来自") and "R007−DR007" in reason


def test_ago_partial():
    days = _days(20)
    h = _ser([40.0 + i for i in range(20)], days)
    a = mg.ago(h, days[-1])
    assert a["y1"] is None and a["m1"] is None
    assert a["prev"] == 58
    assert a["w1"] is not None and a["w1"] == 54  # 7 天前 = 1 月 21 日（周二）当日值
    # 值取整、NaN 被跳过
    h2 = h.copy()
    h2.iloc[-2] = float("nan")
    assert mg.ago(h2, days[-1])["prev"] == 57
    assert isinstance(a["prev"], int)


def test_ago_full_year():
    days = _days(300, "2025-01-01")
    h = _ser([float(i % 90) + 0.4 for i in range(300)], days)
    a = mg.ago(h, days[-1])
    assert all(a[k] is not None for k in ("prev", "w1", "m1", "y1"))


def test_index_history_nan_kept():
    dom = [s for s in SPECS if s["group"] == "domestic"]
    days, fr = _const_frames(dom)
    ext = [s for s in SPECS if s["group"] == "external"]
    h = mg.index_history(days[-3:] + ["20300101"], fr, dom + ext, CFG)
    assert h.iloc[:3].tolist() == [pytest.approx(50)] * 3
    assert math.isnan(h.iloc[3])


def test_gate_rules():
    ok = _ser([5.0, 4.8, 4.6, 4.4], ["202601", "202602", "202603", "202604"])
    flat = _ser([5.0, 5.1, 5.0, 5.2], ["202601", "202602", "202603", "202604"])
    empty = pd.Series(dtype=float)
    g = mg.gate(ok, flat, empty)
    assert g["ok"] is False and g["notes"] == ["背景：M1 同比连续 3 个月回落"]
    g = mg.gate(flat, ok, empty)
    assert g["notes"] == ["背景：社融增速连续 3 个月放缓"]
    fed = _ser([100.0, 100.0, 99.0, 98.0, 96.6], _days(5))
    g = mg.gate(flat, flat, fed)
    assert g["ok"] is False and g["notes"] == ["背景：美元净流动性 4 周收缩 3.4%"]
    g = mg.gate(empty, empty, empty)
    assert g == {"ok": True, "notes": []}
    fed2 = _ser([100.0, 100.0, 99.0, 98.0, 97.5], _days(5))  # 只降 2.5%
    assert mg.gate(flat, flat, fed2)["ok"] is True
