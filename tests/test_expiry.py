import pandas as pd

import expiry


def _days(*months):
    out = []
    for a, b in months:
        out += [d.strftime("%Y%m%d") for d in pd.bdate_range(a, b)]
    return out


TD = _days(("2026-11-01", "2026-11-30"), ("2026-12-01", "2026-12-31"))
MONTHS = [(2026, 11), (2026, 12)]


def _get(evs, kind):
    return [e for e in evs if e["kind"] == kind]


def test_cn_2026_11_12():
    evs = expiry.cn_events(MONTHS, TD)
    idx = _get(evs, "index")
    assert [(e["date"], e["quarterly"]) for e in idx] == [("20261120", False), ("20261218", True)]
    assert all(e["market"] == "A股" and e["name"] == "股指期货、期权交割日" for e in idx)
    etf = _get(evs, "etf_option")
    assert [e["date"] for e in etf] == ["20261125", "20261223"]
    assert all(e["name"] == "ETF 期权到期日" and e["quarterly"] is False for e in etf)


def test_cn_shift_holiday():
    td = [d for d in TD if d != "20261120"]
    idx = _get(expiry.cn_events([(2026, 11)], td), "index")
    assert idx[0]["date"] == "20261123"


def test_us_2026_11_12():
    evs = expiry.us_events(MONTHS, set(expiry.US_HOLIDAYS))
    assert [(e["date"], e["name"], e["quarterly"]) for e in evs] == [
        ("20261120", "月度期权到期日", False),
        ("20261218", "四巫日（股指期货、期权同日到期）", True),
    ]
    assert all(e["market"] == "美股" and e["kind"] == "us" for e in evs)


def test_us_holiday_moves_earlier():
    assert "20270618" in expiry.US_HOLIDAYS
    evs = expiry.us_events([(2027, 6)], set(expiry.US_HOLIDAYS))
    assert evs[0]["date"] == "20270617"


def test_reminders_window():
    evs = expiry.cn_events(MONTHS, TD) + expiry.us_events(MONTHS, set(expiry.US_HOLIDAYS))
    nxt = ["20261118", "20261119", "20261120", "20261123", "20261124"]
    r = expiry.reminders(evs, "20261117", nxt)
    ds = [e["date"] for e in r["upcoming"]]
    assert ds.count("20261120") == 2
    assert "20261125" not in ds
    assert ds == sorted(ds)
    assert r["today"] == []
    r2 = expiry.reminders(evs, "20261120", ["20261123", "20261124", "20261125", "20261126", "20261127"])
    assert len(r2["today"]) == 2
