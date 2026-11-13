import json
from datetime import datetime
from zoneinfo import ZoneInfo

import healthcheck as hc
from test_build import run

SH = ZoneInfo("Asia/Shanghai")


def _card(key, value=1.0):
    return {"key": key, "label": key, "value": value, "unit": "%", "date": "2026-10"}


def _good():
    return {
        "eco_calendar": {"status": "ok", "events": [{"date": "20261106"}] * 3},
        "liquidity": {
            "macro": [_card("cn_cpi"), _card("cn_ppi")],
            "external": [_card(k) for k in ("us_cpi", "us_ppi", "us_nfp", "us_unrate")],
        },
        "meta": {"warnings": ["两融不足 3 个交易所"], "generated_at": "2026-11-09 21:40"},
    }


def _lv(results, item_part):
    return [r["level"] for r in results if item_part in r["item"]]


def test_extract_payload():
    html = 'x\nconst DATA = {"a": 1};\ny'
    assert hc.extract_payload(html) == {"a": 1}
    assert hc.extract_payload("没有数据") is None
    assert hc.extract_payload("const DATA = {坏;\n") is None


def test_check_payload_ok():
    res = hc.check_payload(_good())
    assert hc.overall(res) == "ok"
    assert all(r["level"] == "ok" for r in res)
    warn = [r for r in res if r["item"] == "构建告警"][0]
    assert warn["detail"] == "1 条"


def test_check_payload_calendar():
    d = _good()
    d["eco_calendar"]["status"] = "failed"
    assert "fail" in _lv(hc.check_payload(d), "日历")
    d = _good()
    d["eco_calendar"]["events"] = []
    assert "fail" in _lv(hc.check_payload(d), "日历")
    d = _good()
    del d["eco_calendar"]
    assert "fail" in _lv(hc.check_payload(d), "日历")
    d = _good()
    d["eco_calendar"]["status"] = "stale"
    res = hc.check_payload(d)
    assert _lv(res, "日历") == ["warn"]
    assert "存档" in [r for r in res if "日历" in r["item"]][0]["detail"]


def test_check_payload_cards():
    d = _good()
    d["liquidity"]["macro"] = [_card("cn_cpi", None), _card("cn_ppi")]
    res = hc.check_payload(d)
    assert hc.overall(res) == "fail"
    d = _good()
    d["liquidity"]["macro"] = [_card("cn_cpi")]  # 缺 cn_ppi
    assert hc.overall(hc.check_payload(d)) == "fail"
    d = _good()
    d["liquidity"]["external"] = [_card("us_cpi", None), _card("us_ppi"), _card("us_nfp")]  # 缺 us_unrate
    res = hc.check_payload(d)
    assert hc.overall(res) == "warn"
    assert sum(r["level"] == "warn" for r in res) == 2


def test_check_payload_none():
    res = hc.check_payload(None)
    assert len(res) == 1 and res[0]["level"] == "fail" and "找不到数据" in res[0]["detail"]


def test_overall():
    assert hc.overall([{"level": "ok"}]) == "ok"
    assert hc.overall([{"level": "ok"}, {"level": "warn"}]) == "warn"
    assert hc.overall([{"level": "warn"}, {"level": "fail"}]) == "fail"
    assert hc.overall([]) == "ok"


def test_expected_slots():
    s = hc.expected_slots(datetime(2026, 11, 9, 12, 40, tzinfo=SH), datetime(2026, 11, 10, 22, 0, tzinfo=SH))
    assert [x.strftime("%m-%d %H:%M") for x in s] == ["11-09 21:00", "11-10 12:30", "11-10 21:00"]
    # 区间左开右闭
    s = hc.expected_slots(datetime(2026, 11, 9, 12, 30, tzinfo=SH), datetime(2026, 11, 9, 21, 0, tzinfo=SH))
    assert [x.strftime("%H:%M") for x in s] == ["21:00"]


def test_missed_slots_examples():
    now = datetime(2026, 11, 10, 12, 31, tzinfo=SH)
    assert hc.missed_slots("2026-11-09 12:35", now) == ["11-09 21:00"]
    assert hc.missed_slots("2026-11-09 12:35", datetime(2026, 11, 9, 22, 50, tzinfo=SH)) == []
    assert hc.missed_slots("2026-11-06 21:05", datetime(2026, 11, 9, 12, 31, tzinfo=SH)) == []
    assert hc.missed_slots(None, now) == []


def test_missed_slots_grace_boundary():
    # 21:00 + 3h = 00:00，恰好到点算漏
    assert hc.missed_slots("2026-11-09 12:35", datetime(2026, 11, 10, 0, 0, tzinfo=SH)) == ["11-09 21:00"]
    assert hc.missed_slots("2026-11-09 12:35", datetime(2026, 11, 9, 23, 59, tzinfo=SH)) == []


