from datetime import datetime
from zoneinfo import ZoneInfo

from econ_calendar import (clean_time, dedupe, format_next, is_released, match_item,
                           merge, next_release, normalize, period_of)

SH = ZoneInfo("Asia/Shanghai")


def ev(item="cn_cpi_yoy", date="20261109", time=None, region="cn", label=None, period=None,
       major=True, value=None, pre=None, fore=None):
    return {"date": date, "time": time, "region": region, "item": item, "label": label or item,
            "period": period, "major": major, "value": value, "pre": pre, "fore": fore}


def test_match_ignores_country_and_drops_noise():
    assert match_item("英国贸易帐(英镑)(十月)") is None
    assert match_item("美国当周EIA原油库存变动(桶)") is None
    assert match_item("美国初请失业金人数四周均值") is None
    assert match_item("美国U6失业率(%)(九月)") is None
    assert match_item("中国CPI年率(%)(年度)(十月)")["key"] == "cn_cpi_yoy"
    assert match_item("美国季调后非农就业人口变动(九月)")["key"] == "us_nfp"
    assert match_item("美国未季调核心CPI年率(%)(年度)(八月)")["key"] == "us_core_cpi_yoy"


def test_clean_time_placeholders_only_for_data():
    assert clean_time("02:38", "data") is None
    assert clean_time("23:17", "data") is None
    assert clean_time("09:30", "data") == "09:30"
    assert clean_time("09:45", "data") == "09:45"
    assert clean_time("21:05", "event") == "21:05"
    assert clean_time(None, "data") is None and clean_time("", "event") is None


def test_period_of():
    assert period_of("中国M2货币供应年率(%)(年度)(九月)") == "九月"
    assert period_of("中国季度GDP年率(%)(年度)(第三季度)") == "第三季度"
    assert period_of("中国央行贷款市场报价利率(LPR)") is None
    assert period_of("美联储利率决议") is None


def test_normalize_builds_events_and_drops_noise():
    rows = [
        {"date": "20261014", "time": "09:30", "country": "美国", "event": "中国M2货币供应年率(%)(年度)(九月)",
         "value": "7.2%", "pre_value": "8.0%", "fore_value": ""},
        {"date": "20261014", "time": "10:00", "country": "中国", "event": "英国贸易帐(英镑)(十月)",
         "value": "1", "pre_value": "", "fore_value": ""},
        {"date": "20261014", "time": "02:38", "country": "中国", "event": "中国CPI年率(%)(年度)(九月)",
         "value": "nan", "pre_value": None, "fore_value": "0.8%"},
    ]
    out = normalize(rows)
    assert len(out) == 2
    m2, cpi = out
    assert m2 == {"date": "20261014", "time": "09:30", "region": "cn", "item": "cn_m2",
                  "label": "M2 年率 · 九月", "period": "九月", "major": True,
                  "value": "7.2%", "pre": "8.0%", "fore": None}
    assert cpi["time"] is None and cpi["value"] is None and cpi["fore"] == "0.8%"


def test_dedupe_reschedule_prefers_value():
    a = ev(item="cn_m2", date="20261014", period="九月", value="7.2%")
    b = ev(item="cn_m2", date="20261015", period="九月", value=None)
    for inp in ([a, b], [b, a]):  # 无值的旧日期行在前/在后都一样
        out = dedupe(inp)
        assert [(e["date"], e["value"]) for e in out if e["item"] == "cn_m2"] == [("20261014", "7.2%")]


def test_dedupe_reschedule_no_value_keeps_latest_date():
    a = ev(item="cn_m2", date="20261014", period="九月")
    b = ev(item="cn_m2", date="20261020", period="九月")
    assert [e["date"] for e in dedupe([b, a])] == ["20261020"]


def test_dedupe_same_period_far_apart_kept():
    a = ev(item="cn_m2", date="20251014", period="十月")
    b = ev(item="cn_m2", date="20261014", period="十月")
    assert [e["date"] for e in dedupe([a, b])] == ["20251014", "20261014"]


def test_dedupe_lpr_name_variants_same_day():
    a = ev(item="cn_lpr", date="20261020", time="09:00", value="3.00%")
    b = ev(item="cn_lpr", date="20261020", time="09:00", value="3.50%", period="十月")
    out = dedupe([a, b])
    assert len(out) == 1 and out[0]["value"] == "3.50%"
    assert dedupe([b, a])[0]["value"] == "3.00%"


