"""流动性、外围、板块流向、新闻的纯函数（SPEC §4.0 / §4.1）。

不做 IO（load_policy_ops 只读调用方给的 CSV 路径）。缺失值一律 NaN/None，绝不补 0。
输出均为 JSON 友好的 Python 原生类型（float / str / None），NaN 一律转 None。
红 = 流入，绿 = 流出（配色由模板负责，这里只给数值与方向）。
"""
from __future__ import annotations

import csv
import logging
import math
import re

import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)

_FLAG_TOOLS = ("互换便利", "再贷款")
_AMOUNT_RE = re.compile(r"\d+(\.\d+)?\s*(亿|万亿)")


def _num(x) -> float | None:
    """转成 Python float；None / NaN / inf / 无法转换 → None。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _fmt_date(idx) -> str:
    """卡片日期统一为 YYYYMMDD：Timestamp 索引格式化，字符串索引（trade_date）原样输出。"""
    if isinstance(idx, pd.Timestamp):
        return idx.strftime("%Y%m%d")
    return str(idx)


# ---------- 卡片 ----------

def make_card(key: str, label: str, series: pd.Series, unit: str, *, change: str = "diff",
              spark_n: int = config.SPARK_DAYS, note: str = "", tone: str | None = None,
              change_lag: int = 1) -> dict:
    """生成前端卡片：{key,label,value,unit,change,change_unit,date,spark,note,tone}。

    - value / date：最后一个非 NaN 点（date 统一 YYYYMMDD 字符串）。
    - change：相对最后点之前第 change_lag 个非 NaN 点（默认 1 = 前一点）；
      "diff" 为差值（change_unit = unit），"pct" 为百分比（change_unit = "%"，基数为 0 → None）。
    - spark：最近 spark_n 个非 NaN 值。全空 → value None、spark []、note「无数据」。
    """
    s = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    change_unit = "%" if change == "pct" else unit
    card = {"key": key, "label": label, "value": None, "unit": unit, "change": None,
            "change_unit": change_unit, "date": None, "spark": [], "note": note, "tone": tone}
    if s.empty:
        card["note"] = "无数据"
        return card

    last = float(s.iloc[-1])
    card["value"] = last
    card["date"] = _fmt_date(s.index[-1])
    card["spark"] = [float(v) for v in s.iloc[-spark_n:]] if spark_n > 0 else []

    lag = max(int(change_lag), 1)
    if len(s) > lag:
        ref = float(s.iloc[-1 - lag])
        if change == "pct":
            card["change"] = _num((last / ref - 1.0) * 100.0) if ref != 0 else None
        else:
            card["change"] = _num(last - ref)
    return card


# ---------- 流动性投票 ----------

def _vote(x, hi=None, lo=None, *, hi_incl=False, lo_incl=False, inverted=False):
    """通用三态判定：x 高于 hi → +1，低于 lo → -1，其余 0；inverted 时方向取反；None → None。"""
    if x is None:
        return None
    up = x >= hi if hi_incl else x > hi
    dn = x <= lo if lo_incl else x < lo
    d = 1 if up else (-1 if dn else 0)
    return -d if inverted else d


def _label(score: int, cfg: dict) -> str:
    if score >= cfg["loose"]:
        return "偏松"
    if score <= cfg["tight"]:
        return "偏紧"
    return "中性"


def liquidity_vote(turnover_ratio, margin_chg_pct, flow_5d, shibor_diff_bp, cfg: dict = config.VOTE) -> dict:
    """四信号投票（+1 松 / 0 / -1 紧）。None / NaN 信号 dir=None，不计票。

    ① 成交额 ÷ 前 20 日均额：> turnover_hi 松，< turnover_lo 紧
    ② 两融余额 5 日变化 %：> +margin_pct 松，< -margin_pct 紧
    ③ 大盘主力净流入 5 日累计：> 0 松，< 0 紧
    ④ Shibor 1W 较 20 日均值（bp）：<= -shibor_bp 松，>= +shibor_bp 紧（含边界）
    """
    t, m, f, sh = _num(turnover_ratio), _num(margin_chg_pct), _num(flow_5d), _num(shibor_diff_bp)
    miss = "数据缺失，不计票"
    signals = [
        {"name": "成交额", "dir": _vote(t, cfg["turnover_hi"], cfg["turnover_lo"]),
         "detail": miss if t is None else f"{t:.2f} 倍于前 20 日均额"},
        {"name": "两融余额", "dir": _vote(m, cfg["margin_pct"], -cfg["margin_pct"]),
         "detail": miss if m is None else f"近 5 日 {m:+.2f}%"},
        {"name": "主力净流入", "dir": _vote(f, 0, 0),
         "detail": miss if f is None else f"近 5 日累计 {f:+.1f} 亿"},
        {"name": "Shibor 1W", "dir": _vote(sh, cfg["shibor_bp"], -cfg["shibor_bp"],
                                           hi_incl=True, lo_incl=True, inverted=True),
         "detail": miss if sh is None else f"较 20 日均值 {sh:+.1f}bp"},
    ]
    score = sum(s["dir"] for s in signals if s["dir"] is not None)
    return {"score": score, "label": _label(score, cfg), "signals": signals}


# ---------- 外围 ----------

def fed_net_liquidity(walcl: pd.Series, tga: pd.Series, rrp: pd.Series) -> pd.Series:
    """美联储净流动性（十亿美元）= WALCL/1000 − WTREGEN/1000 − RRPONTSYD。

    WALCL / WTREGEN 为百万美元周频；RRP 为十亿美元日频，按 WALCL 日期 asof 对齐（取不晚于该日的最近有效值）。
    TGA 也 asof 对齐以容忍日期错位；任一分量缺失 → 该日 NaN（不当 0）。索引 = WALCL 的非空日期。
    """
    w = pd.to_numeric(walcl, errors="coerce").dropna().sort_index()
    if w.empty:
        return pd.Series([], dtype=float)

    def _align(s: pd.Series) -> pd.Series:
        s = pd.to_numeric(s, errors="coerce").dropna().sort_index()
        if s.empty:
            return pd.Series(np.nan, index=w.index)
        return pd.Series([s.asof(d) for d in w.index], index=w.index, dtype=float)

    return w / 1000.0 - _align(tga) / 1000.0 - _align(rrp)


def rate_expectation(dff, fed30d, band_bp: float = config.RATE_BAND_BP) -> dict:
    """联邦基金期货隐含利率 (100 − FED30D) 与有效利率 DFF 的差（bp）→ 降息 / 加息 / 按兵不动。"""
    d, f = _num(dff), _num(fed30d)
    if d is None or f is None:
        return {"effective": d, "implied": None, "diff_bp": None, "label": "无数据"}
    implied = 100.0 - f
    diff_bp = (implied - d) * 100.0
    if diff_bp < -band_bp:
        label = "市场预期降息"
    elif diff_bp > band_bp:
        label = "预期加息"
    else:
        label = "预期按兵不动"
    return {"effective": d, "implied": implied, "diff_bp": diff_bp, "label": label}


# ---------- 板块资金流向 ----------

def sector_flow_rank(df: pd.DataFrame, end: str, days: int, top: int = 10) -> dict:
    """取 ≤ end 的最近 days 个交易日，按 industry 汇总 net_amount（亿元）；pct 取 end 当日（无则 None）。

    返回 {inflow: 净额降序前 top, outflow: 净额升序前 top, days: 实际汇总的交易日数}，行 = {name, net, pct}。
    days 可能小于请求的 days（数据不足时）；无数据时返回不含 days 的空榜。
    净额缺失（NaN）的行业日不参与汇总，全缺的行业不入榜（不当 0）。
    """
    empty = {"inflow": [], "outflow": []}
    if df is None or len(df) == 0:
        return empty
    d = df.copy()
    d["trade_date"] = d["trade_date"].astype(str)
    d["net_amount"] = pd.to_numeric(d["net_amount"], errors="coerce")
    d = d[d["trade_date"] <= str(end)]
    dates = sorted(d["trade_date"].unique())[-max(int(days), 1):]
    win = d[d["trade_date"].isin(dates)]
    net = win.groupby("industry")["net_amount"].sum(min_count=1).dropna()
    if net.empty:
        return empty

    on_end = d[d["trade_date"] == str(end)].drop_duplicates("industry").set_index("industry")
    pct_map = pd.to_numeric(on_end["pct_change"], errors="coerce") if "pct_change" in on_end else pd.Series(dtype=float)

    def _row(name, v):
        return {"name": str(name), "net": float(v), "pct": _num(pct_map.get(name))}

    asc = net.sort_values(kind="stable")
    desc = net.sort_values(ascending=False, kind="stable")
    return {"inflow": [_row(n, v) for n, v in desc.head(top).items()],
            "outflow": [_row(n, v) for n, v in asc.head(top).items()],
            "days": len(dates)}


# ---------- 新闻 ----------

def _matcher(kws: list[str]):
    low = [k.casefold() for k in kws if k]
    return lambda text: any(k in text for k in low)


def filter_news(df: pd.DataFrame, kw_domestic: list[str], kw_external: list[str]) -> dict:
    """按标题关键词把 major_news 分为 {domestic, external}。

    - 两组都命中 → domestic。但「外围词包含国内词」的情形（如「英国央行」含「央行」）先把该外围词从
      标题里抹掉再判国内，使「英国央行加息」归外围而「央行与美联储」仍归国内。
    - 按 pub_time 倒序；同标题去重（保留较新）；关键词大小写不敏感。
    - 每条 {time, src, title, url, flag}；url 空 → None；标题原样透传（不 eval / format）。
    - flag：标题含(互换便利|再贷款)且含「数字+亿/万亿」→ 疑似新操作。
    """
    out = {"domestic": [], "external": []}
    need = {"pub_time", "title"}
    if df is None or len(df) == 0 or not need.issubset(df.columns):
        return out

    ext_masks = sorted({e.casefold() for e in kw_external
                        if e and any(k and k.casefold() in e.casefold() and k.casefold() != e.casefold()
                                     for k in kw_domestic)}, key=len, reverse=True)
    is_dom, is_ext = _matcher(kw_domestic), _matcher(kw_external)

    d = df.copy()
    # 丢弃 None/NaN 与空白时间；其余（含 Timestamp 等非 str）统一转 str，避免混合类型排序报错
    d = d[d["pub_time"].map(lambda v: not pd.isna(v) and str(v).strip() != "")]
    if d.empty:
        return out
    d["pub_time"] = d["pub_time"].map(lambda v: str(v).strip())
    d = d.sort_values("pub_time", ascending=False, kind="stable")
    seen = set()
    for r in d.itertuples(index=False):
        title = getattr(r, "title", None)
        if not isinstance(title, str) or not title.strip() or title.strip() in seen:
            continue
        seen.add(title.strip())
        low = title.casefold()
        masked = low
        for e in ext_masks:
            masked = masked.replace(e, " ")
        if is_dom(masked):
            group = "domestic"
        elif is_ext(low):
            group = "external"
        else:
            continue
        url, src = getattr(r, "url", None), getattr(r, "src", None)
        out[group].append({
            "time": r.pub_time,
            "src": src if isinstance(src, str) else "",
            "title": title,
            "url": url if isinstance(url, str) and url.strip() else None,
            "flag": any(t in title for t in _FLAG_TOOLS) and bool(_AMOUNT_RE.search(title)),
        })
    return out


# ---------- 政策工具表 ----------

_POLICY_COLS = ["date", "tool", "amount_yi", "cum_yi", "url", "note"]


def load_policy_ops(path: str) -> list[dict]:
    """读手工维护的 policy_ops.csv（CSV 列 date,tool,amount_yi,cum_yi,url,note）。

    输出行 {date, tool, amount, cum, url, note}：amount / cum 来自 amount_yi / cum_yi（亿元，float）。
    空值 → None；整行为空跳过；只有表头 → []。
    不抛异常（页面不能因一处坏输入崩）：文件缺失 / 编码错误（如 Excel 存成 GBK）/ CSV 格式错误 → [] 并 WARNING；
    某个数字非数字 / 非有限 → 该字段 None 并 WARNING（含文件与行号），整行保留。
    """
    rows = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for lineno, raw in enumerate(csv.DictReader(fh), start=2):
                vals = {c: (raw.get(c) or "").strip() or None for c in _POLICY_COLS}
                if all(v is None for v in vals.values()):
                    continue
                nums = {}
                for src_col, out_col in (("amount_yi", "amount"), ("cum_yi", "cum")):
                    v = vals[src_col]
                    if v is not None:
                        try:
                            v = float(v)
                            if not math.isfinite(v):
                                raise ValueError
                        except ValueError:
                            log.warning("policy_ops %s:%d 列 %s 不是有效数字: %r，置为空", path, lineno, src_col, v)
                            v = None
                    nums[out_col] = v
                rows.append({"date": vals["date"], "tool": vals["tool"], "amount": nums["amount"],
                             "cum": nums["cum"], "url": vals["url"], "note": vals["note"]})
    except (UnicodeDecodeError, csv.Error, OSError) as e:
        log.warning("policy_ops 读取失败 %s: %s: %s", path, type(e).__name__, e)
        return []
    return rows