def test_gate_window():
    d = lambda h, m: datetime(2026, 11, 10, h, m, tzinfo=SH)  # 周二
    assert hc.gate("schedule", d(12, 20), "2026-11-09 21:05")[0] is True
    assert hc.gate("schedule", d(14, 30), "2026-11-09 21:05")[0] is True
    assert hc.gate("schedule", d(12, 19), "2026-11-09 21:05")[0] is False   # 还没到窗口
    assert hc.gate("schedule", d(14, 31), "2026-11-09 21:05")[0] is False   # 过了窗口
    assert hc.gate("schedule", d(20, 50), "2026-11-10 12:35")[0] is True
    assert hc.gate("schedule", d(23, 30), "2026-11-10 12:35")[0] is True
    assert hc.gate("schedule", d(5, 0), "2026-11-09 21:05")[0] is False     # 凌晨不跑


def test_gate_already_built_and_weekend():
    d = datetime(2026, 11, 10, 13, 0, tzinfo=SH)
    assert hc.gate("schedule", d, "2026-11-10 12:25")[0] is False   # 本时段已跑过
    assert hc.gate("schedule", d, "2026-11-10 12:19")[0] is True
    assert hc.gate("schedule", d, None)[0] is True                  # 读不到线上页面也跑
    assert hc.gate("schedule", d, "坏格式")[0] is True
    sat = datetime(2026, 11, 14, 12, 40, tzinfo=SH)
    assert hc.gate("schedule", sat, "2026-11-13 21:05")[0] is False
    assert hc.gate("push", sat, "2026-11-13 21:05")[0] is True        # 推送和手动触发不看时间
    assert hc.gate("workflow_dispatch", datetime(2026, 11, 10, 3, 0, tzinfo=SH), None)[0] is True


