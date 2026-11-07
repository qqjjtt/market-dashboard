import json
import logging
import re
from datetime import datetime, timezone

import pandas as pd
import pytest

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
    com = data["commodities"]
    assert [(c["key"], c["unit"]) for c in com] == [("gold", "元/克"), ("oil", "元/桶")]
    assert all(c["close"] is not None and c["pct_chg"] is not None and c["date"] == DATE for c in com)
    liq = data["liquidity"]
    assert [c["key"] for c in liq["domestic"]] == ["turnover", "margin", "main_flow", "limit", "inst"]
    assert [c["key"] for c in liq["macro"]] == ["shibor_on", "shibor_1w", "m2", "bond_fut", "brent"]
    assert [c["key"] for c in liq["external"]] == ["fed_net", "reserves", "us2y", "us10y", "usd",
                                                    "spx", "ixic", "dji"]
    assert liq["vote"]["label"] in ("偏松", "中性", "偏紧") and len(liq["vote"]["signals"]) == 4
    assert liq["rate_expect"]["label"] != "无数据"
    assert all(c["value"] is not None for k in ("domestic", "macro", "external") for c in liq[k])
    assert data["round"]["count"] == 6 and data["round"]["start"] <= DATE
    assert len(data["monthly"]) == 6
    assert data["sector_flow"]["date"] == DATE and data["sector_flow"]["day"]["inflow"]
    assert len(data["etf_flow"]["dates"]) == len(data["etf_flow"]["daily"]) == len(data["etf_flow"]["cum"])
    assert len(data["etf_flow"]["per_etf"]) == 6 and data["meta"]["team_pool"] == "汇金重仓 6 只"
    usd = next(c for c in liq["external"] if c["key"] == "usd")
    assert "FXCM" in usd["note"] and "DXY" in usd["note"]  # 12759 这类点位不是 DXY，须在卡片上写明
    assert len(data["fingerprint"]["groups"]) == 4
    assert data["temperature"]["series"] and data["temperature"]["kpis"]["today"] is not None
    assert data["concentration"]["date"] == DATE and len(data["concentration"]["rows"]) == 14
    assert data["news"] == {"domestic": [], "external": [], "status": "disabled", "since": None}
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
    assert data["round"]["count"] == 5 and any("159919.SZ" in w for w in data["meta"]["warnings"])


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


# ---------------------------------------------------------------- C1：整体失败不得覆盖旧页面

class AllDownSource(DemoSource):
    """日线对每个候选日都为空（如 token 失效后每个接口都取不到）。"""
    def stock_daily(self, trade_date):
        raise RuntimeError("down")


class NoCalendarSource(DemoSource):
    def trade_days(self, end, n):
        raise RuntimeError("calendar down")


class EmptyCalendarSource(DemoSource):
    def trade_days(self, end, n):
        return []


class EmptyDailySource(DemoSource):
    def stock_daily(self, trade_date):
        return super().stock_daily(trade_date).iloc[0:0]


@pytest.mark.parametrize("cls", [AllDownSource, NoCalendarSource, EmptyCalendarSource, EmptyDailySource])
def test_total_failure_exits_3_and_writes_nothing(tmp_path, monkeypatch, caplog, cls):
    monkeypatch.setattr(build, "_make_sources", lambda demo: (cls(), DemoFred()))
    out = tmp_path / "site" / "i.html"
    with caplog.at_level(logging.ERROR, logger="build"):
        assert main(["--demo", "--out", str(out), "--date", DATE]) == 3
    assert not out.exists()
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_total_failure_keeps_previous_page(tmp_path, monkeypatch):
    out = tmp_path / "i.html"
    out.write_text("LAST GOOD PAGE", encoding="utf-8")
    monkeypatch.setattr(build, "_make_sources", lambda demo: (EmptyDailySource(), DemoFred()))
    assert main(["--demo", "--out", str(out), "--date", DATE]) == 3
    assert out.read_text(encoding="utf-8") == "LAST GOOD PAGE"


def test_partial_failure_still_exits_0(tmp_path, monkeypatch):
    run(tmp_path, BrokenSource(), monkeypatch=monkeypatch)  # run() 断言退出码 0


# ---------------------------------------------------------------- M1：回退日志级别

SH_TZ = build.SH


def _fallback_records(caplog, now):
    caplog.clear()
    daily = {}
    with caplog.at_level(logging.INFO, logger="build"):
        d = build._resolve_trade_day(NotReadyDailySource(), DATE, daily, now)
    return d, [r for r in caplog.records if "交易日回退" in r.getMessage()]


def test_fallback_morning_is_info(caplog):
    d, recs = _fallback_records(caplog, datetime(2026, 11, 6, 9, 30, tzinfo=SH_TZ))
    assert d == "20261105" and len(recs) == 1 and recs[0].levelno == logging.INFO