def test_dedupe_sorted():
    out = dedupe([ev(item="us_nfp", region="us", date="20261106", time=None),
                  ev(item="cn_cpi_yoy", date="20261106", time="09:30"),
                  ev(item="cn_lpr", date="20261105", time="09:00")])
    assert [e["item"] for e in out] == ["cn_lpr", "cn_cpi_yoy", "us_nfp"]


def test_merge_fresh_value_overrides_stale_empty():
    stored = [ev(item="cn_cpi_yoy", date="20261109", value=None)]
    fresh = [ev(item="cn_cpi_yoy", date="20261109", value="0.8%")]
    assert merge(stored, fresh)[0]["value"] == "0.8%"


def test_merge_keeps_stored_when_fresh_missing():
    assert len(merge([ev(item="cn_lpr", date="20261020", value="3.50%")], [])) == 1


def test_next_release_same_day_morning_vs_evening():
    evs = [ev(item="cn_cpi_yoy", date="20261109", time="09:30"),
           ev(item="cn_cpi_yoy", date="20261209", time="09:30", period="十一月")]
    m = datetime(2026, 11, 9, 9, 0, tzinfo=SH)
    n = datetime(2026, 11, 9, 21, 30, tzinfo=SH)
    assert next_release(evs, "cn_cpi_yoy", m)["date"] == "20261109"
    assert next_release(evs, "cn_cpi_yoy", n)["date"] == "20261209"
    assert next_release(evs, "cn_ppi_yoy", m) is None


def test_next_release_converts_timezone():
    evs = [ev(date="20261109", time="09:30")]
    utc_now = datetime(2026, 11, 9, 2, 0, tzinfo=ZoneInfo("UTC"))  # 北京 10:00
    assert next_release(evs, "cn_cpi_yoy", utc_now) is None


def test_next_release_pending_time_counts_until_day_ends():
    now = datetime(2026, 11, 9, 20, 0, tzinfo=SH)
    assert not is_released(ev(date="20261109", time=None), now)
    assert is_released(ev(date="20261108", time=None), now)
    assert is_released(ev(date="20261201", time="09:30", value="0.8%"), now)
    assert is_released(ev(date="20261109", time=None, value="0.8%"), now)


def test_format_next():
    assert format_next("20261109", "09:30") == "下次公布：11/9 周一 09:30"
    assert format_next("20261113", None) == "下次公布：11/13 周五（时间待定）"


# ---------------------------------------------------------------- Task 3：取数与存档
import json
import logging

import pandas as pd
import pytest

import config
import econ_calendar as ec
from sources import RateLimited

TODAY = "20261108"
_COLS = ["date", "time", "country", "event", "value", "pre_value", "fore_value"]


def sh(d, n):
    return (datetime.strptime(d, "%Y%m%d") + __import__("datetime").timedelta(days=n)).strftime("%Y%m%d")


class FakeSrc:
    def __init__(self, rows_for=None, fail_at=None):
        self.calls = []
        self.rows_for = rows_for
        self.fail_at = fail_at

    def eco_cal(self, start, end, country):
        self.calls.append((start, end, country))
        if self.fail_at and len(self.calls) == self.fail_at:
            raise RateLimited("x")
        rows = self.rows_for(start, end, country) if self.rows_for else []
        return pd.DataFrame(rows, columns=_COLS)


def cpi_row(date):
    return (date, "09:30", "中国", "中国CPI年率(%)(年度)(十月)", "1.0%", "0.9%", "1.0%")


def test_fetch_window_chunks():
    src = FakeSrc()
    store, n = ec.fetch_events(src, TODAY, {"covered_from": None, "events": []}, 1000)
    assert n == len(src.calls)
    for country, size in config.ECO_CAL_CHUNK_DAYS.items():
        cs = sorted((s, e) for s, e, c in src.calls if c == country)
        assert all((ec._day(e) - ec._day(s)).days + 1 <= size for s, e in cs)
        for (s1, e1), (s2, e2) in zip(cs, cs[1:]):
            assert s2 == sh(e1, 1)
        assert cs[0][0] == sh(TODAY, -182)
        assert cs[-1][1] == sh(TODAY, 60)
    assert store["covered_from"] == sh(TODAY, -182)


