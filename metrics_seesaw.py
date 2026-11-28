"""A 股资金跷跷板：仅在双篮子全量样本的同一个交易日窗口上比较。"""
from __future__ import annotations

import math
from copy import deepcopy

import pandas as pd


WINDOWS = (1, 5, 20)
SIDES = ("cash", "tech")


def empty_payload(baskets, thresholds, *, asof=None, demo=False):
    return dict(status="demo" if demo else "unavailable", asof=_iso(asof) if asof else None,
                windows={str(n): None for n in WINDOWS}, history=[], timeline=_empty_timeline(),
                baskets=deepcopy(baskets), thresholds=dict(thresholds), etf_windows={})


TIMELINE_FIELDS = tuple(f"{side}_{field}" for side in SIDES for field in ("return_pct", "flow_amount", "flow_pct")) + (
    "return_spread_pct", "flow_spread_pct")


def _empty_timeline():
    """逐日时间轴（列式存储）：dates 为历史区间内全部交易日；各周期给出以该日为截止日的窗口，缺数据为 None。"""
    return dict(dates=[], **{str(n): dict(start=[], **{f: [] for f in TIMELINE_FIELDS}) for n in WINDOWS})


def _timeline_append(timeline, n, row):
    col = timeline[str(n)]
    col["start"].append(row["start"] if row else None)
    for f in TIMELINE_FIELDS:
        side, _, field = f.partition("_")
        value = (row[side][field] if side in SIDES else row[f]) if row else None
        col[f].append(None if value is None else round(value, 6))


def _iso(day):
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def _side_codes(baskets):
    """拒绝空子主题和重复 ETF；同一子主题的同一指数只投一票。"""
    codes = {}
    seen_etfs = set()
    for side in SIDES:
        themes, etfs = [], []
        if not baskets[side]:
            raise ValueError(f"空篮子: {side}")
        for theme in baskets[side]:
            members = theme["etfs"]
            if not members:
                raise ValueError(f"空子主题: {theme['key']}")
            themes.append(tuple(dict.fromkeys(e["index_code"] for e in members)))
            for e in members:
                code = e["code"]
                if not code or not e["index_code"] or code in seen_etfs:
                    raise ValueError(f"ETF/指数缺失或 ETF 重复入篮: {code}")
                seen_etfs.add(code)
                etfs.append(code)
        codes[side] = (themes, etfs)
    return codes


def _aligned(series, calendar):
    """按官方交易日对齐；缺点或重复的日期不填值，不按披露日期自行推断日历。"""
    if series is None:
        return pd.Series(index=calendar, dtype=float)
    clean = pd.to_numeric(series, errors="coerce")
    clean = clean[~clean.index.duplicated(keep=False)].reindex(calendar)
    return clean.where((clean > 0) & (clean < math.inf))


SPLIT_MIN_RATIO = 1.2     # 复权因子单日变动至少这么大才视为拆分/合并；更小的是分红
SPLIT_SHARE_TOLERANCE = 0.15  # 份额跳变与拆分倍数的对数偏差上限，余量留给当天的真实申赎


def _split_units(share, price, factor):
    """把份额、收盘价换算到样本首日的份额单位，使拆分/合并不被记成申赎。

    拆分在复权因子上表现为 t 日成倍跳变（价格自 t 日起按新单位报价），而份额可能在 t−1 日
    （登记日晚间）或 t 日才按新单位披露，故在这两天里找跳变倍数最接近的那天换算份额。
    找不到匹配的份额跳变则把 t−1、t 两天标为缺口，不猜测拆分日。分红只让因子小幅变动，
    份额与收盘价都不调整。返回 (share, price, 复权收盘价)；factor 为 None 时不做任何调整。
    """
    if factor is None:
        return share, price, price
    ratio = factor / factor.shift()
    price_mult = pd.Series(1.0, index=share.index)
    share_mult = pd.Series(1.0, index=share.index)
    bad = factor.isna()
    jumps = share / share.shift()
    for pos in range(1, len(ratio)):
        k = ratio.iloc[pos]
        if not math.isfinite(k) or abs(math.log(k)) < math.log(SPLIT_MIN_RATIO):
            continue
        price_mult.iloc[pos:] *= k
        candidates = [(abs(math.log(jumps.iloc[j] / k)), j) for j in (pos - 1, pos)
                      if j >= 1 and math.isfinite(jumps.iloc[j])]
        miss, j = min(candidates, default=(math.inf, None))
        if miss < SPLIT_SHARE_TOLERANCE:
            share_mult.iloc[j:] *= k
        else:
            bad.iloc[pos - 1:pos + 1] = True
    nan = float("nan")
    return (share.div(share_mult).mask(bad, nan), price.mul(price_mult).mask(bad, nan),
            price.mul(factor).mask(bad, nan))


