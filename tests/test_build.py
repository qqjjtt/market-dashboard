import json
import logging
import re
from datetime import datetime, timezone

import pandas as pd
import pytest

import build
import config
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
    assert "commodities" not in data
    assert [a["key"] for a in data["assets"]] == ["gold", "oil", "bond", "us10y", "usd", "cnh"]
    assert all(a["value"] is not None and a["w1"] is not None and a["w4"] is not None and a["w13"] is not None
               for a in data["assets"])
    liq = data["liquidity"]
    assert [c["key"] for c in liq["domestic"]] == ["turnover", "margin", "main_flow", "limit", "inst"]
    assert [c["key"] for c in liq["macro"]] == [
        "shibor_on", "shibor_1w", "dr007", "dr001_spread", "r007_dr007", "shibor_1y", "m1", "m2", "sf",
        "bond_fut", "brent"]
    assert [c["key"] for c in liq["external"]] == [
        "fed_net", "reserves", "tga", "rrp", "sofr_iorb", "nfci", "hy_oas", "us2y", "us10y", "tips10", "usd",
        "spx", "ixic", "dji"]
    mac = {c["key"]: c for c in liq["macro"]}
    assert mac["dr001_spread"]["unit"] == "bp" and "隔夜逆回购 1.25%" in mac["dr001_spread"]["note"]
    assert mac["r007_dr007"]["unit"] == "bp" and mac["shibor_1y"]["note"] == "同业存单参考"
    assert 0 < mac["sf"]["value"] < 30  # 同比 %，demo 月增约 0.6% → 年化约 7%
    ext = {c["key"]: c for c in liq["external"]}
    assert ext["sofr_iorb"]["unit"] == "bp" and ext["nfci"]["note"] == "负值=偏宽松"
    assert "不另计分" in ext["tga"]["note"] and "不另计分" in ext["rrp"]["note"]
    assert liq["vote"]["label"] in ("偏松", "中性", "偏紧") and len(liq["vote"]["signals"]) == 4
    assert liq["rate_expect"]["label"] != "无数据"
    assert all(c["value"] is not None for k in ("domestic", "macro", "external") for c in liq[k])
    assert data["round"]["count"] == 7 and data["round"]["start"] <= DATE
    assert len(data["monthly"]) == 6
    assert data["sector_flow"]["date"] == DATE and data["sector_flow"]["day"]["inflow"]
    assert len(data["etf_flow"]["dates"]) == len(data["etf_flow"]["daily"]) == len(data["etf_flow"]["cum"])
    assert len(data["etf_flow"]["per_etf"]) == 7 and data["meta"]["team_pool"] == "汇金重仓 7 只"
    usd = next(c for c in liq["external"] if c["key"] == "usd")
    assert "FXCM" in usd["note"] and "DXY" in usd["note"]  # 12759 这类点位不是 DXY，须在卡片上写明
    assert len(data["fingerprint"]["groups"]) == 4
    assert data["temperature"]["series"] and data["temperature"]["kpis"]["today"] is not None
    assert data["concentration"]["date"] == DATE and len(data["concentration"]["rows"]) == 20
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
    assert data["round"]["count"] == 6 and any("159919.SZ" in w for w in data["meta"]["warnings"])


def test_holdings_payload(tmp_path):
    _, data = run(tmp_path)
    h = data["holdings"]
    assert h["base_date"] == "20251231"
    assert [r["code"] for r in h["rows"]] == config.HUIJIN_ETF
    assert all(r["est"] is not None and r["value"] is not None for r in h["rows"])
    assert isinstance(h["total"], float)
    assert h["total"] == pytest.approx(sum(r["value"] for r in h["rows"]), abs=0.5)
    assert h["date"] is not None and h["prev_total"] is not None
    assert h["change"] == pytest.approx(h["total"] - h["prev_total"], abs=0.2)


def test_holdings_missing_etf(tmp_path, monkeypatch):
    _, data = run(tmp_path, MissingEtfSource(), monkeypatch=monkeypatch)
    rows = {r["code"]: r for r in data["holdings"]["rows"]}
    assert rows["159919.SZ"]["est"] is None and rows["159919.SZ"]["value"] is None
    assert rows["159919.SZ"]["base"] == 354.98
    assert isinstance(data["holdings"]["total"], float)


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
    assert data["etf_flow"]["dates"] and data["round"]["count"] == 7 and len(data["monthly"]) == 6
    fp = data["fingerprint"]
    assert fp["tail"] == {"today": None, "yday": None, "source": "无分钟数据"}
    assert fp["groups"] and data["meta"]["tail_source"] == "无分钟数据"
    assert any("尾盘占比" in w for w in data["meta"]["warnings"])


