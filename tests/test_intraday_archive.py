import json
from datetime import datetime, timezone

import pytest

from intraday_archive import archive_intraday, publish_intraday
from intraday_tracker import MINUTE_GRID


DAY = "20261127"
ISO = "2026-11-27"
CLOSE = datetime(2026, 11, 27, 7, tzinfo=timezone.utc)  # 15:00 Shanghai


def _chart():
    n = len(MINUTE_GRID)
    return {
        "meta": {"trade_date": DAY, "sampled_at": "15:00", "points_count": n, "total_grid": n},
        "grid": MINUTE_GRID,
        "volume": {"date": DAY, "today_cum_yi": [0.1] * n},
        "sectors": [{"trade_date": DAY, "has_close_bar": True, "pct_curve": [0.0] * n,
                     "flow_curve": [0.0] * n, "amt_curve": [0.1] * n, "leaders": []}],
    }


def _payload():
    return {"meta": {"trade_date": DAY, "demo": False}, "intraday": _chart()}


def test_archive_and_publish_keep_prior_files(tmp_path):
    history, out = tmp_path / "history", tmp_path / "site"
    path = archive_intraday(_payload(), history, requested_date=DAY, now=CLOSE)
    assert path == history / "intraday" / f"{ISO}.json"
    assert json.loads(path.read_text()) == _chart()
    original = path.read_bytes()
    previous = path.with_name("2026-11-26.json")
    previous.write_bytes(b'{"old":true}')
    path.with_name("2026-02-30.json").write_text("{}")
    modified = _payload()
    modified["intraday"]["sectors"][0]["pct_curve"][-1] = 1.0
    assert archive_intraday(modified, history, requested_date=DAY, now=CLOSE) == path
    assert path.read_bytes() == original

    publish_intraday(history, out)
    target = out / "history" / "intraday"
    assert (target / path.name).read_bytes() == original
    assert (target / previous.name).read_bytes() == b'{"old":true}'
    assert not (target / "2026-02-30.json").exists()
    for directory in (target, path.parent):
        assert json.loads((directory / "manifest.json").read_text()) == {"dates": [ISO, "2026-11-26"]}


@pytest.mark.parametrize("problem", ["before-close", "historical-request", "fallback", "stale-chart", "demo",
                                     "short-chart", "missing-etf-close", "stale-etf", "stale-volume", "short-volume",
                                     "stale-leader", "missing-chart"])
def test_skip_ineligible_chart(tmp_path, problem):
    payload = _payload()
    chart = payload["intraday"]
    requested, now = DAY, CLOSE
    if problem == "before-close":
        now = CLOSE.replace(hour=6, minute=59)
    elif problem == "historical-request":
        requested = "20261126"
    elif problem == "fallback":
        payload["meta"]["trade_date"] = "20261126"
    elif problem == "stale-chart":
        chart["meta"]["trade_date"] = "20261126"
    elif problem == "demo":
        payload["meta"]["demo"] = True
    elif problem == "short-chart":
        chart["meta"]["points_count"] -= 1
    elif problem == "missing-etf-close":
        chart["sectors"][0]["has_close_bar"] = False
    elif problem == "stale-etf":
        chart["sectors"][0]["trade_date"] = "20261126"
    elif problem == "stale-volume":
        chart["volume"]["date"] = "20261126"
    elif problem == "short-volume":
        chart["volume"]["today_cum_yi"].pop()
    elif problem == "stale-leader":
        chart["sectors"][0]["leaders"] = [dict(chart["sectors"][0], trade_date="20261126")]
    elif problem == "missing-chart":
        payload["intraday"] = None
    assert archive_intraday(payload, tmp_path, requested_date=requested, now=now) is None
    assert not (tmp_path / "intraday").exists()


def test_publish_empty_archive(tmp_path):
    history, out = tmp_path / "history", tmp_path / "site"
    publish_intraday(history, out)
    assert json.loads((out / "history" / "intraday" / "manifest.json").read_text()) == {"dates": []}