def calculate(calendar, history_start, baskets, thresholds, index_closes, etf_shares, etf_closes, etf_amounts=None,
              etf_factors=None):
    """calendar 为 YYYYMMDD 开市日，须含历史起点之前的预热日；各输入为同日期索引的 Series 字典。

    N 日窗口使用 t−N 的基期至 t（N+1 个收盘日）。涨跌幅取标的价格指数端点之比，先等权合成
    子主题、再等权合成篮子。逐 ETF 用每日份额变动（亿份）×当日收盘价（元）估算申赎金额（亿元），
    累加后除以完全相同的 ETF 在基期的份额×收盘规模。收益、流入比例及其科技−现金差均以百分点表示。
    etf_factors 为复权因子；给出时先按 _split_units 剔除拆分，缺某只 ETF 的因子视同缺数据。
    """
    days = sorted(set(calendar))
    out = empty_payload(baskets, thresholds, asof=days[-1] if days else None)
    codes = _side_codes(baskets)
    if not days:
        return out
    index_codes = dict.fromkeys(code for themes, _ in codes.values() for theme in themes for code in theme)
    etf_codes = [code for _, etfs in codes.values() for code in etfs]
    # 全部原始观测均须为正值、有限；一个交易日任一代理缺数据会使覆盖该日的窗口留空。
    data = pd.DataFrame({("index", code): _aligned(index_closes.get(code), days) for code in index_codes})
    adjusted = {}
    for code in etf_codes:
        factor = None if etf_factors is None else _aligned(etf_factors.get(code), days)
        share, price, adjusted[code] = _split_units(
            _aligned(etf_shares.get(code), days), _aligned(etf_closes.get(code), days), factor)
        data["share", code] = share
        data["price", code] = price
    bad_days = data.isna().any(axis=1).astype(int).cumsum().to_list()
    levels = data["index"].to_dict("list")
    shares = data["share"]
    prices = data["price"]
    flows = (shares.diff() * prices).to_dict("list")
    scales = (shares * prices).to_dict("list")

    def window(end, n):
        start = end - n
        if start < 0 or bad_days[end] != (bad_days[start - 1] if start else 0):
            return None
        row = dict(days=n, start=_iso(days[start + 1]), end=_iso(days[end]), base_date=_iso(days[start]))
        for side, (themes, etfs) in codes.items():
            theme_returns = [sum(levels[k][end] / levels[k][start] - 1 for k in theme) / len(theme)
                             for theme in themes]
            flow = sum(sum(flows[k][start + 1:end + 1]) for k in etfs)
            scale = sum(scales[k][start] for k in etfs)
            if not math.isfinite(scale) or scale <= 0:
                return None
            values = dict(return_pct=100 * sum(theme_returns) / len(theme_returns),
                          flow_amount=flow, flow_pct=100 * flow / scale)
            if not all(map(math.isfinite, values.values())):
                return None
            row[side] = values
        for key in ("return_pct", "flow_pct"):
            spread = row["tech"][key] - row["cash"][key]
            if not math.isfinite(spread):
                return None
            row[key.replace("_pct", "_spread_pct")] = spread
        return row

    # 每个周期独立向前找最新完整窗口；历史上的缺口不能改用较早的一天，也不能跳过缺失交易日。
    for end, day in enumerate(days):
        if day < history_start:
            continue
        out["timeline"]["dates"].append(_iso(day))
        for n in WINDOWS:
            row = window(end, n)
            _timeline_append(out["timeline"], n, row)
            if row is not None:
                out["windows"][str(n)] = row
            if n == 5:
                out["history"].append(dict(
                    date=_iso(day),
                    cash_return_pct=row["cash"]["return_pct"] if row else None,
                    tech_return_pct=row["tech"]["return_pct"] if row else None,
                    cash_flow_pct=row["cash"]["flow_pct"] if row else None,
                    tech_flow_pct=row["tech"]["flow_pct"] if row else None,
                    return_spread_pct=row["return_spread_pct"] if row else None,
                    flow_spread_pct=row["flow_spread_pct"] if row else None,
                ))
    raw_closes = {code: _aligned(etf_closes.get(code), days) for code in etf_codes}
    amounts = {}
    for code in etf_codes:
        raw = (etf_amounts or {}).get(code)
        if raw is not None:
            clean = pd.to_numeric(raw, errors="coerce")
            clean = clean[~clean.index.duplicated(keep=False)].reindex(days)
            amounts[code] = clean.where((clean >= 0) & (clean < math.inf))

    for n in WINDOWS:
        key = str(n)
        comparable = out["windows"][key]
        end = days.index(comparable["end"].replace("-", "")) if comparable else len(days) - 1
        start = end - n
        if start < 0:
            continue
        members = {}
        for code in etf_codes:
            close = raw_closes[code].iloc[end]
            gain = adjusted[code].iloc[end] / adjusted[code].iloc[start]
            flow = sum(flows[code][start + 1:end + 1])
            scale = scales[code][start]
            turnover = amounts[code].iloc[start + 1:end + 1].sum(min_count=n) if code in amounts else math.nan
            values = dict(close=close, return_pct=100 * (gain - 1),
                          flow_amount=flow, flow_pct=100 * flow / scale if scale > 0 else math.nan,
                          turnover=turnover)
            members[code] = {field: float(value) if math.isfinite(value) else None
                             for field, value in values.items()}
        out["etf_windows"][key] = dict(start=_iso(days[start + 1]), end=_iso(days[end]), members=members)

    if out["windows"]["1"] is not None:
        out["status"] = "ok"
    return out
