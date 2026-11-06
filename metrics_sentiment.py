"""市场体温与板块韭菜浓度纯函数（PROMPT §4.1 / §4.2）。

不做 IO。缺失值一律 NaN/None，绝不补 0；剔除北交所（.BJ）。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

import config

_MF_COLS = ["buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
            "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount"]
_RETAIL_COLS = _MF_COLS[:4]
_TEMP_COLS = ["up_ratio", "median", "limit", "volume", "profit", "score"]
MIN_VOLUME_HISTORY = 5


def _not_bj(codes: pd.Series) -> pd.Series:
    return ~codes.astype(str).str.upper().str.endswith(".BJ")


def _clip(x: float) -> float:
    return float(min(100.0, max(0.0, x)))


def limit_threshold(ts_code: str, name: str | None) -> float:
    """涨跌停阈值（%）：含 ST → 5；300/301/688/689 开头 → 20；否则 10。"""
    if name and "ST" in str(name).upper():
        return 5
    if str(ts_code)[:3] in ("300", "301", "688", "689"):
        return 20
    return 10


def limit_flags(daily: pd.DataFrame, names: dict[str, str]) -> pd.DataFrame:
    """剔除 .BJ 后加 up_limit / down_limit 布尔列（pct_chg 缺失 → 两者皆 False）。"""
    df = daily[_not_bj(daily["ts_code"])].copy()
    thr = np.array([limit_threshold(c, names.get(c)) for c in df["ts_code"]], dtype=float)
    pct = pd.to_numeric(df["pct_chg"], errors="coerce").to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        df["up_limit"] = pct >= thr - 0.2
        df["down_limit"] = pct <= -(thr - 0.2)
    return df


def temperature_series(daily_by_date: dict[str, pd.DataFrame], names: dict[str, str], ma: int = 20) -> pd.DataFrame:
    """逐日五维体温。历史不足 MIN_VOLUME_HISTORY 日的交易日整日不输出。

    score = 可得维度的等权平均（某维度无法计算为 NaN 时跳过，不当 0）。
    """
    dates = sorted(daily_by_date)
    frames = {d: limit_flags(daily_by_date[d], names) for d in dates}
    totals = {d: pd.to_numeric(frames[d]["amount"], errors="coerce").sum(min_count=1) for d in dates}
    rows = {}
    for i, d in enumerate(dates):
        if i < MIN_VOLUME_HISTORY:
            continue
        df = frames[d]
        pct = pd.to_numeric(df["pct_chg"], errors="coerce")
        valid = pct.notna()
        n = int(valid.sum())
        up_ratio = float((pct[valid] > 0).sum() / n * 100) if n else np.nan
        median = _clip(50 + float(pct[valid].median()) * 12.5) if n else np.nan
        n_up, n_dn = int(df["up_limit"].sum()), int(df["down_limit"].sum())
        limit = 50.0 if n_up + n_dn == 0 else n_up / (n_up + n_dn) * 100
        hist = pd.Series([totals[x] for x in dates[max(0, i - ma):i]], dtype=float).dropna()
        base = float(hist.mean()) if len(hist) else np.nan
        today_amt = totals[d]
        volume = (_clip(50 + (float(today_amt) / base - 1) * 100)
                  if pd.notna(today_amt) and pd.notna(base) and base > 0 else np.nan)
        prev = frames[dates[i - 1]]
        yl = set(prev.loc[prev["up_limit"], "ts_code"])
        today_pct = pd.Series(pct.to_numpy(), index=df["ts_code"].to_numpy())
        today_pct = today_pct[~today_pct.index.duplicated()]
        got = today_pct.reindex(list(yl)).dropna()
        profit = float((got > 0).sum() / len(got) * 100) if len(got) else np.nan
        dims = [up_ratio, median, limit, volume, profit]
        avail = [x for x in dims if not (x is None or math.isnan(x))]
        score = float(np.mean(avail)) if avail else np.nan
        rows[d] = dict(up_ratio=up_ratio, median=median, limit=limit, volume=volume, profit=profit, score=score)
    return pd.DataFrame.from_dict(rows, orient="index", columns=_TEMP_COLS).astype(float)


def _delta_text(delta: float) -> str:
    if abs(delta) < 2:
        return "几乎没动"
    return "明显升温" if delta > 0 else "明显降温"


def _dev_text(dev: float) -> str:
    if dev >= 15:
        return "明显偏热"
    if dev >= 5:
        return "偏热"
    if dev > -5:
        return "接近均值"
    if dev > -15:
        return "偏冷"
    return "明显偏冷"


def temperature_kpis(series: pd.DataFrame, window: int = 30) -> dict:
    """4 张 KPI 卡的数值与文案；窗口取 series 最后 window 期（含今日）。

    rank = 窗口内严格高于今日的期数 + 1；pct_above = 窗口内严格低于今日的期数 / n × 100。
    """
    s = series["score"].dropna().tail(window)
    if s.empty:
        return {}
    today = float(s.iloc[-1])
    yday = float(s.iloc[-2]) if len(s) >= 2 else None
    delta = today - yday if yday is not None else None
    mean = float(s.mean())
    dev = today - mean
    n = len(s)
    return dict(
        today=today, yday=yday, delta=delta,
        delta_text=_delta_text(delta) if delta is not None else None,
        mean=mean, dev=dev, dev_text=_dev_text(dev),
        rank=int((s > today).sum()) + 1, n=n,
        pct_above=float((s < today).sum() / n * 100),
        max=float(s.max()), max_date=s.idxmax(),
    )


def _strip_roman(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    return str(x).replace("Ⅰ", "").replace("Ⅱ", "").replace("Ⅲ", "")


def match_sectors(basic: pd.DataFrame, sw: pd.DataFrame | None,
                  recent_limit_codes: set[str]) -> tuple[dict[str, set[str]], str]:
    """板块 → 成分代码集合（含空集），以及口径名。sw 不可用（None/空）时降级为 stock_basic.industry。"""
    use_sw = sw is not None and len(sw) > 0
    if use_sw:
        sw = sw[_not_bj(sw["ts_code"])]
        levels = [[_strip_roman(v) for v in sw[c]] for c in ("l1_name", "l2_name", "l3_name")]
        codes = list(sw["ts_code"])
        labels = ["|".join(parts) for parts in zip(*levels)]  # 仅用于子串匹配，分隔符避免跨层误匹配
        label = "申万行业"
        key = "sw"
    else:
        b = basic[_not_bj(basic["ts_code"])]
        codes = list(b["ts_code"])
        labels = [_strip_roman(v) for v in b["industry"]]
        label = "stock_basic.industry"
        key = "basic"
    out: dict[str, set[str]] = {}
    for sec in config.SECTORS:
        if sec.get("dynamic"):
            out[sec["name"]] = {c for c in recent_limit_codes if not str(c).upper().endswith(".BJ")}
            continue
        kws = sec[key]
        out[sec["name"]] = {c for c, lab in zip(codes, labels) if any(k in lab for k in kws)}
    return out, label


def concentration(mf: pd.DataFrame, codes: set[str] | None = None) -> float | None:
    """金额加权韭菜浓度（%）= Σ(买+卖)(小+中) ÷ Σ(买+卖)(四档) × 100；无成分/无数据 → None。"""
    if mf is None or len(mf) == 0:
        return None
    df = mf[_not_bj(mf["ts_code"])]
    if codes is not None:
        df = df[df["ts_code"].isin(codes)]
    df = df.dropna(subset=_MF_COLS)  # 任一档缺失 → 整只剔除，保证分子分母同口径
    if df.empty:
        return None
    total = float(df[_MF_COLS].to_numpy(dtype=float).sum())
    if total <= 0:
        return None
    return float(df[_RETAIL_COLS].to_numpy(dtype=float).sum() / total * 100)


def six_dim(five_score: float | None, market_conc: float | None) -> float | None:
    """六维 = (5 × 五维 + 全市场浓度) ÷ 6；任一缺失 → None。"""
    for v in (five_score, market_conc):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
    return (5 * five_score + market_conc) / 6


def concentration_wall(mf_today: pd.DataFrame, mf_yday: pd.DataFrame | None,
                       sectors_today: dict[str, set[str]],
                       sectors_yday: dict[str, set[str]] | None) -> list[dict]:
    """热力墙行（按 today 降序，today 缺失的排最后、gray）。

    color 在有 today 的行内按名次三等分：i < n/3 red，i < 2n/3 orange，否则 blue。
    n = 该板块成分中出现在今日 moneyflow 的数量。
    """
    icons = {s["name"]: s["icon"] for s in config.SECTORS}
    present = set(mf_today["ts_code"]) if mf_today is not None and len(mf_today) else set()
    rows = []
    for name, codes in sectors_today.items():
        today = concentration(mf_today, codes)
        yday = None
        if mf_yday is not None and sectors_yday is not None and name in sectors_yday:
            yday = concentration(mf_yday, sectors_yday[name])
        rows.append(dict(icon=icons.get(name, ""), name=name, n=len(set(codes) & present), today=today,
                         yday=yday, chg=(today - yday) if today is not None and yday is not None else None,
                         color="gray"))
    have = sorted((r for r in rows if r["today"] is not None), key=lambda r: -r["today"])
    miss = [r for r in rows if r["today"] is None]
    n = len(have)
    for i, r in enumerate(have):
        r["color"] = "red" if i < n / 3 else "orange" if i < 2 * n / 3 else "blue"
    return have + miss
