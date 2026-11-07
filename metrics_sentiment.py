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


def strip_roman(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    return str(x).replace("Ⅰ", "").replace("Ⅱ", "").replace("Ⅲ", "")


def match_sectors(basic: pd.DataFrame, sw: pd.DataFrame | None,
                  recent_limit_codes: set[str], extra: list[dict] | None = None) -> tuple[dict[str, set[str]], str]:
    """板块 → 成分代码集合（含空集），以及口径名。sw 不可用（None/空）时降级为 stock_basic.industry。

    sw 口径：先去掉 Ⅰ/Ⅱ/Ⅲ，再要求关键词与 L2 或 L3 名称**完全相等**（不用 L1，也不做子串匹配，
    否则「白酒」会命中「非白酒」、「电池」会命中「光伏电池组件」「电池化学品」等）。
    """
    use_sw = sw is not None and len(sw) > 0
    if use_sw:
        sw = sw[_not_bj(sw["ts_code"])]
        l2 = [strip_roman(v) for v in sw["l2_name"]]
        l3 = [strip_roman(v) for v in sw["l3_name"]]
        codes = list(sw["ts_code"])
        labels = list(zip(l2, l3))
        label = "申万行业"
        key = "sw"
    else:
        b = basic[_not_bj(basic["ts_code"])]
        codes = list(b["ts_code"])
        labels = [strip_roman(v) for v in b["industry"]]
        label = "stock_basic.industry"
        key = "basic"
    out: dict[str, set[str]] = {}
    for sec in config.SECTORS:
        if sec.get("dynamic"):
            out[sec["name"]] = {c for c in recent_limit_codes if not str(c).upper().endswith(".BJ")}
            continue
        kws = sec[key]
        if use_sw:
            kwset = set(kws)
            out[sec["name"]] = {c for c, names in zip(codes, labels) if kwset.intersection(names)}
        else:
            out[sec["name"]] = {c for c, lab in zip(codes, labels) if any(k in lab for k in kws)}
    if use_sw:  # 热门行只在申万口径下加入；降级口径忽略
        for ex in extra or []:
            if ex["name"] in out:
                continue
            kwset = set(ex["sw"])
            out[ex["name"]] = {c for c, names in zip(codes, labels) if kwset.intersection(names)}
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
    retail = [f"{side}_{lv}_amount" for lv in config.RETAIL_LEVELS for side in ("buy", "sell")]
    return float(df[retail].to_numpy(dtype=float).sum() / total * 100)


def six_dim(five_score: float | None, market_conc: float | None) -> float | None:
    """六维 = (5 × 五维 + 全市场浓度) ÷ 6；任一缺失 → None。"""
    for v in (five_score, market_conc):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
    return (5 * five_score + market_conc) / 6


def concentration_wall(mf_today: pd.DataFrame, mf_yday: pd.DataFrame | None,
                       sectors_today: dict[str, set[str]],
                       sectors_yday: dict[str, set[str]] | None,
                       extra: list[dict] | None = None, hot_fixed: set[str] | None = None) -> list[dict]:
    """热力墙行（按 today 降序，today 缺失的排最后、gray）。

    color 在有 today 的行内按名次三等分：i < n/3 red，i < 2n/3 orange，否则 blue。
    n = 该板块成分中出现在今日 moneyflow 的数量。
    extra = 动态热门行（一律 hot、图标取其 icon）；hot_fixed = 被标热的固定板块名。
    """
    icons = {s["name"]: s["icon"] for s in config.SECTORS}
    hot_names = set(hot_fixed or ())
    for ex in extra or []:
        icons[ex["name"]] = ex.get("icon") or config.HOT_SECTORS["icon"]
        hot_names.add(ex["name"])
    present = set(mf_today["ts_code"]) if mf_today is not None and len(mf_today) else set()
    rows = []
    for name, codes in sectors_today.items():
        today = concentration(mf_today, codes)
        yday = None
        if mf_yday is not None and sectors_yday is not None and name in sectors_yday:
            yday = concentration(mf_yday, sectors_yday[name])
        rows.append(dict(icon=icons.get(name, ""), name=name, hot=name in hot_names,
                         n=len(set(codes) & present), today=today,
                         yday=yday, chg=(today - yday) if today is not None and yday is not None else None,
                         color="gray"))
    have = sorted((r for r in rows if r["today"] is not None), key=lambda r: -r["today"])
    miss = [r for r in rows if r["today"] is None]
    n = len(have)
    for i, r in enumerate(have):
        r["color"] = "red" if i < n / 3 else "orange" if i < 2 * n / 3 else "blue"
    return have + miss


# ---------------------------------------------------------------- 热门行业 / 获利盘

def hot_industries(sw_daily: pd.DataFrame | None, classify: pd.DataFrame | None, covered: set[str],
                   day: str, cfg: dict) -> list[dict]:
    """全市场申万 L2/L3 行业热度排名（成交放大为主、涨幅为辅），按分数降序，rank 从 1 起。

    covered=True：自身或父级 L2 名称（去罗马数字）在 covered 里。观测不足 recent+base 天、
    day 当日流通市值低于下限的行业不参与。
    """
    if (not isinstance(sw_daily, pd.DataFrame) or sw_daily.empty
            or not isinstance(classify, pd.DataFrame) or classify.empty):
        return []
    rd, bd = int(cfg["recent_days"]), int(cfg["base_days"])
    cl = classify.drop_duplicates("index_code").set_index("index_code")
    names = {c: strip_roman(n) for c, n in cl["industry_name"].items()}
    cl = cl[cl["level"].isin(["L2", "L3"])]
    df = sw_daily[sw_daily["ts_code"].isin(cl.index) & (sw_daily["trade_date"].astype(str) <= day)].copy()
    for col in ("pct_change", "amount", "float_mv"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    cand = []
    for code, g in df.groupby("ts_code"):
        g = g.drop_duplicates("trade_date", keep="last").sort_values("trade_date")
        last = g[g["trade_date"].astype(str) == day]
        if last.empty:
            continue
        mv = last["float_mv"].iloc[0]
        if not (pd.notna(mv) and mv >= cfg["min_float_mv"]):
            continue
        g = g.tail(rd + bd)
        if len(g) < rd + bd or g[["amount", "pct_change"]].isna().any().any():
            continue
        base = g["amount"].head(bd).mean()
        if not base > 0:
            continue
        ratio = float(g["amount"].tail(rd).mean() / base)
        pct_n = float((np.prod(1 + g["pct_change"].tail(rd).to_numpy() / 100) - 1) * 100)
        level, parent_code = cl.at[code, "level"], cl.at[code, "parent_code"]
        parent = names.get(parent_code, "") if level == "L3" else ""
        cand.append(dict(code=code, name=names[code], level=level, parent_code=parent_code, parent=parent,
                         amount_ratio=ratio, pct_n=pct_n,
                         covered=names[code] in covered or (parent != "" and parent in covered)))
    if not cand:
        return []
    f = pd.DataFrame(cand)
    f["score"] = cfg["w_amount"] * f["amount_ratio"].rank(pct=True) + cfg["w_pct"] * f["pct_n"].rank(pct=True)
    f = f.sort_values(["score", "code"], ascending=[False, True], kind="stable").reset_index(drop=True)
    f["rank"] = f.index + 1
    out = f.to_dict("records")
    for r in out:
        r["covered"] = bool(r["covered"])
    return out


def pick_dynamic(ranked: list[dict], n: int) -> list[dict]:
    """按分数取前 n 个未被固定板块覆盖、且与已选项互无父子关系的行业。"""
    chosen: list[dict] = []
    for r in ranked:
        if len(chosen) >= n:
            break
        if r["covered"]:
            continue
        if any(s["code"] == r.get("parent_code") or s.get("parent_code") == r["code"] for s in chosen):
            continue
        chosen.append(r)
    return chosen


def hot_fixed_names(ranked: list[dict], top_k: int, sectors: list[dict] | None = None) -> set[str]:
    """固定板块的 sw 关键词里任一行业（或其 L3 子行业）进全市场前 top_k → 该板块为热门。"""
    keys: set[str] = set()
    for r in ranked[:top_k]:
        keys.add(r["name"])
        if r.get("parent"):
            keys.add(r["parent"])
    return {s["name"] for s in (sectors if sectors is not None else config.SECTORS)
            if not s.get("dynamic") and keys.intersection(s["sw"])}


def profit_ratio(win: pd.DataFrame | None, mv: pd.DataFrame | None, codes: set[str] | None) -> float | None:
    """获利盘比例（%）= Σ winner_rate × circ_mv ÷ Σ circ_mv；剔除北交所，只用两者俱全且 circ_mv > 0 的个股。
    codes=None 为全市场；无可用个股 → None。"""
    if (not isinstance(win, pd.DataFrame) or not isinstance(mv, pd.DataFrame)
            or win.empty or mv.empty):
        return None
    w = win[["ts_code", "winner_rate"]].drop_duplicates("ts_code")
    m = mv[["ts_code", "circ_mv"]].drop_duplicates("ts_code")
    df = w.merge(m, on="ts_code")
    df = df[_not_bj(df["ts_code"])]
    if codes is not None:
        df = df[df["ts_code"].isin(codes)]
    df = df.assign(winner_rate=pd.to_numeric(df["winner_rate"], errors="coerce"),
                   circ_mv=pd.to_numeric(df["circ_mv"], errors="coerce"))
    df = df[df["winner_rate"].notna() & (df["circ_mv"] > 0)]
    if df.empty:
        return None
    return float((df["winner_rate"] * df["circ_mv"]).sum() / df["circ_mv"].sum())