def test_fallback_after_17_is_warning(caplog):
    d, recs = _fallback_records(caplog, datetime(2026, 11, 6, 20, 30, tzinfo=SH_TZ))
    assert d == "20261105" and len(recs) == 1 and recs[0].levelno == logging.WARNING


def test_info_fallback_not_collected_as_warning(tmp_path, monkeypatch):
    now = datetime(2026, 11, 6, 9, 30, tzinfo=SH_TZ)
    payload = build.collect(NotReadyDailySource(), DemoFred(), DATE, None, True, now=now)
    assert payload["meta"]["trade_date"] == "20261105"
    assert not any("交易日回退" in w for w in payload["meta"]["warnings"])


# ---------------------------------------------------------------- I2：头条日期标注

def test_headline_stale_labels_and_six_dim(tmp_path, monkeypatch):
    _, data = run(tmp_path, NotReadySource(), monkeypatch=monkeypatch)
    cs = {c["key"]: c for c in data["conclusions"]}
    sd, cd = data["sector_flow"]["date"], data["concentration"]["date"]
    assert sd != DATE and cd != DATE
    assert f"截至 {int(sd[4:6])}/{int(sd[6:])}" in cs["sector"]["sub"]
    assert f"浓度截至 {int(cd[4:6])}/{int(cd[6:])}" in cs["retail"]["text"]
    assert data["temperature"]["six_dim"] is None  # 浓度不是 D 当日，不合成六维


def test_headline_same_day_has_no_stale_label_and_six_dim(tmp_path):
    _, data = run(tmp_path)
    cs = {c["key"]: c for c in data["conclusions"]}
    assert "截至" not in cs["sector"]["sub"] and "浓度截至" not in cs["retail"]["text"]
    assert data["temperature"]["six_dim"] is not None


# ---------------------------------------------------------------- I3：新闻覆盖

class FailedNews(DemoSource):
    def major_news(self, start_dt, end_dt, max_calls=None):
        return super().major_news(start_dt, end_dt).iloc[0:0]

    def last_news_coverage(self):
        return {"status": "failed", "since": None}


class PartialNews(DemoSource):
    def last_news_coverage(self):
        return {"status": "partial", "since": "2026-11-04 08:30:00"}


class RaisingNews(DemoSource):
    def major_news(self, start_dt, end_dt, max_calls=None):
        raise RuntimeError("boom")


class CountingNews(DemoSource):
    calls = 0

    def major_news(self, start_dt, end_dt, max_calls=None):
        CountingNews.calls += 1
        return super().major_news(start_dt, end_dt)


def test_news_disabled_skips_fetch_and_page_section(tmp_path, monkeypatch):
    monkeypatch.setattr(build.config, "NEWS_ENABLED", False)
    CountingNews.calls = 0
    _, data = run(tmp_path, CountingNews(), monkeypatch=monkeypatch)
    assert CountingNews.calls == 0  # 下线期间不消耗 major_news 配额
    assert data["news"]["status"] == "disabled"


def test_news_status_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(build.config, "NEWS_ENABLED", True)
    _, data = run(tmp_path)
    assert data["news"]["status"] == "ok" and data["news"]["since"] is None
    assert data["news"]["domestic"] and data["news"]["external"]


def test_news_status_failed_and_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(build.config, "NEWS_ENABLED", True)
    _, data = run(tmp_path, FailedNews(), monkeypatch=monkeypatch)
    assert data["news"]["status"] == "failed" and data["news"]["domestic"] == []
    _, data = run(tmp_path, PartialNews(), monkeypatch=monkeypatch)
    assert data["news"]["status"] == "partial" and data["news"]["since"] == "2026-11-04 08:30:00"
    assert data["news"]["domestic"]


def test_news_status_failed_when_source_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(build.config, "NEWS_ENABLED", True)
    _, data = run(tmp_path, RaisingNews(), monkeypatch=monkeypatch)
    assert data["news"]["status"] == "failed" and data["news"]["external"] == []


# ---------------------------------------------------------------- I4：尾盘 / 指纹失败不连坐

class MalformedMinutes(DemoSource):
    def etf_minutes(self, code, start, end):
        return pd.DataFrame({"foo": [1, 2], "amount": [1.0, 2.0]})  # 缺 trade_time 列


def test_tail_failure_does_not_wipe_national_team_block(tmp_path, monkeypatch):
    _, data = run(tmp_path, MalformedMinutes(), monkeypatch=monkeypatch)
    assert data["etf_flow"]["dates"] and data["round"]["count"] == 6 and len(data["monthly"]) == 6
    fp = data["fingerprint"]
    assert fp["tail"] == {"today": None, "yday": None, "source": "无分钟数据"}
    assert fp["groups"] and data["meta"]["tail_source"] == "无分钟数据"
    assert any("尾盘占比" in w for w in data["meta"]["warnings"])


