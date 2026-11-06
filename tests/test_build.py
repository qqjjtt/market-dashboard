import json
import re
from datetime import datetime, timezone

import pandas as pd

import build
from build import main, resolve_date
from demo import DemoFred, DemoSource

DATE = "20261106"


def extract_data_json(html):
    m = re.search(r"const DATA = ([^\n]*);\n", html)
    assert m, "找不到嵌入的 DATA"
    return m.group(1)


def run(tmp_path, src=None, fred=None, monkeypatch=None, extra=()):
    if monkeypatch is not None:
        monkeypatch.setattr(build, "_make_sources", lambda demo: (src or DemoSource(), fred or DemoFred()))
    out = tmp_path / "i.html"
    assert main(["--demo", "--out", str(out), "--date", DATE, *extra]) == 0
    html = out.read_text(encoding="utf-8")
    return html, json.loads(extract_data_json(html))


def test_resolve_date_uses_shanghai():
    assert resolve_date(None, datetime(2026, 11, 6, 17, 0, tzinfo=timezone.utc)) == "20261107"
    assert resolve_date("20261101") == "20261101"


def test_build_demo_end_to_end(tmp_path):
    html, data = run(tmp_path)
    assert "/*__DATA__*/null" not in html
    assert "当前为随机模拟数据" in html
    assert data["meta"]["demo"] is True and len(data["conclusions"]) == 4
    assert set(data) >= {"meta", "conclusions", "indices", "liquidity", "policy_ops", "news", "sector_flow",
                         "etf_flow", "round", "monthly", "fingerprint", "temperature", "concentration"}
    assert data["meta"]["trade_date"] == DATE
    assert data["meta"]["tail_source"] == "stk_mins·510300" and data["fingerprint"]["tail"]["today"] is not None
    assert data["meta"]["sector_mode"] in ("申万行业", "stock_basic.industry")
    assert len(data["indices"]) == 5
    liq = data["liquidity"]
    assert [c["key"] for c in liq["domestic"]] == ["turnover", "margin", "main_flow", "limit", "inst"]
    assert [c["key"] for c in liq["macro"]] == ["shibor_on", "shibor_1w", "m2", "bond_fut", "brent"]
    assert [c["key"] for c in liq["external"]] == ["fed_net", "reserves", "us2y", "us10y", "usd",
                                                    "spx", "ixic", "dji"]
    assert liq["vote"]["label"] in ("偏松", "中性", "偏紧") and len(liq["vote"]["signals"]) == 4
    assert liq["rate_expect"]["label"] != "无数据"
    assert all(c["value"] is not None for k in ("domestic", "macro", "external") for c in liq[k])
    assert data["round"]["count"] == 29 and data["round"]["start"] <= DATE
    assert len(data["monthly"]) == 6
    assert data["sector_flow"]["date"] == DATE and data["sector_flow"]["day"]["inflow"]
    assert len(data["etf_flow"]["dates"]) == len(data["etf_flow"]["daily"]) == len(data["etf_flow"]["cum"])
    assert len(data["etf_flow"]["per_etf"]) == 11
    usd = next(c for c in liq["external"] if c["key"] == "usd")
    assert "FXCM" in usd["note"] and "DXY" in usd["note"]  # 12759 这类点位不是 DXY，须在卡片上写明
    assert len(data["fingerprint"]["groups"]) == 4
    assert data["temperature"]["series"] and data["temperature"]["kpis"]["today"] is not None
    assert data["concentration"]["date"] == DATE and len(data["concentration"]["rows"]) == 14
    assert data["news"]["domestic"] and data["news"]["external"]
    assert isinstance(data["meta"]["warnings"], list)
    assert "TUSHARE_TOKEN" not in html


class NotReadySource(DemoSource):
    """D 当天 moneyflow 与板块资金流向尚未出。"""
    def moneyflow(self, trade_date):
        if trade_date == DATE:
            return super().moneyflow(trade_date).iloc[0:0]
        return super().moneyflow(trade_date)

    def sector_flow(self, start, end):
        df = super().sector_flow(start, end)
        return df[df["trade_date"] != DATE].reset_index(drop=True)


def test_build_when_moneyflow_not_ready(tmp_path, monkeypatch):
    _, data = run(tmp_path, NotReadySource(), monkeypatch=monkeypatch)
    assert data["concentration"]["date"] != data["meta"]["trade_date"]
    assert data["concentration"]["rows"] and data["concentration"]["market"] is not None
    assert data["sector_flow"]["date"] != DATE and data["sector_flow"]["day"]["inflow"]
    ws = data["meta"]["warnings"]
    assert any("数据截至" in w and "资金流向" in w for w in ws)
    assert any("数据截至" in w and "板块" in w for w in ws)
    assert len(data["conclusions"]) == 4


class NotReadyDailySource(DemoSource):
    """D 当天日线未出：交易日回退到上一个开市日。"""
    def stock_daily(self, trade_date):
        if trade_date == DATE:
            return super().stock_daily(trade_date).iloc[0:0]
        return super().stock_daily(trade_date)


def test_trade_date_falls_back_when_daily_empty(tmp_path, monkeypatch):
    _, data = run(tmp_path, NotReadyDailySource(), monkeypatch=monkeypatch)
    assert data["meta"]["trade_date"] == "20261105"


class MissingEtfSource(DemoSource):
    def etf_share(self, code, start, end):
        if code == "159919.SZ":
            return super().etf_share(code, start, end).iloc[0:0]
        return super().etf_share(code, start, end)


