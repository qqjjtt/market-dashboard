"""Offline tests for the limit dashboard's data semantics and source call budget."""
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

import limit_tracker as tracker


DAYS = ["20260105", "20260106", "20260107", "20260108", "20260109", "20260112"]


def limit(day, code, board=1, typ="U", **overrides):
    return {
        "trade_date": day, "ts_code": code, "name": "同名股", "limit": typ,
        "industry": "源行业", "limit_times": board, "up_stat": f"{board}/{board}",
        "amount": 2e8, "fd_amount": 1e8, "float_mv": 10e8,
        "first_time": "092500", "last_time": "09:25:00", "open_times": 0,
        **overrides,
    }


def topic(day, code, tag="涨停", **overrides):
    return {"trade_date": day, "ts_code": code, "tag": tag, "lu_desc": "机器人",
            "theme": "机器人、算力", "status": "99连板", "limit_order": 8e8, **overrides}


def members(code="000001.SZ"):
    return pd.DataFrame([{
        "ts_code": code, "l1_code": "801080.SI", "l1_name": "电子",
        "l2_code": "801083.SI", "l2_name": "元件Ⅱ", "l3_code": "850822.SI", "l3_name": "印制电路板Ⅲ",
    }])


def write_day(cache, prefix, day, rows):
    pd.DataFrame(rows).to_csv(cache / f"{prefix}_{day}.csv", index=False)


def write_calendar(cache, days):
    (cache / "trade_cal.json").write_text(json.dumps({"days": days}), encoding="utf-8")


class NoAPI:
    def __getattr__(self, name):
        raise AssertionError(f"Unexpected upstream API access: {name}")


def test_limit_source_is_authoritative_and_missing_values_stay_missing():
    day, wrong_day = DAYS[:2]
    a, b, c, d, kpl_only = "000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ", "000005.SZ"
    a_row = limit(day, a, 2, up_stat="3/5", pct_chg=0, amount=None, fd_amount=None,
                  float_mv=None, first_time=0, last_time="bad", open_times=None)
    lim = pd.DataFrame([a_row, a_row, limit(day, b, 1), limit(day, c, None, "Z"),
                        limit(day, d, 2, "D"), limit(day, "000006.SZ", 1, "")])
    ku = pd.DataFrame([topic(day, a, net_change=0), topic(wrong_day, b),
                       topic(day, c), topic(day, kpl_only, name="*ST仅开盘啦")])
    kz = pd.DataFrame([topic(day, d, tag="炸板"), topic(day, a, tag="炸板", lu_desc="不匹配")])
    up, zha, down = tracker.enrich_limit_stocks(lim, ku, kz, members(), include_kpl_extra=True)
    assert [s["ts_code"] for s in up] == [a, b]
    assert [s["ts_code"] for s in zha] == [c]
    assert [s["ts_code"] for s in down] == [d]
    s = up[0]
    assert s["concepts"] == ["机器人", "算力"]
    assert (s["limit_times"], s["stat_label"], s["trend_boards"], s["trend_days"], s["is_trend"]) == (2, "5天3板", 3, 5, True)
    assert (s["first_time"], s["last_time"], s["style"], s["open_times"]) == ("—", "—", "—", None)
    for field in ("amount_yi", "fd_yi", "seal_to_amount", "seal_to_mv", "float_mv_yi", "total_mv_yi", "close"):
        assert s[field] is None
    assert s["pct_chg"] == s["net_change_yi"] == 0
    assert s["sw_l2"] == "元件" and s["sw_l3"] == "印制电路板"
    for s in (up[1], zha[0], down[0]):
        assert s["concepts"] == [] and s["primary_concept"] == ""
        assert s["sw_l2"] == "未分类" and s["industry"] == "源行业"
    assert tracker.enrich_limit_stocks(pd.DataFrame(), ku, kz, members()) == ([], [], [])
    assert tracker.classify_board_style("92500", "09:25:00", 0) == "一字板"
    assert tracker.classify_board_style("92500", "10:03:00", None) == "T字板"