def test_fingerprint_failure_keeps_flow_block(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad groups")
    monkeypatch.setattr(build.me, "amount_groups", boom)
    _, data = run(tmp_path, monkeypatch=monkeypatch)
    assert data["etf_flow"]["dates"] and data["round"]["count"] == 6 and len(data["monthly"]) == 6
    assert data["fingerprint"]["groups"] == [] and data["fingerprint"]["tail"]["source"] == "无分钟数据"
    assert any("成交指纹" in w for w in data["meta"]["warnings"])


# ---------------------------------------------------------------- M3 / M5 / M7 additive keys

def test_indices_carry_date(tmp_path):
    _, data = run(tmp_path)
    assert all(x["date"] == DATE for x in data["indices"])


def test_round_attribution_and_sector_days_in_payload(tmp_path):
    _, data = run(tmp_path)
    assert data["round"]["attribution"] == [0.7, 0.9]
    assert data["sector_flow"]["d5"]["days"] == 5 and data["sector_flow"]["day"]["days"] == 1


def test_scrub_masks_fred_api_key(monkeypatch):
    import build
    monkeypatch.setenv("TUSHARE_TOKEN", "SECRET123")
    monkeypatch.setenv("FRED_API_KEY", "FREDKEY456")
    assert build._scrub("a SECRET123 b FREDKEY456") == "a *** b ***"


# ---------------------------------------------------------------- 每日存档 --archive

def _real_run(tmp_path, monkeypatch, *extra):
    """非 demo 模式运行（数据源换成 DemoSource，不联网），返回退出码与输出页路径。"""
    monkeypatch.setattr(build, "_make_sources", lambda demo: (DemoSource(), DemoFred()))
    out = tmp_path / "site" / "i.html"
    return main(["--out", str(out), "--date", DATE, *extra]), out


def test_archive_writes_payload_named_by_trade_date(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "abcdef1234567890")
    code, out = _real_run(tmp_path, monkeypatch, "--archive", str(tmp_path / "history"))
    assert code == 0
    files = list((tmp_path / "history").glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    td = data["meta"]["trade_date"]
    assert files[0].name == f"{td[:4]}-{td[4:6]}-{td[6:]}.json"
    assert data["meta"]["demo"] is False and data["meta"]["code_version"] == "abcdef1"
    assert data == json.loads(extract_data_json(out.read_text(encoding="utf-8")))


def test_archive_overwrites_same_trade_date(tmp_path, monkeypatch):
    hist = tmp_path / "history"
    _real_run(tmp_path, monkeypatch, "--archive", str(hist))
    _real_run(tmp_path, monkeypatch, "--archive", str(hist))
    assert len(list(hist.glob("*.json"))) == 1
    assert not list(hist.glob("*.tmp"))


def test_code_version_none_outside_ci(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    _, data = run(tmp_path)
    assert data["meta"]["code_version"] is None


def test_demo_never_archived(tmp_path):
    out = tmp_path / "i.html"
    assert main(["--demo", "--out", str(out), "--date", DATE, "--archive", str(tmp_path / "history")]) == 0
    assert not (tmp_path / "history").exists()


def test_total_failure_archives_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "_make_sources", lambda demo: (EmptyDailySource(), DemoFred()))
    out = tmp_path / "i.html"
    assert main(["--out", str(out), "--date", DATE, "--archive", str(tmp_path / "history")]) == 3
    assert not (tmp_path / "history").exists()


# ---------------------------------------------------------------- 金价、油价

class NoCommoditySource(DemoSource):
    def gold_spot(self, start, end):
        raise RuntimeError("无权限")

    def crude_fut(self, start, end):
        return super().crude_fut(start, end).iloc[0:0]


def test_commodities_missing_degrades(tmp_path, monkeypatch):
    _, data = run(tmp_path, NoCommoditySource(), monkeypatch=monkeypatch)
    assert [c["key"] for c in data["commodities"]] == ["gold", "oil"]
    assert all(c["close"] is None and c["pct_chg"] is None and c["date"] is None for c in data["commodities"])
    assert any("黄金" in w for w in data["meta"]["warnings"])


class FlatGoldSource(DemoSource):
    def gold_spot(self, start, end):
        return pd.DataFrame({"trade_date": ["20261104", "20261105"], "close": [500.0, 510.0]})


def test_commodity_change_from_previous_close(tmp_path, monkeypatch):
    _, data = run(tmp_path, FlatGoldSource(), monkeypatch=monkeypatch)
    gold = data["commodities"][0]
    assert gold["close"] == 510.0 and gold["date"] == "20261105" and gold["pct_chg"] == pytest.approx(2.0)