def test_fetch_splits_when_truncated(caplog):
    def rows(start, end, country):
        if start == sh(TODAY, -14) and country == "中国":
            return [cpi_row(start)] * config.ECO_CAL_ROW_LIMIT
        return []
    src = FakeSrc(rows)
    with caplog.at_level(logging.WARNING):
        ec.fetch_events(src, TODAY, {"covered_from": None, "events": []}, 1000, backfill=False)
    cn = [(s, e) for s, e, c in src.calls if c == "中国"]
    assert cn[0] == (sh(TODAY, -14), sh(TODAY, 16))
    # 对半切开，且一直切到单日
    assert (cn[1][0], cn[1][1]) != cn[0] and cn[1][0] == cn[0][0]
    assert (sh(TODAY, -14), sh(TODAY, -14)) in cn
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    # 单日仍 100 行：不再继续切（同一单日只出现一次）
    assert cn.count((sh(TODAY, -14), sh(TODAY, -14))) == 1


def test_fetch_respects_max_calls_backfill_resumes():
    src = FakeSrc()
    store, n = ec.fetch_events(src, TODAY, {"covered_from": None, "events": []}, 20)
    assert n == 20 and len(src.calls) == 20
    refresh_calls = [c for c in src.calls if c[0] >= sh(TODAY, -14)]
    assert len(refresh_calls) == 14            # 刷新窗口优先取完
    cf1 = store["covered_from"]
    assert cf1 is not None and sh(TODAY, -182) < cf1 <= sh(TODAY, -14)
    src2 = FakeSrc()
    store2, _ = ec.fetch_events(src2, TODAY, store, 20)
    back2 = [c for c in src2.calls if c[1] < sh(TODAY, -14)]
    assert back2
    assert max(e for s, e, c in back2) <= cf1
    assert store2["covered_from"] < cf1


def test_fetch_stops_on_error_keeps_partial(caplog):
    old = ev(item="us_nfp", date="20261001", region="us")
    def rows(start, end, country):
        return [cpi_row(start)] if start == sh(TODAY, -14) and country == "中国" else []
    src = FakeSrc(rows, fail_at=3)
    with caplog.at_level(logging.WARNING):
        store, n = ec.fetch_events(src, TODAY, {"covered_from": None, "events": [old]}, 100)
    assert len(src.calls) == 3
    assert any("RateLimited" in r.getMessage() and "财经日历" in r.getMessage() for r in caplog.records)
    items = {(e["item"], e["date"]) for e in store["events"]}
    assert ("us_nfp", "20261001") in items and ("cn_cpi_yoy", sh(TODAY, -14)) in items
    assert store["_refresh_ok"] is False


def test_load_store_missing_or_corrupt(tmp_path, caplog):
    empty = {"covered_from": None, "events": []}
    with caplog.at_level(logging.WARNING):
        assert ec.load_store(str(tmp_path / "nope.json")) == empty
    assert not caplog.records
    for content in ("", "not json", '{"events": 5}'):
        p = tmp_path / "bad.json"
        p.write_text(content)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert ec.load_store(str(p)) == empty
        assert any("无法读取" in r.getMessage() for r in caplog.records)


def test_save_then_load_roundtrip(tmp_path):
    p = str(tmp_path / "sub" / "e.json")
    store = {"covered_from": "20260601", "events": [ev(date="20261110"), ev(date="20261101", label="中文")],
             "_refresh_ok": True}
    ec.save_store(p, store)
    got = ec.load_store(p)
    assert got["covered_from"] == "20260601"
    assert [e["date"] for e in got["events"]] == ["20261101", "20261110"]
    assert "_refresh_ok" not in got
    assert "中文" in open(p, encoding="utf-8").read()
    assert not (tmp_path / "sub" / "e.json.tmp").exists()


def test_normalize_drops_bad_dates():
    rows = [{"date": "", "time": "09:30", "event": "中国CPI年率(%)(年度)(十月)"},
            {"date": "2026-11-09", "time": "09:30", "event": "中国CPI年率(%)(年度)(十月)"},
            {"date": "20261109", "time": "09:30", "event": "中国CPI年率(%)(年度)(十月)"}]
    assert [e["date"] for e in normalize(rows)] == ["20261109"]


# ---------------------------------------------------------------- 终审修复：改期 / 取消 / 存档不裁剪 / 坏行

_US_CPI_OCT = "美国未季调CPI年率(%)(年度)(十月)"


