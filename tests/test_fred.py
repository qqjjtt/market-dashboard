"""FredSource 单元测试：注入假 fetch_text，不触网。"""
import logging
import math

import pandas as pd

from fred import FredSource


def test_fred_parses_dot_as_nan():
    s = FredSource(lambda url: "observation_date,WALCL\n2026-10-28,6745635\n2026-11-04,.\n").series("WALCL", "2026-09-01")
    assert s.iloc[0] == 6745635 and math.isnan(s.iloc[1])
    assert s.index[0] == pd.Timestamp("2026-10-28") and s.index.name is None
    assert s.index[1] == pd.Timestamp("2026-11-04")


def test_fred_accepts_DATE_header():
    s = FredSource(lambda url: "DATE,DFF\n2026-11-03,3.88\n2026-11-04,3.87\n").series("DFF", "2026-09-01")
    assert list(s.values) == [3.88, 3.87]


def test_fred_url_contains_id_and_start():
    urls = []
    FredSource(lambda url: urls.append(url) or "DATE,WALCL\n2026-10-28,1\n").series("WALCL", "2026-09-01")
    assert urls == ["https://fred.stlouisfed.org/graph/fredgraph.csv?id=WALCL&cosd=2026-09-01"]


def test_fred_non_csv_returns_none(caplog):
    with caplog.at_level(logging.WARNING):
        assert FredSource(lambda url: "<html>blocked</html>").series("WALCL", "2026-09-01") is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_fred_exception_returns_none(caplog):
    def boom(url):
        raise TimeoutError("timed out")
    with caplog.at_level(logging.WARNING):
        assert FredSource(boom).series("WALCL", "2026-09-01") is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_fred_header_only_returns_none():
    assert FredSource(lambda url: "DATE,WALCL\n").series("WALCL", "2026-09-01") is None


def test_fred_garbage_values_become_nan():
    s = FredSource(lambda url: "DATE,X\n2026-11-03,abc\n2026-11-04,2\n").series("X", "2026-09-01")
    assert math.isnan(s.iloc[0]) and s.iloc[1] == 2


API_JSON = '{"observations": [{"date": "2026-10-28", "value": "6745635"}, {"date": "2026-11-04", "value": "."}]}'


def test_fred_api_key_uses_official_api_json():
    urls = []
    s = FredSource(lambda url: urls.append(url) or API_JSON, api_key="k123").series("WALCL", "2026-09-01")
    assert urls == ["https://api.stlouisfed.org/fred/series/observations"
                    "?series_id=WALCL&observation_start=2026-09-01&api_key=k123&file_type=json"]
    assert s.iloc[0] == 6745635 and math.isnan(s.iloc[1])
    assert s.index[1] == pd.Timestamp("2026-11-04") and s.name == "WALCL"


def test_fred_api_error_returns_none_without_leaking_key(caplog):
    def boom(url):
        raise RuntimeError(f"HTTP 400 for {url}")
    with caplog.at_level(logging.WARNING):
        assert FredSource(boom, api_key="k123").series("WALCL", "2026-09-01") is None
    assert caplog.records and all("k123" not in r.getMessage() for r in caplog.records)


def test_fred_api_error_json_returns_none(caplog):
    body = '{"error_code": 400, "error_message": "Bad Request.  The value for variable api_key is not registered."}'
    with caplog.at_level(logging.WARNING):
        assert FredSource(lambda url: body, api_key="bad").series("WALCL", "2026-09-01") is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_fred_from_env(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "envkey")
    assert FredSource.from_env().api_key == "envkey"
    monkeypatch.delenv("FRED_API_KEY")
    assert FredSource.from_env().api_key is None


def test_fred_failure_warning_names_the_path(caplog):
    def boom(url):
        raise TimeoutError("timed out")
    with caplog.at_level(logging.WARNING):
        FredSource(boom).series("WALCL", "2026-09-01")
        FredSource(boom, api_key="k123").series("WALCL", "2026-09-01")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("公开 CSV" in m for m in msgs) and any("官方 API" in m for m in msgs)
    assert all("k123" not in m for m in msgs)


def test_release_dates_parses_json():
    urls = []

    def fetch(url):
        urls.append(url)
        return '{"release_dates":[{"release_id":10,"date":"2026-12-10"},{"release_id":10,"date":"2026-11-13"},{"release_id":10,"date":"2026-11-13"}]}'
    assert FredSource(fetch, api_key="K").release_dates(10, "2026-11-08") == ["20261113", "20261210"]
    assert "include_release_dates_with_no_data=true" in urls[0]
    assert "release_id=10" in urls[0] and "realtime_start=2026-11-08" in urls[0]


def test_release_dates_without_key_returns_none():
    calls = []
    assert FredSource(lambda url: calls.append(url) or "{}").release_dates(10, "2026-11-08") is None
    assert calls == []


def test_release_dates_error_does_not_leak_key(caplog):
    def boom(url):
        raise RuntimeError("failed " + url)
    with caplog.at_level(logging.WARNING):
        assert FredSource(boom, api_key="SECRETKEY").release_dates(10, "2026-11-08") is None
    assert caplog.records and "SECRETKEY" not in caplog.text


def test_release_dates_empty_returns_none():
    assert FredSource(lambda u: '{"release_dates":[]}', api_key="K").release_dates(10, "2026-11-08") is None
