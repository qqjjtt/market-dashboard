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