def test_exact_adjacent_promotion_and_price_coverage():
    a, b, c, d, e, f = [f"{i:06d}.SZ" for i in range(1, 7)]
    prev_df = pd.DataFrame([limit(DAYS[0], a), limit(DAYS[0], b, 2),
                            limit(DAYS[0], c, 4), limit(DAYS[0], d, 5), limit(DAYS[0], e)])
    today_df = pd.DataFrame([limit(DAYS[1], a, 2), limit(DAYS[1], b, 4), limit(DAYS[1], c, 5),
                             limit(DAYS[1], f, 2), limit(DAYS[1], d, 1, "D", pct_chg=-10),
                             limit(DAYS[1], e, None)])
    prev, _, _ = tracker.enrich_limit_stocks(prev_df, None, None, None)
    up, zha, down = tracker.enrich_limit_stocks(today_df, None, None, None)
    quotes = pd.DataFrame([{"ts_code": a, "open": 11, "pre_close": 10, "pct_chg": -4.996}])
    result = tracker.compute_promotion_and_yesterday_effect(up, zha, prev, quotes, today_down=down)
    tiers = {t["from_board"]: t for t in result["tiers"]}
    assert (result["prev_count"], result["eligible_count"], result["promoted_count"], result["unknown_count"]) == (5, 4, 2, 1)
    assert result["overall_promo_rate"] == 50
    assert [x["ts_code"] for x in tiers[1]["success"]] == [a]
    assert [x["ts_code"] for x in tiers[1]["unknown"]] == [e]
    assert [x["ts_code"] for x in tiers[2]["failed"]] == [b]
    assert [x["ts_code"] for x in tiers[4]["success"]] == [c]
    assert [x["ts_code"] for x in tiers[4]["failed"]] == [d]
    assert result["avg_open_pct"] == 10 and result["quote_count"] == 2
    assert result["big_loss_count"] == 1  # don't round -4.996 before classifying
    assert result["up_close_ratio"] == result["up_Close_ratio"] == 0
    assert result["broken_high_boards"][0]["today_pct"] == -10


def test_replay_and_rotations_keep_unavailable_trading_sessions(tmp_path):
    write_calendar(tmp_path, DAYS)
    members().to_csv(tmp_path / "sw_members.csv", index=False)
    a, b, c = "000001.SZ", "000002.SZ", "000003.SZ"
    for day, entries in zip(DAYS, (
        [(a, 1)], [(a, 2), (b, 2)], [], [(a, 3)], [(a, 4)], [(a, 5)],
    )):
        if entries:
            write_day(tmp_path, "limit_list", day, [limit(day, code, height) for code, height in entries])
    write_day(tmp_path, "kpl_u", DAYS[0], [topic(DAYS[0], a)])
    write_day(tmp_path, "kpl_u", DAYS[3], [topic(DAYS[3], a)])
    write_day(tmp_path, "kpl_u", DAYS[5], [topic(DAYS[5], a), topic(DAYS[5], c)])
    store = tracker.LimitDataStore(pro=NoAPI(), cache_dir=tmp_path, offline=True)
    history = tracker.build_multi_day_history(store, DAYS, members())
    assert history["dates"] == DAYS
    assert history["unavailable_dates"] == [DAYS[2]]
    assert [r["promo_rate"] for r in history["daily"]] == [None, 100, None, None, 100, 100]
    assert [r["b1_to_b2_rate"] for r in history["daily"]] == [None, 100, None, None, None, None]
    assert history["daily"][2]["up_count"] is None
    assert history["daily"][3]["up_count"] == 1
    assert history["sw_l3_rotation"][0]["series"] == [1, 1, None, 1, 1, 1]
    assert history["concept_rotation"][0]["series"] == [1, None, None, 1, None, 1]
    selected = tracker.build_dashboard_payload(store, DAYS[2])
    assert selected["meta"]["trade_date"] == DAYS[2]
    assert selected["meta"]["latest_available_date"] == DAYS[-1]
    assert selected["overview"]["up_count"] is None
    assert selected["promotion"]["available"] is False
    after_gap = tracker.build_dashboard_payload(store, DAYS[3])
    assert after_gap["overview"]["prev_date"] == DAYS[2]
    assert after_gap["promotion"]["overall_promo_rate"] is None
    latest = tracker.build_dashboard_payload(store, lookback_days=5)
    assert latest["history"]["daily"][0]["promo_rate"] == 100  # predecessor outside plotted window
    assert DAYS[2] in latest["meta"]["available_dates"]
    history_stock = tracker.build_stock_history(store, a, days=6, end_date=DAYS[-1])
    assert history_stock["observed_days"] == 5 and history_stock["unavailable_dates"] == [DAYS[2]]
    assert tracker.build_stock_history(store, c, days=6)["events"] == []
    json.dumps(latest, allow_nan=False)


