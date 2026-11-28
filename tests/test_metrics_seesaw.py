import json
from types import SimpleNamespace

import pandas as pd
import pytest

import build
import config
from metrics_seesaw import calculate


def _theme(key, *members):
    return dict(key=key, name=key, etfs=[dict(code=e, name=e, index_code=i, index_name=i) for e, i in members])


BASKETS = {
    "cash": [_theme("fcf", ("A", "I1"), ("B", "I1")), _theme("dividend", ("C", "I2"), ("D", "I3"))],
    "tech": [_theme("tech", ("E", "I4"))],
}
THRESHOLDS = dict(return_pct=0.5, flow_pct=0.2)


def inputs(n):
    days = pd.bdate_range("2026-04-20", periods=n + 1).strftime("%Y%m%d").tolist()
    # Make the calendar irregular (a market holiday), not inferred from price observations.
    del days[1]
    index = {f"I{i}": pd.Series([100.0 + k * i for k in range(n)], index=days) for i in range(1, 5)}
    shares = {e: pd.Series([scale + k * change for k in range(n)], index=days, dtype=float)
              for e, scale, change in [("A", 1, 1), ("B", 10, 0), ("C", 6, -1), ("D", 30, 0), ("E", 100, 4)]}
    prices = {e: pd.Series([2 + k for k in range(n)], index=days, dtype=float) for e in shares}
    # With longer samples keep C's shares valid aside from explicit missing rows.
    if n > 6:
        shares["C"] += n
    return days, index, shares, prices


def iso(day):
    return pd.Timestamp(day).strftime("%Y-%m-%d")


def test_seesaw_weights_themes_and_normalizes_matching_starting_scale():
    days, index, shares, prices = inputs(6)
    prices["C"] *= 2
    p = calculate(days, days[-1], BASKETS, THRESHOLDS, index, shares, prices)
    w = p["windows"]["5"]
    assert (w["base_date"], w["start"], w["end"]) == (iso(days[0]), iso(days[1]), iso(days[-1]))
    # [(I1), (I2+I3)/2] / 2, not an ETF-count- or asset-weighted return.
    assert w["cash"]["return_pct"] == pytest.approx((5 + (10 + 15) / 2) / 2)
    assert w["tech"]["return_pct"] == pytest.approx(20)
    assert w["return_spread_pct"] == pytest.approx(11.25)
    # A adds 1 share per day; C sheds 1 at 2× A's price; E adds 4.
    assert w["cash"]["flow_amount"] == -25
    assert w["cash"]["flow_pct"] == pytest.approx(-25 / (1*2 + 10*2 + 6*4 + 30*2) * 100)
    assert w["tech"]["flow_amount"] == 100
    assert w["tech"]["flow_pct"] == 50
    assert w["flow_spread_pct"] == pytest.approx(50 - w["cash"]["flow_pct"])
    assert p["windows"]["20"] is None and p["thresholds"] == THRESHOLDS
    assert p["history"][0]["flow_spread_pct"] == w["flow_spread_pct"]
    json.dumps(p, allow_nan=False)


def test_seesaw_independent_latest_exact_windows_and_history_gaps():
    days, index, shares, prices = inputs(61)
    # Old gap breaks only the longer lookback; latest gap affects all windows on the last day.
    shares["A"] = shares["A"].drop([days[43], days[60]])
    index["I2"].loc[days[49]] = float("inf")
    p = calculate(list(reversed(days)), days[30], BASKETS, THRESHOLDS, index, shares, prices)
    assert p["asof"] == iso(days[-1])
    assert p["windows"]["1"]["end"] == p["windows"]["5"]["end"] == iso(days[59])
    assert p["windows"]["20"]["end"] == iso(days[42])
    assert p["windows"]["20"]["start"] == iso(days[23])
    history = {r["date"]: r for r in p["history"]}
    assert list(history) == list(map(iso, days[30:]))
    assert history[iso(days[30])]["cash_flow_pct"] is not None  # pre-history warmup
    for k in (43, 48, 49, 54, 60):
        assert all(v is None for field, v in history[iso(days[k])].items() if field != "date")
    assert history[iso(days[55])]["cash_flow_pct"] is not None