def test_build_skips_missing_etf(tmp_path, monkeypatch):
    _, data = run(tmp_path, MissingEtfSource(), monkeypatch=monkeypatch)
    assert data["round"]["count"] == 28 and any("159919.SZ" in w for w in data["meta"]["warnings"])


class DownFred(DemoFred):
    def series(self, series_id, start):
        return None


def test_build_fred_down(tmp_path, monkeypatch):
    _, data = run(tmp_path, fred=DownFred(), monkeypatch=monkeypatch)
    ext = {c["key"]: c for c in data["liquidity"]["external"]}
    assert ext["fed_net"]["value"] is None and ext["reserves"]["value"] is None
    assert ext["us2y"]["value"] is not None
    assert data["liquidity"]["rate_expect"]["label"] == "无数据"
    assert any("FRED" in w for w in data["meta"]["warnings"])
    assert len(data["conclusions"]) == 4


class NoMinutesSource(DemoSource):
    def etf_minutes(self, code, start, end):
        return None


def test_build_no_minutes(tmp_path, monkeypatch):
    _, data = run(tmp_path, NoMinutesSource(), monkeypatch=monkeypatch)
    t = data["fingerprint"]["tail"]
    assert t["today"] is None and t["yday"] is None and t["source"] == "无分钟数据"
    assert data["meta"]["tail_source"] == "无分钟数据"


class CountingMinutes(DemoSource):
    calls = 0

    def etf_minutes(self, code, start, end):
        type(self).calls += 1
        return None


def test_minutes_stops_after_first_none(tmp_path, monkeypatch):
    CountingMinutes.calls = 0
    run(tmp_path, CountingMinutes(), monkeypatch=monkeypatch)
    assert CountingMinutes.calls == 1


class RecordingMinutes(DemoSource):
    seen = []

    def etf_minutes(self, code, start, end):
        type(self).seen.append((code, start, end))
        return super().etf_minutes(code, start, end)


def test_tail_uses_single_representative_etf_one_call(tmp_path, monkeypatch):
    """stk_mins 限额 1 次/小时：每次构建只调一次，代码取 config.TAIL_CODE，区间 yday→D。"""
    RecordingMinutes.seen = []
    _, data = run(tmp_path, RecordingMinutes(), monkeypatch=monkeypatch)
    assert len(RecordingMinutes.seen) == 1
    code, start, end = RecordingMinutes.seen[0]
    assert code == "510300.SH" and end == DATE and start < end
    t = data["fingerprint"]["tail"]
    assert t["today"] is not None and t["source"] == "stk_mins·510300"


class BrokenSource(DemoSource):
    def margin(self, start, end):
        raise RuntimeError("boom token=SECRET123")

    def sector_flow(self, start, end):
        raise RuntimeError("down")


def test_single_source_failure_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "SECRET123")
    html, data = run(tmp_path, BrokenSource(), monkeypatch=monkeypatch)
    dom = {c["key"]: c for c in data["liquidity"]["domestic"]}
    assert dom["margin"]["value"] is None and dom["turnover"]["value"] is not None
    assert data["sector_flow"]["day"] == {"inflow": [], "outflow": []}
    assert len(data["conclusions"]) == 4
    assert "SECRET123" not in html


def test_round_start_arg(tmp_path):
    _, data = run(tmp_path, extra=("--round-start", "20261102"))
    assert data["round"]["start"] == "20261102"


class GrowingShares(DemoSource):
    """份额逐日增长 -> 本轮净申购为正。"""
    def etf_share(self, code, start, end):
        days = [d for d in self.trade_days(end, 400) if start <= d <= end]
        return pd.DataFrame({"trade_date": days, "share": [100 + i * 0.5 for i in range(len(days))]})


def test_round_attribution_only_when_total_positive(tmp_path, monkeypatch):
    _, data = run(tmp_path, GrowingShares(), monkeypatch=monkeypatch)
    r = data["round"]
    assert r["total"] > 0 and r["low"] < r["mid"] < r["high"]
    assert data["conclusions"][2]["tone"] == "red" and "仍在" in data["conclusions"][2]["text"]


def test_round_attribution_none_when_total_not_positive(tmp_path):
    _, data = run(tmp_path)  # 默认 demo 种子下本轮累计为负
    r = data["round"]
    assert r["total"] is not None and r["total"] <= 0
    assert r["low"] is None and r["high"] is None and r["mid"] is None


class ExactLimits(DemoSource):
    calls = []

    def limit_counts(self, trade_date):
        type(self).calls.append(trade_date)
        return {"up": 32, "down": 15}


def test_temperature_limit_counts_use_exact_values(tmp_path, monkeypatch):
    """体温面板展示的涨停/跌停与流动性卡片同为 limit_list_d 精确值；每个交易日只调用一次。"""
    ExactLimits.calls = []
    _, data = run(tmp_path, ExactLimits(), monkeypatch=monkeypatch)
    t = data["temperature"]
    assert t["limit_up"] == 32 and t["limit_down"] == 15
    card = next(c for c in data["liquidity"]["domestic"] if c["key"] == "limit")
    assert card["value"] == 32 and card["note"] == "跌停 15"
    assert ExactLimits.calls.count(DATE) == 1


class NoExactLimits(DemoSource):
    def limit_counts(self, trade_date):
        return {"up": None, "down": None}


def test_temperature_limit_counts_fallback_to_approx_with_warning(tmp_path, monkeypatch):
    _, data = run(tmp_path, NoExactLimits(), monkeypatch=monkeypatch)
    t = data["temperature"]
    assert isinstance(t["limit_up"], int) and isinstance(t["limit_down"], int)
    assert any("涨跌停精确家数缺失" in w for w in data["meta"]["warnings"])