def test_sw_cross_and_board_capital_preserve_legacy_payload_and_nulls():
    day = DAYS[0]
    a, b, c = "000001.SZ", "000002.SZ", "000003.SZ"
    sw = pd.concat([members(a), members(b), members(c).assign(l2_code="801099.SI", l2_name="仅跌停行业")])
    lim = pd.DataFrame([limit(day, a, 2, amount=0, fd_amount=0),
                        limit(day, b, None, fd_amount=None, up_stat=None), limit(day, c, 1, "D")])
    ku = pd.DataFrame([topic(day, a, theme="机器人、电子、算力、光通信", net_change=0)])
    up, zha, down = tracker.enrich_limit_stocks(lim, ku, None, sw)
    ladder = tracker.build_ladder(up)
    assert [r["board"] for r in ladder] == [2]
    assert ladder[0]["amount_yi"] == ladder[0]["fd_yi"] == 0
    assert ladder[0]["avg_turnover"] is None
    sw_quotes = pd.DataFrame([{"ts_code": "801083.SI", "name": "元件", "amount": 1e4},
                              {"ts_code": "wrong", "name": "仅跌停行业", "amount": 2e4}])
    matrix = tracker.build_sw_capital_matrix(up, zha, down, sw_quotes)
    row, down_only = matrix
    assert row["board_capital"]["b2"] == {"count": 1, "amount_yi": 0, "fd_yi": 0}
    assert row["board_capital"]["unknown"]["count"] == 1
    assert row["fd_yi"] is None and row["net_change_yi"] is None
    assert row["index_amount_yi"] == 1 and row["index_mv_yi"] is None
    assert down_only["down_count"] == 1 and down_only["index_amount_yi"] is None
    ranking, cross = tracker.build_concept_ranking_and_cross(up, zha)
    assert {r["name"] for r in ranking} == {"机器人", "算力", "电子", "光通信"}
    assert cross["sw_l2_list"] == cross["levels"]["L2"]["industry_list"]
    assert cross["cells"] == cross["levels"]["L2"]["cells"]
    for level, industry in (("L1", "电子"), ("L2", "元件"), ("L3", "印制电路板")):
        cells = cross["levels"][level]["cells"]
        assert {c["industry"] for c in cells} == {industry}
        assert all(x["amount_yi"] == x["fd_yi"] == 0 and x["count"] == 1 for x in cells)


class FakePro:
    def __init__(self, days, limits, concepts=(), max_rows=10000):
        self.days = days
        self.limits = limits
        self.concepts = concepts
        self.max_rows = max_rows
        self.calls = []

    def _result(self, source, rows, trade_date=None, start_date=None, end_date=None, tag=None):
        lo, hi = (trade_date, trade_date) if trade_date else (start_date, end_date)
        self.calls.append((source, tag, lo, hi))
        return pd.DataFrame([r for r in rows if lo <= r["trade_date"] <= hi and
                             (tag is None or r["tag"] == tag)][:self.max_rows])

    def limit_list_d(self, limit_type=None, **kwargs):
        rows = self.limits
        if limit_type:
            rows = [dict(r, tag=limit_type) for r in rows if r["limit"] == limit_type]
        return self._result("lim", rows, tag=limit_type, **kwargs)

    def kpl_list(self, **kwargs):
        return self._result("kpl", self.concepts, **kwargs)

    def index_member_all(self, **kwargs):
        return members()

    def sw_daily(self, **kwargs):
        return self._result("sw", [], **kwargs)

    def daily(self, fields, **kwargs):
        return self._result("daily", [], **kwargs)

    def trade_cal(self, **kwargs):
        self.calls.append(("calendar", None, kwargs["start_date"], kwargs["end_date"]))
        return pd.DataFrame({"cal_date": self.days})