def test_fingerprint_failure_keeps_flow_block(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad groups")
    monkeypatch.setattr(build.me, "amount_groups", boom)
    _, data = run(tmp_path, monkeypatch=monkeypatch)
    assert data["etf_flow"]["dates"] and data["round"]["count"] == 7 and len(data["monthly"]) == 6
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


# ---------------------------------------------------------------- 大类资产、资金面新指标

class NoCommoditySource(DemoSource):
    def gold_spot(self, start, end):
        raise RuntimeError("无权限")

    def crude_fut(self, start, end):
        return super().crude_fut(start, end).iloc[0:0]


def test_assets_missing_degrades(tmp_path, monkeypatch):
    _, data = run(tmp_path, NoCommoditySource(), monkeypatch=monkeypatch)
    by = {a["key"]: a for a in data["assets"]}
    assert list(by) == ["gold", "oil", "bond", "us10y", "usd", "cnh"]
    for k in ("gold", "oil"):
        assert all(by[k][f] is None for f in ("value", "date", "w1", "w4", "w13"))
    assert all(by[k]["value"] is not None for k in ("bond", "us10y", "usd", "cnh"))
    assert any("黄金" in w for w in data["meta"]["warnings"])


def test_assets_fields_and_kinds(tmp_path):
    _, data = run(tmp_path)
    by = {a["key"]: a for a in data["assets"]}
    assert {k: (a["unit"], a["chg_kind"]) for k, a in by.items()} == {
        "gold": ("元/克", "pct"), "oil": ("元/桶", "pct"), "bond": ("元", "pct"),
        "us10y": ("%", "bp"), "usd": ("点", "pct"), "cnh": ("元/美元", "pct")}
    assert by["bond"]["note"] == "价格涨=收益率降" and by["cnh"]["note"] == "数值涨=人民币贬"
    assert all(a["date"] == DATE for a in data["assets"])


class FlatGoldSource(DemoSource):
    def gold_spot(self, start, end):
        return pd.DataFrame({"trade_date": ["20261030", "20261105", "20261106", "20261109"],
                             "close": [400.0, 500.0, 510.0, 999.0]})


def test_assets_only_uses_points_up_to_trade_date(tmp_path, monkeypatch):
    _, data = run(tmp_path, FlatGoldSource(), monkeypatch=monkeypatch)
    gold = data["assets"][0]
    assert gold["value"] == 510.0 and gold["date"] == "20261106"
    assert gold["w1"] is None  # 仅 3 个 <= D 的观测，不足 6 个


def test_dr001_spread_follows_policy_rate_effective_date(tmp_path):
    _, d1 = run(tmp_path)
    c1 = next(c for c in d1["liquidity"]["macro"] if c["key"] == "dr001_spread")
    assert c1["value"] is not None and c1["date"] == DATE
    out = tmp_path / "early.html"
    assert main(["--demo", "--out", str(out), "--date", "20261030"]) == 0
    d0 = json.loads(extract_data_json(out.read_text(encoding="utf-8")))
    c0 = next(c for c in d0["liquidity"]["macro"] if c["key"] == "dr001_spread")
    # 政策利率表现在从 2024-07-22 起有锚：11-02 换锚前按当时的 7 天逆回购 1.40% 计算，换锚后按 1.25%
    assert c0["value"] is not None and c0["date"] == "20261030"
    assert next(c for c in d0["liquidity"]["macro"] if c["key"] == "dr007")["value"] is not None


class NoRepoSource(DemoSource):
    def repo(self, code, start, end):
        raise RuntimeError("无权限")


def test_repo_missing_degrades(tmp_path, monkeypatch):
    _, data = run(tmp_path, NoRepoSource(), monkeypatch=monkeypatch)
    mac = {c["key"]: c for c in data["liquidity"]["macro"]}
    assert all(mac[k]["value"] is None for k in ("dr007", "dr001_spread", "r007_dr007"))
    assert mac["shibor_1y"]["value"] is not None
    assert any("DR007" in w for w in data["meta"]["warnings"])


# ---------------------------------------------------------------- 观察池天量

def test_watch_pool_in_payload(tmp_path):
    _, data = run(tmp_path)
    w = data["watch"]
    assert w["count"] == 24 and w["date"] == DATE and isinstance(w["spikes"], list)
    assert "511380.SH" in config.WATCH_ETF and not set(config.WATCH_ETF) & set(config.HUIJIN_ETF)


class SpikeSource(DemoSource):
    def etf_daily(self, code, start, end):
        df = super().etf_daily(code, start, end)
        if code == "588000.SH" and len(df):
            df.loc[df["trade_date"] == DATE, "amount"] = df["amount"].mean() * 10
        return df


def test_watch_spike_reaches_page_and_team_card(tmp_path, monkeypatch):
    _, data = run(tmp_path, SpikeSource(), monkeypatch=monkeypatch)
    codes = [s["code"] for s in data["watch"]["spikes"]]
    assert "588000.SH" in codes
    team = next(c for c in data["conclusions"] if c["key"] == "team")
    assert "观察池天量" in team["sub"]
    assert team["text"].startswith(("仍在", "放缓", "疑似撤出"))  # 判断不受观察池影响


ZONES = {z[2] for z in config.GAUGE_ZONES}


def test_liquidity_index_payload(tmp_path):
    _, data = run(tmp_path)
    li = data["liquidity_index"]
    assert isinstance(li["value"], int) and 0 <= li["value"] <= 100 and li["zone"] in ZONES
    assert len(li["components"]) == 11
    comp = {c["key"]: c for c in li["components"]}
    for k in ("gold", "sc_oil"):
        assert comp[k]["scored"] is False and comp[k]["score"] is None
    assert comp["us10y"]["scored"] is True and isinstance(comp["us10y"]["score"], int)
    assert comp["us10y"]["chg5_unit"] == "bp" and comp["usd"]["chg5_unit"] == "%"
    assert "隔夜逆回购 1.25%" in comp["dr001_spread"]["hint"]
    assert len(li["combos"]) == 3 and all(c["of"] == 3 for c in li["combos"])
    assert set(li["ago"]) == {"prev", "w1", "m1", "y1"} and all(v is not None for v in li["ago"].values())
    assert len(li["history"]) >= 240 and li["history"][-1]["date"] == DATE
    assert li["gate"]["ok"] in (True, False) and isinstance(li["gate"]["notes"], list)
    assert li["text"].startswith(li["zone"]) and li["reason"] in li["text"]
    assert data["meta"]["warnings"] == []


def test_gauge_fred_down(tmp_path, monkeypatch):
    _, data = run(tmp_path, fred=DownFred(), monkeypatch=monkeypatch)
    li = data["liquidity_index"]
    assert li["value"] is not None and li["reason"].endswith("（外部缺 2 项）")
    assert li["gate"]["ok"] is True or li["gate"]["notes"]


def test_calendar_payload(tmp_path):
    _, data = run(tmp_path, extra=())
    assert set(data["calendar"]) == {"today", "upcoming"}


def _cal(tmp_path, monkeypatch, date):
    monkeypatch.setattr(build, "_make_sources", lambda demo: (DemoSource(), DemoFred()))
    out = tmp_path / "c.html"
    assert main(["--demo", "--out", str(out), "--date", date]) == 0
    return json.loads(extract_data_json(out.read_text(encoding="utf-8")))["calendar"]


def test_calendar_upcoming_and_today(tmp_path, monkeypatch):
    cal = _cal(tmp_path, monkeypatch, "20261117")
    on_20 = [e for e in cal["upcoming"] if e["date"] == "20261120"]
    assert {e["market"] for e in on_20} == {"A股", "美股"} and cal["today"] == []
    assert not any(e["date"] == "20261125" for e in cal["upcoming"])
    cal = _cal(tmp_path, monkeypatch, "20261120")
    assert len(cal["today"]) == 2


def test_liquidity_conclusion_from_index(tmp_path):
    _, data = run(tmp_path)
    li = data["liquidity_index"]
    card = next(c for c in data["conclusions"] if c["key"] == "liquidity")
    assert card["text"].startswith("流动性") and f"（{li['value']}）" in card["text"]
    assert card["sub"] == li["reason"]


def test_gauge_no_duplicate_fetches(tmp_path, monkeypatch):
    calls = []

    class Spy(DemoSource):
        def repo(self, code, start, end):
            calls.append(("repo", code, start))
            return super().repo(code, start, end)

        def fut_mapping(self, code, start, end):
            calls.append(("fut_mapping", code, start))
            return super().fut_mapping(code, start, end)

    run(tmp_path, src=Spy(), monkeypatch=monkeypatch)
    assert len(calls) == len(set(calls))
    assert {c[:2] for c in calls if c[0] == "fut_mapping"} == {("fut_mapping", "T.CFX"), ("fut_mapping", "SC.INE")}


def test_holdings_update_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HUIJIN_UPDATED", {"date": "20261102", "source": "2026 年第 3 季度报告"})
    _, data = run(tmp_path)  # DATE = 20261106，更新后第 4 天
    up = data["holdings"]["updated"]
    assert up == {"date": "20261102", "source": "2026 年第 3 季度报告", "is_new": True}
    monkeypatch.setattr(config, "HUIJIN_UPDATED", {"date": "20261001", "source": "2026 年中期报告"})
    _, data = run(tmp_path)
    assert data["holdings"]["updated"]["is_new"] is False


def test_concentration_hot_rows_and_profit(tmp_path):
    _, data = run(tmp_path)
    cn = data["concentration"]
    fixed = {s["name"] for s in config.SECTORS}
    dyn = [r for r in cn["rows"] if r["name"] not in fixed]
    assert len(cn["rows"]) == 20 and len(dyn) == 5
    assert all(r["hot"] and r["icon"] == config.HOT_SECTORS["icon"] for r in dyn)
    assert len({r["name"] for r in dyn}) == 5
    done = [r for r in cn["rows"] if r["profit"] is not None]
    assert done and all(abs(r["profit"] + r["trapped"] - 100) < 0.11 for r in done)
    assert cn["market_profit"] is not None and abs(cn["market_profit"] + cn["market_trapped"] - 100) < 0.11
    assert cn["profit_date"] == DATE
    hot = cn["hot"]
    assert len(hot) == config.HOT_SECTORS["top_k"] and [h["rank"] for h in hot] == list(range(1, len(hot) + 1))
    assert set(hot[0]) >= {"code", "name", "level", "amount_ratio", "pct_n", "score", "rank", "covered"}
    import metrics_sentiment as ms
    assert {r["name"] for r in cn["rows"] if r["hot"] and r["name"] in fixed} == ms.hot_fixed_names(hot, config.HOT_SECTORS["top_k"])


class OneDayLateWinner(DemoSource):
    def winner_rate(self, trade_date):
        if trade_date == DATE:
            return super().winner_rate(trade_date).iloc[0:0]
        return super().winner_rate(trade_date)


def test_profit_falls_back_to_previous_day(tmp_path, monkeypatch):
    _, data = run(tmp_path, OneDayLateWinner(), monkeypatch=monkeypatch)
    cn = data["concentration"]
    assert cn["profit_date"] is not None and cn["profit_date"] < DATE
    assert any("获利盘数据截至" in w for w in data["meta"]["warnings"])
    assert cn["market_profit"] is not None


class NoClassify(DemoSource):
    def sw_classify(self):
        raise RuntimeError("boom")


def test_hot_degrades_when_classify_fails(tmp_path, monkeypatch):
    _, data = run(tmp_path, NoClassify(), monkeypatch=monkeypatch)
    cn = data["concentration"]
    assert len(cn["rows"]) == 15 and cn["hot"] == [] and not any(r["hot"] for r in cn["rows"])
    assert cn["market_profit"] is not None


class FixedL3Hot(DemoSource):
    """让固定板块「光伏设备·组件」的 L3 子行业放量暴涨 → 该固定板块标热。"""
    def sw_classify(self):
        cl = super().sw_classify()
        l2 = cl[cl["industry_name"] == "光伏设备Ⅱ"]["index_code"].iloc[0]
        extra = pd.DataFrame([("899999.SI", "光伏电池组件Ⅲ", "L3", l2)], columns=cl.columns)
        return pd.concat([cl, extra], ignore_index=True)

    def sw_daily_range(self, start, end):
        df = super().sw_daily_range(start, end)
        one = df[df["ts_code"] == df["ts_code"].iloc[0]].copy()
        one["ts_code"], one["name"], one["float_mv"] = "899999.SI", "光伏电池组件Ⅲ", 9e6
        days = sorted(one["trade_date"])
        one["amount"] = [1e5 if d < days[-5] else 1e7 for d in one["trade_date"]]
        one["pct_change"] = [0.0 if d < days[-5] else 9.0 for d in one["trade_date"]]
        return pd.concat([df, one], ignore_index=True)


def test_fixed_sector_marked_hot_by_l3_child(tmp_path, monkeypatch):
    _, data = run(tmp_path, FixedL3Hot(), monkeypatch=monkeypatch)
    cn = data["concentration"]
    row = next(r for r in cn["rows"] if r["name"] == "光伏设备·组件")
    assert row["hot"] is True
    assert cn["hot"][0]["name"] == "光伏电池组件" and cn["hot"][0]["covered"] is True
    assert "光伏电池组件" not in {r["name"] for r in cn["rows"]}  # covered 的不当动态行
    assert len(cn["rows"]) == 20


# ---------------------------------------------------------------- 估值·筹码

def test_valuation_demo_end_to_end(tmp_path):
    html, data = run(tmp_path)
    v = data["valuation"]
    assert [r["code"] for r in v["pb"]["rows"]] == [c for c, _ in config.VALUE_INDICES]
    assert len(v["pb"]["rows"]) == 8
    assert all(r["pb"] is not None and r["pct"] is not None for r in v["pb"]["rows"])
    assert v["pb"]["avg_pct"] is not None and v["pb"]["level"] in ("偏低", "中等", "偏高")
    ch = v["chips"]
    assert ch["trapped"] is not None and ch["level"] in ("偏重", "中等", "偏轻")
    assert [b["name"] for b in ch["boards"]] == ["沪深主板", "创业板", "科创板"]
    assert len(ch["history"]["dates"]) >= 5 and len(ch["history"]["dates"]) == len(ch["history"]["trapped"])
    assert ch["history"]["dates"][-1] == ch["date"] and ch["history"]["dates"] == sorted(ch["history"]["dates"])
    mg = v["margin"]
    assert mg["verdict"] in ("干净", "中性", "拥挤") and mg["lev_hist"]["values"]
    assert not any("估值" in w and "生成失败" in w for w in data["meta"]["warnings"])


class ValueBroken(DemoSource):
    def index_pb(self, code, start, end):
        raise RuntimeError("pb down")

    def margin_detail(self, start, end):
        raise RuntimeError("margin down")


def test_valuation_blocks_degrade_independently(tmp_path, monkeypatch):
    _, data = run(tmp_path, ValueBroken(), monkeypatch=monkeypatch)
    v = data["valuation"]
    assert len(v["pb"]["rows"]) == 8 and all(r["pb"] is None for r in v["pb"]["rows"])
    assert v["pb"]["avg_pct"] is None and v["pb"]["level"] is None
    assert v["margin"]["verdict"] is None and v["margin"]["lev_hist"]["dates"] == []
    assert v["chips"]["trapped"] is not None  # 套牢盘不受影响
    ws = data["meta"]["warnings"]
    assert any("PB" in w for w in ws) and any("融资明细" in w for w in ws)


class ChipsBroken(DemoSource):
    def winner_rate(self, trade_date):
        raise RuntimeError("cyq down")


def test_valuation_chips_empty_when_winner_rate_down(tmp_path, monkeypatch):
    _, data = run(tmp_path, ChipsBroken(), monkeypatch=monkeypatch)
    v = data["valuation"]
    assert v["chips"]["trapped"] is None and v["chips"]["history"] == {"dates": [], "trapped": []}
    assert v["pb"]["avg_pct"] is not None and v["margin"]["verdict"] is not None


class MarketStatsBroken(DemoSource):
    def market_stats(self, start, end):
        raise RuntimeError("daily_info down")


def test_valuation_margin_blank_when_market_stats_down(tmp_path, monkeypatch):
    _, data = run(tmp_path, MarketStatsBroken(), monkeypatch=monkeypatch)
    assert data["valuation"]["margin"]["verdict"] is None
    assert any("市场流通市值与成交额" in w for w in data["meta"]["warnings"])
