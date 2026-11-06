"""ETF（国家队）纯函数：净申赎、披露规则、本轮汇总、月度合计、成交指纹、尾盘占比、结论文案。

不做 IO。缺失值一律 NaN/None，绝不补 0；未披露 ≠ 净赎回。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

MINUS = "−"  # 负号用 −，与页面其它文案一致


def _num(x) -> float | None:
    """NaN/None → None，其余转 float。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def _signed(x: float) -> str:
    """带符号整数百分号前的数字：+5 / −3 / 0。"""
    n = _round_half_up(x)
    if n > 0:
        return f"+{n}"
    if n < 0:
        return f"{MINUS}{-n}"
    return "0"


def net_flow(share: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """净申赎（亿元）= (当日份额 − 前一交易日份额) × 当日收盘；任一端 NaN 则 NaN。

    share 单位亿份、close 单位元，相乘即亿元。
    """
    close = close.reindex(index=share.index, columns=share.columns)
    return (share - share.shift(1)) * close


def aggregate(net: pd.DataFrame, min_ratio: float = 0.8) -> pd.Series:
    """逐日合计（sum(min_count=1)）；当日非空只数 / 列数 < min_ratio → NaN（视为未披露）。"""
    total = net.sum(axis=1, min_count=1)
    ncols = net.shape[1]
    if ncols == 0:
        return total.astype(float)
    enough = net.notna().sum(axis=1) >= min_ratio * ncols - 1e-9
    return total.where(enough)


def round_summary(daily: pd.Series, start: str, attribution: tuple[float, float], count: int) -> dict:
    """本轮汇总：start 起的累计（只加非 NaN）、归因区间与最近 3 个交易日（新→旧，NaN → None）。"""
    s = daily.iloc[0:0] if len(daily) == 0 else daily[daily.index >= start]
    vals = s.dropna()
    total = float(vals.sum()) if len(vals) else None
    lo_r, hi_r = attribution
    low = total * lo_r if total is not None else None
    high = total * hi_r if total is not None else None
    mid = (low + high) / 2 if total is not None else None
    last3 = [{"date": str(d), "value": _num(v)} for d, v in list(s.items())[-3:][::-1]]
    return {
        "count": count,
        "start": start,
        "end": str(s.index[-1]) if len(s) else None,
        "total": total,
        "low": low,
        "high": high,
        "mid": mid,
        "last3": last3,
    }


def monthly_totals(daily: pd.Series, months: int) -> list[dict]:
    """最近 months 个月的合计；days = 该月在序列中的交易日数（含未披露日）。"""
    if len(daily) == 0 or months <= 0:
        return []
    idx = pd.Series([str(d) for d in daily.index])
    ym = idx.str[:6].to_numpy()
    values = daily.to_numpy(dtype=float)
    keys = sorted(set(ym))[-months:]
    out = []
    for k in keys:
        mask = ym == k
        v = values[mask]
        valid = v[~np.isnan(v)]
        out.append({
            "label": f"{int(k[4:6])}月",
            "days": int(mask.sum()),
            "value": float(valid.sum()) if len(valid) else None,
        })
    return out


def amount_groups(amount: pd.DataFrame, today: str, yday: str, groups: dict[str, list[str]]) -> list[dict]:
    """各组成交额（亿元）今/昨对比。chg_pct、ratio 取整；昨日为 0/NaN 或今日缺失 → None。

    key 与 name 均取 groups 的键（构建脚本如需展示名可自行改写）。n = 今日有数据的只数。
    """
    out = []
    for name, codes in groups.items():
        cols = [c for c in codes if c in amount.columns]
        t_ser = amount.loc[today, cols] if (cols and today in amount.index) else pd.Series(dtype=float)
        y_ser = amount.loc[yday, cols] if (cols and yday in amount.index) else pd.Series(dtype=float)
        t = _num(t_ser.sum(min_count=1)) if len(t_ser) else None
        y = _num(y_ser.sum(min_count=1)) if len(y_ser) else None
        chg = ratio = None
        if t is not None and y:  # y 为 None / 0 都跳过
            chg = _round_half_up((t / y - 1) * 100)
            ratio = _round_half_up(t / y * 100)
        out.append({
            "key": name,
            "name": name,
            "n": int(t_ser.notna().sum()) if len(t_ser) else 0,
            "today": t,
            "yday": y,
            "chg_pct": chg,
            "ratio": ratio,
        })
    return out


def tail_ratio(minutes: pd.DataFrame | None, start: str = "14:30") -> float | None:
    """尾盘占比 %：时分严格晚于 start 的成交额之和 ÷ 全天成交额之和 × 100。无数据 → None。"""
    if minutes is None or len(minutes) == 0:
        return None
    hm = pd.to_datetime(minutes["trade_time"]).dt.strftime("%H:%M")
    amt = pd.to_numeric(minutes["amount"], errors="coerce")
    ok = amt.notna()
    total = float(amt[ok].sum())
    if not ok.any() or total <= 0:
        return None
    tail = float(amt[ok & (hm > start)].sum())
    return tail / total * 100


def fingerprint_conclusion(broad_chg: float | None, industry_chg: float | None) -> str:
    """按宽基十只、行业对照六只的成交变化 % 生成一句结论（PROMPT §4.5）。"""
    b, i = _num(broad_chg), _num(industry_chg)
    if b is None or i is None:
        return "数据不足，暂无法判断宽基与行业 ETF 的成交变化。"
    if b < -10 and i > 0:
        return (f"宽基通道明显降温、行业 ETF 反而 {_signed(i)}%——托底的钱在收手，进攻的钱在换标的。"
                "但成交降温 ≠ 净赎回，方向要等份额回填才能定。")
    if b > 10 and i < 0:
        return (f"宽基通道放量（{_signed(b)}%）、行业 ETF 反而 {_signed(i)}%——资金向宽基集中，"
                "留意是否有托底迹象。")
    if b > 10 and i > 10:
        return f"宽基（{_signed(b)}%）与行业 ETF（{_signed(i)}%）成交同步放大——ETF 通道整体活跃度上升。"
    if b < -10 and i < -10:
        return (f"宽基（{_signed(b)}%）与行业 ETF（{_signed(i)}%）成交同步萎缩——ETF 通道整体降温，"
                "但成交降温 ≠ 净赎回。")
    return f"宽基 {_signed(b)}%、行业 ETF {_signed(i)}%，两个通道成交变化不大。"


_ZH_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def ratio_footnote(groups: list[dict]) -> str:
    """比值条底注：只有 XX 越过了 100%，其余全部低于 100%；或 N 组全部低于 100%。"""
    valid = [g for g in groups if g.get("ratio") is not None]
    if not valid:
        return ""
    over = [g for g in valid if g["ratio"] > 100]
    n = len(valid)
    if not over:
        if any(g["ratio"] == 100 for g in valid):
            return f"{_ZH_NUM.get(n, n)}组均未越过 100%"
        return f"{_ZH_NUM.get(n, n)}组全部低于 100%"
    names = "、".join(str(g.get("name") or g.get("key")) for g in over)
    pcts = "、".join(f"+{_round_half_up(g['ratio'] - 100)}%" for g in over)
    if len(over) == n:
        return f"{_ZH_NUM.get(n, n)}组全部越过了 100%（{pcts}）"
    rest = "其余未超过 100%" if any(g["ratio"] == 100 for g in valid) else "其余全部低于 100%"
    return f"只有{names}越过了 100%（{pcts}），{rest}"