def test_range_chunks_skip_complete_caches_and_split_saturation(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "RANGE_TRADE_DAYS", 4)
    monkeypatch.setattr(tracker, "LIMIT_LIST_MAX_ROWS", 5)
    monkeypatch.setattr(tracker, "KPL_MAX_ROWS", 5)
    dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-01-05", periods=15)]
    rows = [limit(d, code) for d in dates for code in ("000001.SZ", "000002.SZ")]
    topics = [topic(d, code) for d in dates for code in ("000001.SZ", "000002.SZ")]
    # A stale sentinel cache is preserved; only this date is skipped.
    write_day(tmp_path, "limit_list", dates[5], [limit(dates[5], "000009.SZ")])
    pro = FakePro(dates, rows, topics, max_rows=5)
    store = tracker.LimitDataStore(pro, tmp_path)
    lim, ku, kz = store.ensure_limit_range(dates)
    assert len(lim) == len(ku) == 15 and not kz
    assert lim[dates[5]]["ts_code"].tolist() == ["000009.SZ"]
    assert all(len(lim[d]) == 2 for d in dates if d != dates[5])
    assert all(len(ku[d]) == 2 for d in dates)
    assert all(dates.index(hi) - dates.index(lo) < 4 for _, _, lo, hi in pro.calls)
    assert not any(src == "lim" and lo <= dates[5] <= hi for src, _, lo, hi in pro.calls)
    assert len(pro.calls) == len(set(pro.calls))
    pro.calls.clear()
    store.ensure_limit_range(dates)
    assert pro.calls == []


def test_saturated_singleton_is_not_persisted_and_empty_day_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "LIMIT_LIST_MAX_ROWS", 2)
    day, missing = DAYS[:2]
    pro = FakePro(DAYS, [limit(day, "000001.SZ"), limit(day, "000002.SZ")], max_rows=2)
    store = tracker.LimitDataStore(pro, tmp_path)
    lim, _, _ = store.ensure_limit_range([day, missing])
    assert all(f.empty for f in lim.values())
    assert not list(tmp_path.glob("limit_list_*.csv"))
    assert pro.calls == [("lim", None, day, missing), ("lim", None, day, day),
                         ("lim", "U", day, day), ("lim", None, missing, missing)]


def test_saturated_singleton_is_complete_after_disjoint_status_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "LIMIT_LIST_MAX_ROWS", 2)
    day = DAYS[0]
    pro = FakePro(DAYS, [limit(day, "000001.SZ", typ="U"), limit(day, "000002.SZ", typ="Z"),
                         limit(day, "000003.SZ", typ="D")], max_rows=2)
    store = tracker.LimitDataStore(pro, tmp_path)
    assert list(store.get_limit_list(day)["limit"]) == ["U", "Z", "D"]
    assert list(store.get_limit_list(day)["limit"]) == ["U", "Z", "D"]
    assert pro.calls == [("lim", t, day, day) for t in (None, "U", "Z", "D")]


def test_dashboard_fetches_selected_sources_once_and_reuses_prev_snapshot(tmp_path, monkeypatch):
    dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-01-01", periods=67)]

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.strptime(dates[-1], "%Y%m%d").replace(tzinfo=tz)

    monkeypatch.setattr(tracker, "datetime", FixedDateTime)
    rows = [limit(d, "000001.SZ", i + 1) for i, d in enumerate(dates[:-1])]
    pro = FakePro(dates, rows)
    store = tracker.LimitDataStore(pro, tmp_path)
    payload = tracker.build_dashboard_payload(store, lookback_days=13, refresh=True)
    date = dates[-2]
    assert payload["meta"]["trade_date"] == date
    assert payload["promotion"]["overall_promo_rate"] == payload["history"]["daily"][-1]["promo_rate"] == 100
    requests_covering_selected = Counter((source, tag) for source, tag, lo, hi in pro.calls
                                         if source != "calendar" and lo <= date <= hi)
    assert requests_covering_selected == {("lim", None): 1, ("kpl", "涨停"): 1, ("sw", None): 1, ("daily", None): 1}
    assert pro.calls.count(("lim", None, dates[-1], dates[-1])) == 1
    assert sum(source == "calendar" for source, *_ in pro.calls) == 1


