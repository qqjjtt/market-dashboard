"""财经日历：白名单匹配、清洗、去重、下次公布。纯逻辑，无 I/O、无网络。"""
import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

SH = ZoneInfo("Asia/Shanghai")
log = logging.getLogger(__name__)

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
        date = str(r.get("date") or "")
        if not (len(date) == 8 and date.isdigit()):
            continue
        out.append({
            "date": date,
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


def _best(group, date_first=False, fresh_from=0):
    """group: [(idx, ev)]。第一轮（同日）：有 value > 出现更晚；
    第二轮（改期，date_first=True）：有 value > 本次新取到（idx ≥ fresh_from）> 日期更晚 > 出现更晚
    （单次取数内行序不可靠；改期提前时新数据必须盖过存档里的旧日期）。"""
    if date_first:
        return max(group, key=lambda p: (p[1]["value"] is not None, p[0] >= fresh_from, p[1]["date"], p[0]))
    return max(group, key=lambda p: (p[1]["value"] is not None, p[0], p[1]["date"]))


def _day(d):
    return datetime.strptime(d, "%Y%m%d")


def dedupe(events, fresh_from=None):
    """fresh_from：events 中从这个下标起是本次新取到的（merge 用）；None 表示不区分。"""
    fresh_from = len(events) if fresh_from is None else fresh_from
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
                final.append(_best(cluster, True, fresh_from))
                cluster = [p]
        final.append(_best(cluster, True, fresh_from))

    res = [p[1] for p in final]
    res.sort(key=lambda e: (e["date"], e["time"] or "99:99", e["region"], e["item"]))
    return res


def merge(stored, fresh):
    stored = list(stored)
    return dedupe(stored + list(fresh), fresh_from=len(stored))


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


# ---------------------------------------------------------------- 存档与分段取数

def _empty_store():
    return {"covered_from": None, "events": []}


def load_store(path):
    if not os.path.exists(path):
        return _empty_store()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        cf, evs = d.get("covered_from"), d["events"]
        if not isinstance(evs, list):
            raise ValueError("events")
        if cf is not None and not (isinstance(cf, str) and len(cf) == 8 and cf.isdigit()):
            raise ValueError("covered_from")
    except Exception:  # noqa: BLE001
        log.warning("财经日历存档无法读取，已从空数据开始")
        return _empty_store()
    good, bad, gone = [], 0, 0
    for e in evs:
        if not _valid_row(e):
            bad += 1
        elif e["item"] not in _BY_KEY:
            gone += 1
        else:
            good.append(_refresh_row(e))
    if bad:
        log.warning("财经日历存档有 %d 条记录格式不对，已忽略", bad)
    if gone:
        log.info("财经日历存档有 %d 条记录已不在白名单里，已去掉", gone)
    return {"covered_from": cf, "events": good}


_ROW_KEYS = ("date", "time", "region", "item", "label", "period", "major", "value", "pre", "fore")


def _valid_row(e):
    return (isinstance(e, dict) and all(k in e for k in _ROW_KEYS) and isinstance(e["item"], str)
            and isinstance(e["date"], str) and len(e["date"]) == 8 and e["date"].isdigit())


def _refresh_row(e):
    """按当前白名单重写 label / major / region（报告期保留）。"""
    it = _BY_KEY[e["item"]]
    p = e["period"]
    return dict(e, region=it["region"], major=it["major"],
                label=f"{it['label']} · {p}" if p else it["label"])


def save_store(path, store):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    evs = sorted(store["events"], key=lambda e: (e["date"], e["time"] or "99:99", e["region"], e["item"]))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"covered_from": store.get("covered_from"), "events": evs}, f, ensure_ascii=False, indent=0)
    os.replace(tmp, path)


def _shift(d, n):
    return (_day(d) + timedelta(days=n)).strftime("%Y%m%d")


class _Fetcher:
    def __init__(self, src, max_calls):
        self.src, self.max_calls = src, max_calls
        self.calls = 0
        self.rows = []
        self.error = None     # 异常类型名
        self.out_of_budget = False

    def fetch(self, country, s, e):
        """取 [s, e]（含）。返回是否完整取到；预算用完或出错时返回 False 并停止后续。"""
        if self.error or self.out_of_budget:
            return False
        if self.calls >= self.max_calls:
            self.out_of_budget = True
            return False
        self.calls += 1
        try:
            df = self.src.eco_cal(s, e, country)
        except Exception as ex:  # noqa: BLE001
            self.error = type(ex).__name__
            return False
        n = 0 if df is None else len(df)
        if n:
            self.rows.extend(df.to_dict("records"))
        if n >= config.ECO_CAL_ROW_LIMIT:
            span = (_day(e) - _day(s)).days
            if span < 1:
                log.warning("财经日历 %s 单日返回达 %d 行上限，可能被截断", s, config.ECO_CAL_ROW_LIMIT)
                return True
            mid = _shift(s, span // 2)
            left = self.fetch(country, s, mid)
            return self.fetch(country, _shift(mid, 1), e) and left
        return True


def fetch_events(src, today, store, max_calls, backfill=True):
    """刷新窗口优先，再从新到旧回填。返回 (新 store, 调用次数)。
    新 store 带临时键 _refresh_ok（刷新窗口是否全部取到），save_store 不会写出它。"""
    f = _Fetcher(src, max_calls)
    chunk_days = config.ECO_CAL_CHUNK_DAYS
    refresh_ok = True
    r_start, r_end = _shift(today, -config.ECO_CAL_PAST_DAYS), _shift(today, config.ECO_CAL_FUTURE_DAYS)
    for country, size in chunk_days.items():
        s = r_start
        while s <= r_end:
            e = min(r_end, _shift(s, size - 1))
            if not f.fetch(country, s, e):
                refresh_ok = False
                break
            s = _shift(e, 1)

    covered = store.get("covered_from")
    target = _shift(today, -config.ECO_CAL_KEEP_DAYS)
    if backfill and refresh_ok and (covered is None or covered > target):
        top = r_start if covered is None else min(covered, r_start)
        prog = {c: top for c in chunk_days}
        while max(prog.values()) > target:
            c = max(prog, key=lambda k: prog[k])       # 落后（起点更近）的一方先取
            e = _shift(prog[c], -1)
            s = max(target, _shift(e, -(chunk_days[c] - 1)))
            if not f.fetch(c, s, e):
                break
            prog[c] = s
        new_cov = max(prog.values())
        if new_cov < top:
            covered = new_cov
        if max(prog.values()) > target and not f.error:
            log.info("财经日历回填未完成（预算 %d 次用完），下次构建继续", max_calls)

    if f.error:
        log.warning("财经日历取数中断（%s），已用存档补齐", f.error)
    elif not refresh_ok:
        log.warning("财经日历取数次数用完（%d 次），刷新窗口未取全，已用存档补齐", max_calls)
    fresh = normalize(f.rows)
    stored = store.get("events", [])
    if refresh_ok:   # 刷新窗口取全了：窗口内没公布值、这次又没再出现的旧行（改期 / 取消）去掉
        seen = {(e["item"], e["date"]) for e in fresh} | {(e["item"], e["period"]) for e in fresh if e["period"]}
        stored = [e for e in stored
                  if e["value"] is not None or not (r_start <= e["date"] <= r_end)
                  or (e["item"], e["date"]) in seen or (e["period"] and (e["item"], e["period"]) in seen)]
    # 存档只增不裁；payload 的时间范围由 build 控制
    return {"covered_from": covered, "events": merge(stored, fresh), "_refresh_ok": refresh_ok}, f.calls