def _src_with(rows):
    """rows: [(date, time, country, event, value)]，按段返回落在 [start, end] 内、国家相同的行。"""
    def f(start, end, country):
        return [(d, t, c, e, v, "", "") for d, t, c, e, v in rows if c == country and start <= d <= end]
    return f


def _fetch(stored, rows, **kw):
    store, _ = ec.fetch_events(FakeSrc(_src_with(rows), **kw), TODAY,
                               {"covered_from": None, "events": stored}, 1000, backfill=False)
    return store


def test_merge_fresh_beats_stored_when_neither_has_value():
    stored = [ev(item="us_cpi_yoy", region="us", date="20261113", period="十月")]
    fresh = [ev(item="us_cpi_yoy", region="us", date="20261110", period="十月")]
    out = merge(stored, fresh)
    assert [e["date"] for e in out] == ["20261110"]
    assert "_fresh" not in out[0] and set(out[0]) == set(fresh[0])
    assert merge(fresh, stored)[0]["date"] == "20261113"   # 反过来：后来的（fresh）赢


def test_fetch_moved_earlier_replaces_stored():
    stored = [ev(item="us_cpi_yoy", region="us", date="20261113", period="十月")]
    store = _fetch(stored, [("20261110", "21:30", "美国", _US_CPI_OCT, "")])
    assert [e["date"] for e in store["events"] if e["item"] == "us_cpi_yoy"] == ["20261110"]
    now = datetime(2026, 11, 8, 10, 0, tzinfo=SH)
    assert next_release(store["events"], "us_cpi_yoy", now)["date"] == "20261110"


def test_fetch_no_period_moved_keeps_only_fresh_date():
    stored = [ev(item="cn_lpr", date="20261113", time="09:00")]
    store = _fetch(stored, [("20261117", "09:00", "中国", "中国央行贷款市场报价利率(LPR)", "")])
    assert [e["date"] for e in store["events"] if e["item"] == "cn_lpr"] == ["20261117"]


def test_fetch_cancelled_row_dropped_only_when_refresh_ok():
    cancelled = ev(item="cn_m2", date="20261113", period="十月")
    assert not [e for e in _fetch([cancelled], [])["events"] if e["item"] == "cn_m2"]
    failed = _fetch([cancelled], [], fail_at=1)
    assert failed["_refresh_ok"] is False
    assert [e["date"] for e in failed["events"] if e["item"] == "cn_m2"] == ["20261113"]


def test_fetch_keeps_valued_and_out_of_window_rows():
    valued = ev(item="cn_lpr", date="20261101", value="3.00%")
    old = ev(item="cn_m2", date="20261001", period="九月")
    far = ev(item="cn_gdp", date=sh(TODAY, 61), period="第四季度")
    store = _fetch([valued, old, far], [])
    assert {(e["item"], e["date"]) for e in store["events"]} == {
        ("cn_lpr", "20261101"), ("cn_m2", "20261001"), ("cn_gdp", sh(TODAY, 61))}


def test_fetch_keeps_rows_older_than_keep_days_in_store():
    ancient = ev(item="cn_lpr", date=sh(TODAY, -400), value="3.45%")
    store = _fetch([ancient], [])
    assert [e["date"] for e in store["events"]] == [sh(TODAY, -400)]


def test_load_store_drops_malformed_rows(tmp_path):
    good = ev(item="cn_lpr", date="20261020", time="09:00", value="3.00%")
    bad = [5, {"date": "20261020"}, dict(good, date="2026-10-20"), dict(good, item=None),
           {k: v for k, v in good.items() if k != "value"}]
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"covered_from": "20260601", "events": [good, *bad]}), encoding="utf-8")
    got = ec.load_store(str(p))
    assert got["covered_from"] == "20260601"
    assert len(got["events"]) == 1 and got["events"][0]["value"] == "3.00%"
    ec.merge(got["events"], [])   # 不再 KeyError


def test_load_store_refreshes_from_whitelist(tmp_path):
    gone = ev(item="us_powell_old", region="us", date="20261020")
    stale = ev(item="cn_m2", date="20261014", period="九月", label="旧名字", major=False, region="us",
               value="7.2%")
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"covered_from": None, "events": [gone, stale]}), encoding="utf-8")
    got = ec.load_store(str(p))["events"]
    assert len(got) == 1
    e = got[0]
    assert (e["item"], e["label"], e["major"], e["region"], e["period"], e["value"]) == (
        "cn_m2", "M2 年率 · 九月", True, "cn", "九月", "7.2%")