def test_local_november_cache_builds_strict_json_without_upstream(tmp_path, monkeypatch):
    fixture = Path(tracker.CACHE_DIR)
    if not (fixture / "limit_list_20261126.csv").exists():
        pytest.skip("optional checkout-local November cache is absent")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 27, 12, tzinfo=tz)

    monkeypatch.setattr(tracker, "datetime", FixedDateTime)
    for f in fixture.iterdir():
        if f.suffix in (".csv", ".json"):
            shutil.copy(f, tmp_path / f.name)
    store = tracker.LimitDataStore(pro=NoAPI(), cache_dir=tmp_path, offline=True)
    payload = tracker.build_dashboard_payload(store, "20261126", 15)
    assert (payload["overview"]["up_count"], payload["overview"]["zha_count"], payload["overview"]["down_count"]) == (65, 29, 5)
    assert payload["promotion"]["overall_promo_rate"] == 27.5
    assert payload["history"]["daily"][-1]["promo_rate"] == 27.5
    assert payload["history"]["unavailable_dates"] == []
    assert payload["meta"]["coverage"]["sw_daily"] is True
    assert "limit_list_d" in payload["meta"]["source_scope"]
    assert all(payload["cross_matrix"]["levels"][level]["cells"] for level in ("L1", "L2", "L3"))
    json.dumps(payload, allow_nan=False)


class Recorder:
    """Returns a fixed frame per API and records each upstream call."""

    def __init__(self, frames=None, fail=()):
        self.frames, self.fail, self.calls = frames or {}, set(fail), []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append(name)
            if name in self.fail:
                raise RuntimeError("upstream down")
            return self.frames.get(name, pd.DataFrame())
        return call


def _stamp(path, when):
    ts = when.timestamp()
    import os
    os.utime(path, (ts, ts))


def test_same_day_cache_before_settle_is_refetched_but_final_cache_is_not(tmp_path):
    day = DAYS[0]
    write_day(tmp_path, "limit_list", day, [limit(day, "000001.SZ")])
    path = tmp_path / f"limit_list_{day}.csv"
    fresh = pd.DataFrame([limit(day, "000001.SZ"), limit(day, "000002.SZ")])

    _stamp(path, datetime(2026, 1, 5, 15, 40, tzinfo=tracker.SH))  # 当天收盘后不久写入：临时
    api = Recorder({"limit_list_d": fresh})
    got = tracker.LimitDataStore(api, tmp_path).get_limit_list(day)
    assert api.calls == ["limit_list_d"] and len(got) == 2

    _stamp(path, datetime(2026, 1, 5, 19, 0, tzinfo=tracker.SH))  # 定稿后写入：不再请求
    assert len(tracker.LimitDataStore(NoAPI(), tmp_path).get_limit_list(day)) == 2
    _stamp(path, datetime(2026, 1, 6, 9, 0, tzinfo=tracker.SH))  # 次日写入的历史日：最终
    assert len(tracker.LimitDataStore(NoAPI(), tmp_path).get_limit_list(day)) == 2


def test_provisional_daily_refetch_failure_keeps_existing_cache(tmp_path):
    day = DAYS[0]
    path = tmp_path / f"daily_{day}.csv"
    pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": day, "close": 10.0}]).to_csv(path, index=False)
    _stamp(path, datetime(2026, 1, 5, 16, 0, tzinfo=tracker.SH))
    api = Recorder(fail={"daily"})
    got = tracker.LimitDataStore(api, tmp_path).get_stock_daily(day)
    assert api.calls == ["daily"] and got["ts_code"].tolist() == ["000001.SZ"]
    offline = tracker.LimitDataStore(NoAPI(), tmp_path, offline=True)
    assert len(offline.get_stock_daily(day)) == 1  # 离线模式从不重取