def test_run_gate_writes_output(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    page = 'const DATA = ' + json.dumps({"meta": {"generated_at": "2026-11-10 12:25"}}) + ';\n'
    assert hc.run_gate("http://x/", "schedule", now=datetime(2026, 11, 10, 13, 0, tzinfo=SH),
                       fetch=lambda u: page) == 0
    assert "run=false" in out.read_text()
    out.write_text("")
    assert hc.run_gate("http://x/", "schedule", now=datetime(2026, 11, 10, 13, 0, tzinfo=SH),
                       fetch=lambda u: 1 / 0) == 0
    assert "run=true" in out.read_text()


def test_summary_markdown():
    res = [{"level": "ok", "item": "A", "detail": "好"}, {"level": "warn", "item": "B", "detail": "慢"},
           {"level": "fail", "item": "C", "detail": "坏"}]
    md = hc.summary_markdown(res, ["告警甲", "告警乙"], ["11-09 21:00"])
    assert "构建体检" in md and "✅" in md and "⚠️" in md and "❌" in md
    assert "11-09 21:00" in md and "<details>" in md and "告警甲" in md
    md2 = hc.summary_markdown(res[:1], [], [])
    assert "漏跑" not in md2 and "<details>" not in md2


def test_demo_page_passes(tmp_path, monkeypatch):
    html, _data = run(tmp_path, monkeypatch=monkeypatch)
    payload = hc.extract_payload(html)
    assert payload is not None
    res = hc.check_payload(payload)
    cal = [r for r in res if "日历" in r["item"]]
    assert cal and cal[0]["level"] == "ok"
    assert sum(r["level"] == "ok" and r["item"] != "构建告警" and "日历" not in r["item"] for r in res) == 6


def test_live_check_missed_and_unreadable():
    now = datetime(2026, 11, 10, 12, 31, tzinfo=SH)
    page = 'const DATA = ' + json.dumps({"meta": {"generated_at": "2026-11-09 12:35"}}) + ';\n'
    res = hc.live_check("http://x/", now=now, fetch=lambda url: page)
    assert res[0]["level"] == "fail" and "漏跑或严重延迟：11-09 21:00" in res[0]["detail"]
    page = 'const DATA = ' + json.dumps({"meta": {"generated_at": "2026-11-09 21:05"}}) + ';\n'
    assert hc.live_check("http://x/", now=now, fetch=lambda url: page) == []

    def boom(url):
        raise OSError("down")
    res = hc.live_check("http://x/", now=now, fetch=boom)
    assert res[0]["level"] == "warn" and "无法读取线上页面" in res[0]["detail"]


def test_verify_live_second_poll_matches():
    pages = iter(['const DATA = {"meta": {"generated_at": "old"}};\n',
                  'const DATA = {"meta": {"generated_at": "new"}};\n'])
    sleeps = []
    rc = hc.verify_live("http://x/", "new", "ok", timeout=360, interval=30,
                        fetch=lambda url: next(pages), sleep=sleeps.append)
    assert rc == 0 and sleeps == [30]


def test_verify_live_never_matches():
    sleeps = []
    rc = hc.verify_live("http://x/", "new", "ok", timeout=90, interval=30,
                        fetch=lambda url: 'const DATA = {"meta": {"generated_at": "old"}};\n', sleep=sleeps.append)
    assert rc == 1 and len(sleeps) >= 2


def test_verify_live_status_fail():
    rc = hc.verify_live("http://x/", "new", "fail", timeout=60, interval=30,
                        fetch=lambda url: 'const DATA = {"meta": {"generated_at": "new"}};\n', sleep=lambda s: None)
    assert rc == 1


def test_verify_live_fetch_errors_keep_polling():
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("x")
        return 'const DATA = {"meta": {"generated_at": "new"}};\n'
    assert hc.verify_live("http://x/", "new", "warn", timeout=360, interval=30, fetch=fetch,
                          sleep=lambda s: None) == 0
    assert len(calls) == 2


def test_main_writes_outputs_and_exits_0(tmp_path, monkeypatch):
    html, data = run(tmp_path, monkeypatch=monkeypatch)
    out, summ = tmp_path / "out.txt", tmp_path / "sum.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))
    assert hc.main([str(tmp_path / "i.html")]) == 0
    o = out.read_text(encoding="utf-8")
    assert "status=" in o and f"generated_at={data['meta']['generated_at']}" in o
    assert "构建体检" in summ.read_text(encoding="utf-8")


def test_fetch_url_adds_cache_buster(monkeypatch):
    seen = []

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"ok"

    monkeypatch.setattr(hc.urllib.request, "urlopen", lambda req, timeout: seen.append((req.full_url, timeout)) or R())
    assert hc._fetch_url("http://x/") == "ok"
    assert "?t=" in seen[0][0] and seen[0][1] == 20


import pytest


@pytest.mark.parametrize("bad", ["2026-11-10 21:40:05", "2026-11-10T21:40", 123, "x"])
def test_live_check_bad_generated_at_warns(bad):
    page = "const DATA = " + json.dumps({"meta": {"generated_at": bad}}) + ";\n"
    res = hc.live_check("http://x/", now=datetime(2026, 11, 10, 9, 8, tzinfo=SH), fetch=lambda u: page)
    assert res[0]["level"] == "warn" and "格式无法识别" in res[0]["detail"]


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(liquidity=[]),
    lambda d: d.update(meta="x"),
    lambda d: d.update(eco_calendar={"status": "ok", "events": 5}),
    lambda d: d.update(liquidity={"macro": "x", "external": 3}),
    lambda d: d.update(meta={"warnings": 7}),
    lambda d: d.update(eco_calendar="x"),
])
def test_check_payload_malformed_shapes_do_not_raise(mutate):
    d = _good()
    mutate(d)
    assert hc.overall(hc.check_payload(d)) in ("ok", "warn", "fail")


def test_main_never_fails_on_malformed_live(tmp_path, monkeypatch):
    html, data = run(tmp_path, monkeypatch=monkeypatch)
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(hc, "_fetch_url", lambda u: 'const DATA = {"meta": {"generated_at": 123}};\n')
    assert hc.run_check(str(tmp_path / "i.html"), "http://x/") == 0
    assert "generated_at=" + data["meta"]["generated_at"] in out.read_text(encoding="utf-8")


def test_run_check_internal_error_still_writes_outputs(tmp_path, monkeypatch):
    html, data = run(tmp_path, monkeypatch=monkeypatch)
    out, summ = tmp_path / "out.txt", tmp_path / "sum.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))

    def boom(*a, **k):
        raise ValueError("x")
    monkeypatch.setattr(hc, "check_payload", boom)
    assert hc.run_check(str(tmp_path / "i.html"), None) == 0
    o = out.read_text(encoding="utf-8")
    assert "status=fail" in o and "generated_at=" + data["meta"]["generated_at"] in o
    assert "体检脚本异常：ValueError" in summ.read_text(encoding="utf-8")


def test_run_check_missing_file_still_zero(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert hc.run_check(str(tmp_path / "nope.html"), None) == 0
    assert "status=fail" in out.read_text(encoding="utf-8")


def test_verify_live_empty_expect_fails_fast():
    sleeps = []
    assert hc.verify_live("http://x/", "", "ok", fetch=lambda u: 1 / 0, sleep=sleeps.append) == 1
    assert sleeps == []
