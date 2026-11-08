"""财经日历：白名单匹配、清洗、去重、下次公布。纯逻辑，无 I/O、无网络。"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

SH = ZoneInfo("Asia/Shanghai")

_PERIOD_RE = re.compile(r"^(一|二|三|四|五|六|七|八|九|十|十一|十二)月$|^第[一二三四]季度$")
_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")
_WEEKDAYS = "一二三四五六日"
_RESCHEDULE_DAYS = 45

_ITEMS = [(it, re.compile(it["pattern"])) for it in config.ECO_CAL_ITEMS]
_BY_KEY = {it["key"]: it for it in config.ECO_CAL_ITEMS}


def match_item(event_name):
    for it, rx in _ITEMS:
        if rx.search(event_name or ""):
            return it
    return None


def clean_time(t, kind):
    """data 类的占位时间（分钟不在 00/15/30/45）视为未知；event 类保留原时间。"""
    m = _TIME_RE.match(t or "")
    if not m:
        return None
    if kind == "data" and m.group(2) not in ("00", "15", "30", "45"):
        return None
    return t


def period_of(event_name):
    m = re.search(r"\(([^()]*)\)\s*$", event_name or "")
    if m and _PERIOD_RE.match(m.group(1)):
        return m.group(1)
    return None


def _val(x):
    if x is None:
        return None
    s = str(x).strip()
    return None if s == "" or s.lower() == "nan" else s


def normalize(rows):
    out = []
    for r in rows:
        name = r.get("event") or ""
        it = match_item(name)
        if it is None:
            continue
        period = period_of(name)
        out.append({
            "date": str(r.get("date") or ""),
            "time": clean_time(r.get("time"), it["kind"]),
            "region": it["region"],
            "item": it["key"],
            "label": f"{it['label']} · {period}" if period else it["label"],
            "period": period,
            "major": it["major"],
            "value": _val(r.get("value")),
            "pre": _val(r.get("pre_value")),
            "fore": _val(r.get("fore_value")),
        })
    return out


def _best(group):
    """group: [(idx, ev)]。优先级：有 value > 出现更晚 > 日期更晚。"""
    return max(group, key=lambda p: (p[1]["value"] is not None, p[0], p[1]["date"]))


def _day(d):
    return datetime.strptime(d, "%Y%m%d")


def dedupe(events):
    # 第一轮：同 (item, date) 一条
    g1 = {}
    for i, e in enumerate(events):
        g1.setdefault((e["item"], e["date"]), []).append((i, e))
    kept = [_best(g) for g in g1.values()]

    # 第二轮：同 item、同非空 period、日期相差 ≤45 天的一条
    final = [p for p in kept if not p[1]["period"]]
    groups = {}
    for p in kept:
        if p[1]["period"]:
            groups.setdefault((p[1]["item"], p[1]["period"]), []).append(p)
    for g in groups.values():
        g.sort(key=lambda p: p[1]["date"])
        cluster = [g[0]]
        for p in g[1:]:
            if (_day(p[1]["date"]) - _day(cluster[-1][1]["date"])).days <= _RESCHEDULE_DAYS:
                cluster.append(p)
            else:
                final.append(_best(cluster))
                cluster = [p]
        final.append(_best(cluster))

    res = [p[1] for p in final]
    res.sort(key=lambda e: (e["date"], e["time"] or "99:99", e["region"], e["item"]))
    return res


def merge(stored, fresh):
    return dedupe(list(stored) + list(fresh))


def _sh(now):
    return now.astimezone(SH)


def is_released(ev, now):
    if ev["value"]:
        return True
    n = _sh(now)
    if ev["time"]:
        return (ev["date"], ev["time"]) <= (n.strftime("%Y%m%d"), n.strftime("%H:%M"))
    return ev["date"] < n.strftime("%Y%m%d")


def next_release(events, item, now):
    cands = [e for e in events if e["item"] == item and not is_released(e, now)]
    if not cands:
        return None
    return min(cands, key=lambda e: (e["date"], e["time"] or "99:99"))


def format_next(date, time):
    d = _day(date)
    s = f"下次公布：{d.month}/{d.day} 周{_WEEKDAYS[d.weekday()]}"
    return f"{s} {time}" if time else f"{s}（时间待定）"
