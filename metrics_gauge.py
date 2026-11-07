"""流动性指数的纯计算（规则见 README「流动性指数」）。索引均为 YYYYMMDD 字符串、升序。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config

_PHRASE = {"domestic_tight": "国内资金在收紧", "external_tight": "外部在收紧", "risk_off": "资金在避险"}
_GROUP_CN = {"domestic": "国内", "external": "外部"}


def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def change_n(x: pd.Series, kind: str, n: int) -> pd.Series:
    x = pd.to_numeric(x, errors="coerce")
    if kind == "pct_level":
        return (x - x.shift(n)) * 100.0
    if kind == "bp_level":
        return x - x.shift(n)
    if kind == "price":
        return np.log(x / x.shift(n))
    raise ValueError(f"unknown kind: {kind}")


def break_mask(chg: pd.Series, break_dates: list[str], n: int) -> pd.Series:
    out = chg.copy()
    idx = list(chg.index)
    for b in break_dates or []:
        pos = next((i for i, d in enumerate(idx) if d >= b), None)
        if pos is None:
            continue
        out.iloc[pos:pos + n] = np.nan
    return out


def z_series(chg: pd.Series, window: int, min_obs: int, floor: float) -> pd.Series:
    vals = chg.dropna()
    s = vals.rolling(window, min_periods=min_obs).std(ddof=1).shift(1)
    s = s.reindex(chg.index)
    # 被剔除（NaN）的日子没有 z；其余日子用「之前最近 window 个有效样本」的标准差
    s = s.where(chg.notna())
    return chg / s.clip(lower=floor)


def score(z, dir: int):
    if dir == 0 or not _ok(z):
        return None
    return float(min(100.0, max(0.0, 50.0 + dir * float(z) * 50.0 / 3.0)))


def zone(v):
    if not _ok(v):
        return None
    zones = config.GAUGE_ZONES
    if v < zones[0][1]:
        return zones[0][2]
    for lo, hi, name in zones:
        if lo <= v < hi:
            return name
    return zones[-1][2]


def component_frame(x: pd.Series, spec: dict, breaks: list[str], cfg: dict) -> pd.DataFrame:
    x = pd.to_numeric(x, errors="coerce")
    chg = change_n(x, spec["kind"], cfg["n"])
    chg = break_mask(chg, breaks, cfg["n"])
    floor = cfg["floor_log"] if spec["kind"] == "price" else cfg["floor_bp"]
    z = z_series(chg, cfg["window"], cfg["min_obs"], floor)
    sc = z.map(lambda v: score(v, spec["dir"]))
    sc = pd.to_numeric(sc, errors="coerce")
    return pd.DataFrame({"value": x, "chg": chg, "z": z, "score": sc}, index=x.index)


def _days_between(a: str, b: str) -> int:
    return (datetime.strptime(b, "%Y%m%d") - datetime.strptime(a, "%Y%m%d")).days


def index_on(day: str, frames: dict, specs: list, cfg: dict) -> dict:
    items: dict[str, dict] = {}
    for sp in specs:
        f = frames.get(sp["key"])
        if f is None or f.empty:
            continue
        sub = f[f.index <= day]
        if sub.empty:
            continue
        d = sub.index[-1]
        if _days_between(d, day) > cfg["stale_days"]:
            continue
        row = sub.iloc[-1]
        sc = row["score"]
        if sp["dir"] != 0 and not _ok(sc):
            continue
        if not _ok(row["value"]):
            continue
        items[sp["key"]] = {
            "date": d,
            "value": float(row["value"]),
            "chg": float(row["chg"]) if _ok(row["chg"]) else None,
            "z": float(row["z"]) if _ok(row["z"]) else None,
            "score": float(sc) if sp["dir"] != 0 else None,
        }
    group_score: dict[str, float | None] = {}
    missing = {"domestic": 0, "external": 0}
    for g in ("domestic", "external"):
        keys = [sp["key"] for sp in specs if sp["group"] == g and sp["dir"] != 0]
        have = [items[k]["score"] for k in keys if k in items]
        missing[g] = len(keys) - len(have)
        group_score[g] = float(np.mean(have)) if keys and len(have) * 2 >= len(keys) and have else None
    dom, ext = group_score["domestic"], group_score["external"]
    if dom is not None and ext is not None:
        value = 0.5 * dom + 0.5 * ext
    else:
        value = dom if dom is not None else ext
    return {"value": value, "domestic": dom, "external": ext, "missing": missing, "items": items}


def index_history(days: list[str], frames: dict, specs: list, cfg: dict) -> pd.Series:
    vals = []
    for d in days:
        v = index_on(d, frames, specs, cfg)["value"]
        vals.append(np.nan if v is None else v)
    return pd.Series(vals, index=list(days), dtype=float)


def ago(hist: pd.Series, day: str) -> dict:
    h = hist.dropna()

    def at_or_before(key: str, strict: bool):
        sub = h[h.index < key] if strict else h[h.index <= key]
        return None if sub.empty else int(round(float(sub.iloc[-1])))

    t = datetime.strptime(day, "%Y%m%d")
    out = {"prev": at_or_before(day, True)}
    for k, n in (("w1", 7), ("m1", 30), ("y1", 365)):
        out[k] = at_or_before((t - timedelta(days=n)).strftime("%Y%m%d"), False)
    return out


def combos(zs: dict, combo_specs: list, cfg: dict) -> list[dict]:
    res = []
    for c in combo_specs:
        conds = []
        for key, sign, name in c["conds"]:
            z = zs.get(key)
            conds.append({"name": name, "ok": bool(_ok(z) and sign * float(z) >= cfg["z_cond"])})
        met = sum(1 for x in conds if x["ok"])
        res.append({"key": c["key"], "name": c["name"], "met": met, "of": len(conds),
                    "on": met >= cfg["combo_need"], "conds": conds})
    return res


def verdict(value, combos_out: list, items: dict, specs: list, missing: dict) -> tuple[str, str]:
    if not _ok(value):
        return "数据不足", "数据不足"
    parts = []
    for c in combos_out:
        if c.get("on"):
            names = "、".join(x["name"] for x in c["conds"] if x["ok"])
            parts.append(f"{_PHRASE.get(c['key'], c['name'])}：{names}")
    if parts:
        reason = "；".join(parts)
    else:
        names = {s["key"]: s["name"] for s in specs}
        dev = [(abs(it["score"] - 50), k, it["score"]) for k, it in items.items()
               if _ok(it.get("score")) and k in names]
        dev = sorted((d for d in dev if d[0] > 10), reverse=True)[:2]
        if dev:
            reason = "主要来自 " + "、".join(f"{names[k]}{'偏紧' if sc < 50 else '偏松'}" for _, k, sc in dev)
        else:
            reason = "各项都在常态范围内"
    for g in ("domestic", "external"):
        n = sum(1 for s in specs if s["group"] == g and s["dir"] != 0)
        miss = (missing or {}).get(g, 0)
        if n and (n - miss) * 2 < n:
            reason += f"（{_GROUP_CN[g]}数据不足）"
        elif miss > 0:
            reason += f"（{_GROUP_CN[g]}缺 {miss} 项）"
    return f"{zone(value)}。{reason}", reason


def _falling3(s: pd.Series) -> bool:
    v = pd.to_numeric(s, errors="coerce").dropna()
    if len(v) < 4:
        return False
    last = v.iloc[-4:].tolist()
    return all(last[i] > last[i + 1] for i in range(3))


def gate(m1_yoy: pd.Series, sf_yoy: pd.Series, fed_net: pd.Series) -> dict:
    notes = []
    if _falling3(m1_yoy):
        notes.append("背景：M1 同比连续 3 个月回落")
    if _falling3(sf_yoy):
        notes.append("背景：社融增速连续 3 个月放缓")
    f = pd.to_numeric(fed_net, errors="coerce").dropna()
    if len(f) >= 5 and f.iloc[-5] != 0:
        chg = float(f.iloc[-1]) / float(f.iloc[-5]) - 1.0
        if chg < -0.03:
            notes.append(f"背景：美元净流动性 4 周收缩 {abs(chg) * 100:.1f}%")
    return {"ok": not notes, "notes": notes}