def test_seesaw_missing_member_never_becomes_zero_or_subset():
    days, index, shares, prices = inputs(27)
    del index["I3"]
    amounts = {"A": pd.Series(2.0, index=days)}
    p = calculate(days, days[5], BASKETS, THRESHOLDS, index, shares, prices, amounts)
    assert p["status"] == "unavailable" and p["asof"] == iso(days[-1])
    assert list(p["windows"].values()) == [None, None, None]
    assert len(p["history"]) == 22
    assert all(r["cash_return_pct"] is None and r["tech_flow_pct"] is None for r in p["history"])
    detail = p["etf_windows"]["5"]
    assert detail["end"] == iso(days[-1])
    assert detail["members"]["A"] == pytest.approx(dict(
        close=28, return_pct=100 * (28 / 23 - 1), flow_amount=130,
        flow_pct=100 * 130 / (22 * 23), turnover=10,
    ))
    assert detail["members"]["B"]["turnover"] is None


def test_seesaw_rejects_duplicate_and_nonpositive_observations():
    days, index, shares, prices = inputs(25)
    shares["E"] = pd.concat([shares["E"], shares["E"].iloc[[-1]]])
    prices["C"].loc[days[-2]] = 0
    p = calculate(days, days[0], BASKETS, THRESHOLDS, index, shares, prices)
    assert {w["end"] for w in p["windows"].values()} == {iso(days[-3])}


def _split_case(lead):
    """E 在第 3 日 1 拆 2：价格自第 3 日减半，份额在第 3 日（lead=0）或提前一日（lead=1）翻倍。"""
    days, index, shares, prices = inputs(6)
    factors = {e: pd.Series(1.0, index=days) for e in shares}
    factors["E"].iloc[3:] = 2.0
    prices["E"].iloc[3:] /= 2
    shares["E"].iloc[3 - lead:] *= 2
    return days, index, shares, prices, factors


@pytest.mark.parametrize("lead", [0, 1])
def test_seesaw_split_is_not_a_subscription(lead):
    days, index, shares, prices, factors = _split_case(lead)
    p = calculate(days, days[-1], BASKETS, THRESHOLDS, index, shares, prices, etf_factors=factors)
    w = p["windows"]["5"]
    # Same as the unsplit series: E adds 4 shares a day at prices 3..7.
    assert w["tech"]["flow_amount"] == pytest.approx(100)
    assert w["tech"]["flow_pct"] == pytest.approx(50)
    member = p["etf_windows"]["5"]["members"]["E"]
    assert member["close"] == pytest.approx(3.5)  # raw, post-split quote
    assert member["return_pct"] == pytest.approx(100 * (7 / 2 - 1))
    assert member["flow_amount"] == pytest.approx(100)
    # Without factors the split shows up as a huge inflow — the bug this guards against.
    raw = calculate(days, days[-1], BASKETS, THRESHOLDS, index, shares, prices)
    assert raw["windows"]["5"]["tech"]["flow_pct"] > 100


def test_seesaw_unmatched_split_becomes_gap_and_missing_factor_is_missing_data():
    days, index, shares, prices, factors = _split_case(0)
    shares["E"].iloc[3:] /= 2  # factor doubles but disclosed shares never do
    p = calculate(days, days[-1], BASKETS, THRESHOLDS, index, shares, prices, etf_factors=factors)
    assert p["windows"]["5"] is None and p["windows"]["1"]["end"] == iso(days[-1])
    assert [r["tech_flow_pct"] is None for r in p["history"]] == [True]
    days, index, shares, prices, factors = _split_case(0)
    del factors["A"]
    p = calculate(days, days[0], BASKETS, THRESHOLDS, index, shares, prices, etf_factors=factors)
    assert p["status"] == "unavailable" and list(p["windows"].values()) == [None, None, None]


