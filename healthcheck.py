"""构建体检与上线核对（只用标准库）。

两种用法：
  python healthcheck.py site/index.html [--live-url URL]
      体检刚构建好的页面（关键数据是否齐全、有没有漏跑），永远 exit 0，不拦发布；
      结论写进 GITHUB_OUTPUT（status、generated_at）和 GITHUB_STEP_SUMMARY。
  python healthcheck.py --verify-live URL --expect-generated TEXT [--status S]
      部署后轮询线上页面，确认已更新到本次版本；没更新到、或体检结论是 fail 时 exit 1（运行标红）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SH = ZoneInfo("Asia/Shanghai")
SLOTS = ((9, 7), (21, 37))  # 北京时间的两个主定时点
CN_CARDS = ("cn_cpi", "cn_ppi")
US_CARDS = ("us_cpi", "us_ppi", "us_nfp", "us_unrate")
ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌"}


def extract_payload(html: str) -> dict | None:
    """取页面里嵌入的 `const DATA = ...;` JSON；取不到或 JSON 坏返回 None。"""
    m = re.search(r"const DATA = ([^\n]*);\n", html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _res(level: str, item: str, detail: str) -> dict:
    return {"level": level, "item": item, "detail": detail}


def _find_card(cards, key):
    return next((c for c in (cards or []) if isinstance(c, dict) and c.get("key") == key), None)


def _calendar_result(data: dict) -> dict:
    eco = data.get("eco_calendar")
    if not isinstance(eco, dict) or eco.get("status") == "failed" or not eco.get("events"):
        return _res("fail", "财经日历", "取数失败或没有事件")
    if eco.get("status") == "stale":
        return _res("warn", "财经日历", "本次未能刷新，用的是存档")
    return _res("ok", "财经日历", f"{len(eco['events'])} 条事件")


def _card_result(cards, key, miss_level: str) -> dict:
    card = _find_card(cards, key)
    if card is None or card.get("value") is None:
        return _res(miss_level, f"卡片 {key}", "缺失或无数据")
    return _res("ok", f"卡片 {key}",
                f"{card.get('label', key)} {card['value']}{card.get('unit') or ''}（{card.get('date')}）")


def check_payload(data: dict | None) -> list[dict]:
    if data is None:
        return [_res("fail", "页面数据", "页面里找不到数据")]
    liq = data.get("liquidity") or {}
    out = [_calendar_result(data)]
    out += [_card_result(liq.get("macro"), k, "fail") for k in CN_CARDS]
    out += [_card_result(liq.get("external"), k, "warn") for k in US_CARDS]
    n = len((data.get("meta") or {}).get("warnings") or [])
    out.append(_res("ok", "构建告警", f"{n} 条"))
    return out


def overall(results: list[dict]) -> str:
    levels = {r["level"] for r in results}
    return "fail" if "fail" in levels else "warn" if "warn" in levels else "ok"


def expected_slots(start: datetime, end: datetime) -> list[datetime]:
    """北京时间 (start, end] 内所有工作日的 09:07 和 21:37，升序。不看 A 股节假日。"""
    start, end = start.astimezone(SH), end.astimezone(SH)
    out = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        if day.weekday() < 5:
            for h, m in SLOTS:
                s = day.replace(hour=h, minute=m)
                if start < s <= end:
                    out.append(s)
        day += timedelta(days=1)
    return out


def missed_slots(prev_generated: str | None, now: datetime, grace_hours: float = 3.0) -> list[str]:
    """上次上线之后、已过宽限期仍没跑的定时点（"MM-DD HH:MM"）。"""
    if not prev_generated:
        return []
    prev = datetime.strptime(prev_generated, "%Y-%m-%d %H:%M").replace(tzinfo=SH)
    grace = timedelta(hours=grace_hours)
    return [s.strftime("%m-%d %H:%M") for s in expected_slots(prev, now + timedelta(days=1))
            if s + grace <= now]


def summary_markdown(results: list[dict], warnings: list[str], missed: list[str]) -> str:
    lines = ["## 构建体检", "", "| 结果 | 项目 | 说明 |", "| --- | --- | --- |"]
    for r in results:
        lines.append(f"| {ICON.get(r['level'], r['level'])} | {r['item']} | {r['detail']} |")
    if missed:
        lines += ["", "**漏跑的定时点：** " + "、".join(missed)]
    if warnings:
        lines += ["", f"<details><summary>构建告警原文（{len(warnings)} 条）</summary>", ""]
        lines += [f"- {w}" for w in warnings]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def _fetch_url(url: str) -> str:
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}t={int(time.time())}", headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
        return r.read().decode("utf-8", "replace")


def _generated_at(html: str) -> str | None:
    payload = extract_payload(html)
    return ((payload or {}).get("meta") or {}).get("generated_at")


def live_check(url: str, now: datetime | None = None, fetch=_fetch_url) -> list[dict]:
    """用线上（上一版）页面的生成时间检查有没有漏跑；返回要追加的结果项。"""
    now = now or datetime.now(SH)
    try:
        prev = _generated_at(fetch(url))
    except Exception:  # noqa: BLE001
        prev = None
    if prev is None:
        return [_res("warn", "漏跑检查", "无法读取线上页面，未检查漏跑")]
    missed = missed_slots(prev, now)
    if missed:
        return [dict(_res("fail", "漏跑检查", "漏跑：" + "、".join(missed)), missed=missed)]
    return []


def _append(env_name: str, text: str) -> None:
    path = os.environ.get(env_name)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def run_check(html_path: str, live_url: str | None, now: datetime | None = None, fetch=_fetch_url) -> int:
    with open(html_path, encoding="utf-8") as f:
        payload = extract_payload(f.read())
    results = check_payload(payload)
    if live_url:
        results += live_check(live_url, now, fetch)
    meta = (payload or {}).get("meta") or {}
    missed = [m for r in results for m in r.get("missed", [])]
    md = summary_markdown(results, meta.get("warnings") or [], missed)
    print(md)
    _append("GITHUB_STEP_SUMMARY", md)
    _append("GITHUB_OUTPUT", f"status={overall(results)}\ngenerated_at={meta.get('generated_at') or ''}\n")
    return 0  # 体检不拦发布


def verify_live(url: str, expect: str, status: str | None, timeout: float = 360, interval: float = 30,
                fetch=_fetch_url, sleep=time.sleep) -> int:
    """轮询线上页面直到 generated_at 等于期望值；没更新到、或体检 fail 返回 1。"""
    waited, seen, ok = 0.0, None, False
    while True:
        try:
            seen = _generated_at(fetch(url))
        except Exception:  # noqa: BLE001
            seen = None
        if seen == expect:
            ok = True
            break
        if waited >= timeout:
            break
        sleep(interval)
        waited += interval
    lines = ["## 上线核对", ""]
    lines.append(f"- {'✅' if ok else '❌'} 线上页面生成时间：{seen or '读取失败'}（期望 {expect}）")
    if status:
        lines.append(f"- {ICON.get(status, status)} 构建体检结论：{status}")
    md = "\n".join(lines) + "\n"
    print(md)
    _append("GITHUB_STEP_SUMMARY", md)
    return 0 if ok and status != "fail" else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建体检与上线核对")
    ap.add_argument("html", nargs="?", help="构建出的 index.html 路径")
    ap.add_argument("--live-url", help="线上页面地址，用来检查漏跑")
    ap.add_argument("--verify-live", metavar="URL", help="部署后核对线上页面是否已更新")
    ap.add_argument("--expect-generated", default="", help="期望的 meta.generated_at")
    ap.add_argument("--status", help="build 阶段体检结论 ok|warn|fail")
    ap.add_argument("--timeout", type=float, default=360)
    ap.add_argument("--interval", type=float, default=30)
    a = ap.parse_args(argv)
    if a.verify_live:
        return verify_live(a.verify_live, a.expect_generated, a.status, a.timeout, a.interval)
    if not a.html:
        ap.error("需要 HTML_PATH 或 --verify-live")
    return run_check(a.html, a.live_url)


if __name__ == "__main__":
    sys.exit(main())
