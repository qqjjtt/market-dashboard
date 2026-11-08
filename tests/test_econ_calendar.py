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