def test_seesaw_dividend_adjusts_member_return_only():
    days, index, shares, prices = inputs(6)
    factors = {e: pd.Series(1.0, index=days) for e in shares}
    factors["C"].iloc[4:] = 1.05  # cash distribution: no share change
    p = calculate(days, days[-1], BASKETS, THRESHOLDS, index, shares, prices, etf_factors=factors)
    base = calculate(days, days[-1], BASKETS, THRESHOLDS, index, shares, prices)
    assert p["windows"]["5"] == base["windows"]["5"]
    assert p["etf_windows"]["5"]["members"]["C"]["return_pct"] == pytest.approx(100 * (7 * 1.05 / 2 - 1))


def test_build_seesaw_respects_demo_and_calendar():
    p = build._seesaw(SimpleNamespace(D="20261127", demo=True))
    assert p["status"] == "demo" and p["asof"] == "2026-11-27"
    assert p["windows"] == {"1": None, "5": None, "20": None} and p["history"] == []
    p = build._seesaw(SimpleNamespace(D="20261127", demo=False, shared={"cal": ["20261126"]}))
    assert p["status"] == "unavailable" and not p["history"]


def test_build_seesaw_queries_mapped_codes_once_with_warmup_and_handles_source_failure(caplog):
    days = pd.bdate_range("2026-04-01", "2026-11-30").strftime("%Y%m%d").tolist()
    asof = "20261127"
    expected_start = [d for d in days if d < "20260527"][-20]
    members = [e for side in config.SEESAW_BASKETS.values() for theme in side for e in theme["etfs"]]
    calls = []

    def make_series(method, field):
        def fetch(code, start, end):
            calls.append((method, code))
            assert start == expected_start and end == asof
            if method == "share" and code == "512890.SH":
                raise RuntimeError("no disclosure source")
            dates = [d for d in days if start <= d <= end]
            return pd.DataFrame({"trade_date": dates, field: 10.0})
        return fetch

    src = SimpleNamespace(index_daily=make_series("index", "close"), etf_share=make_series("share", "share"),
                          etf_daily=make_series("daily", "close"), etf_adj=make_series("adj", "adj_factor"))
    p = build._seesaw(SimpleNamespace(D=asof, demo=False, src=src, shared={"cal": days}))
    assert {code for method, code in calls if method == "index"} == {e["index_code"] for e in members}
    assert len(calls) == len(set(calls)) == 4 * len(members)
    assert p["status"] == "unavailable"
    assert p["history"][0]["date"] == "2026-05-27" and p["history"][-1]["date"] == "2026-11-27"
    assert "512890.SH" in caplog.text and "no disclosure source" in caplog.text


def test_seesaw_timeline_has_every_window_for_every_history_day():
    days, index, shares, prices = inputs(30)
    shares["A"] = shares["A"].drop([days[20]])
    p = calculate(days, days[10], BASKETS, THRESHOLDS, index, shares, prices)
    t = p["timeline"]
    assert t["dates"] == list(map(iso, days[10:]))
    for n in ("1", "5", "20"):
        assert all(len(col) == len(t["dates"]) for col in t[n].values())
    last = p["windows"]["5"]
    assert t["5"]["start"][-1] == last["start"]
    assert t["5"]["tech_flow_amount"][-1] == pytest.approx(last["tech"]["flow_amount"])
    assert t["5"]["flow_spread_pct"][-1] == pytest.approx(last["flow_spread_pct"], abs=1e-4)
    # The missing day blanks every window covering it, and nothing else in the 1-day series.
    at = t["dates"].index(iso(days[20]))
    assert t["1"]["cash_flow_pct"][at] is None and t["1"]["cash_flow_pct"][at + 1] is None
    assert t["1"]["cash_flow_pct"][at + 2] is not None
    # Too short before day 20, and every later 20-day window spans the gap.
    assert set(t["20"]["start"]) == {None} and p["windows"]["20"] is None
    assert [r["flow_spread_pct"] for r in p["history"]] == pytest.approx(t["5"]["flow_spread_pct"], abs=1e-4)
    json.dumps(p, allow_nan=False)
