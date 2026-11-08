"""估值·筹码页的纯计算：PB 分位、套牢盘汇总、融资杠杆与买入占比。只展示，不参与流动性打分。"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import metrics_sentiment as ms

BOARDS = ("沪深主板", "创业板", "科创板")


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def _r(x, nd):
    v = _f(x)
    return None if v is None else round(v, nd)


def percentile_rank(series: pd.Series, value: float) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna() if series is not None else pd.Series(dtype=float)
    v = _f(value)
    if s.empty or v is None:
        return None
    return float((s <= v).mean() * 100)


def pb_rows(frames: dict, names: list, start: str) -> list[dict]:
    rows = []
    limit = (datetime.strptime(start, "%Y%m%d") + timedelta(days=30)).strftime("%Y%m%d")
    for code, name in names:
        row = {"code": code, "name": name, "pb": None, "pct": None, "since": None, "full": False}
        df = frames.get(code) if isinstance(frames, dict) else None
        try:
            if isinstance(df, pd.DataFrame) and not df.empty and {"trade_date", "pb"} <= set(df.columns):
                d = df.assign(trade_date=df["trade_date"].astype(str),
                              pb=pd.to_numeric(df["pb"], errors="coerce")).dropna(subset=["pb"])
                d = d.drop_duplicates("trade_date", keep="last").sort_values("trade_date")
                d = d[d["trade_date"] >= start]
                if not d.empty:
                    last = float(d["pb"].iloc[-1])
                    row["pb"] = round(last, 2)
                    row["pct"] = _r(percentile_rank(d["pb"], last), 1)
                    row["since"] = d["trade_date"].iloc[0]
                    row["full"] = row["since"] <= limit
        except Exception:  # noqa: BLE001  单个指数异常不影响其它行
            row.update(pb=None, pct=None, since=None, full=False)
        rows.append(row)
    return rows


def level(avg, low, high, labels=("偏低", "中等", "偏高")) -> str | None:
    v = _f(avg)
    if v is None:
        return None
    return labels[0] if v < low else labels[2] if v > high else labels[1]


def board_of(ts_code) -> str | None:
    if not isinstance(ts_code, str) or "." not in ts_code:
        return None
    num, suf = ts_code.split(".", 1)
    if suf == "SH":
        return "科创板" if num[:3] in ("688", "689") else "沪深主板"
    if suf == "SZ":
        return "创业板" if num[:3] in ("300", "301", "302") else "沪深主板"
    return None


def chips_summary(win, mv, deep) -> dict:
    out = {"trapped": None, "boards": [{"name": n, "trapped": None} for n in BOARDS], "deep_share": None}

    def trapped(codes):
        p = ms.profit_ratio(win, mv, codes)
        return None if p is None else round(100 - round(p, 1), 1)

    out["trapped"] = trapped(None)
    if isinstance(win, pd.DataFrame) and not win.empty and "ts_code" in win.columns:
        codes = win["ts_code"].astype(str)
        board = codes.map(board_of)
        for b in out["boards"]:
            b["trapped"] = trapped(set(codes[board == b["name"]]))
        w = pd.to_numeric(win["winner_rate"], errors="coerce")[board.notna()].dropna()
        if len(w):
            out["deep_share"] = round(float((w < deep).mean() * 100), 1)
    return out


def _empty_margin() -> dict:
    return {"date": None, "rzye": None, "lev": None, "lev_pct": None, "buy_ratio": None, "buy_pct": None,
            "drawdown": None, "net5": None, "verdict": None, "lev_hist": {"dates": [], "values": []}}


def empty_margin() -> dict:
    return _empty_margin()


def margin_metrics(m, s, cfg: dict) -> dict:
    out = _empty_margin()
    need_m, need_s = {"trade_date", "rzye", "rzmre"}, {"trade_date", "float_mv", "amount"}
    if (not isinstance(m, pd.DataFrame) or not isinstance(s, pd.DataFrame) or m.empty or s.empty
            or not need_m <= set(m.columns) or not need_s <= set(s.columns)):
        return out
    mm = m[["trade_date", "rzye", "rzmre"]].assign(trade_date=m["trade_date"].astype(str))
    ss = s[["trade_date", "float_mv", "amount"]].assign(trade_date=s["trade_date"].astype(str))
    j = (mm.merge(ss, on="trade_date").drop_duplicates("trade_date", keep="last")
         .sort_values("trade_date").reset_index(drop=True))
    for c in ("rzye", "rzmre", "float_mv", "amount"):
        j[c] = pd.to_numeric(j[c], errors="coerce")
    j = j[(j["float_mv"] > 0) & (j["amount"] > 0)].dropna().reset_index(drop=True)
    if j.empty:
        return out
    lev = j["rzye"] / j["float_mv"] * 100
    buy = (j["rzmre"] / j["amount"] * 100).rolling(cfg["buy_ma"], min_periods=cfg["buy_ma"]).mean()
    out["date"] = j["trade_date"].iloc[-1]
    out["rzye"] = _r(j["rzye"].iloc[-1], 0)
    out["lev"] = _r(lev.iloc[-1], 3)
    out["lev_pct"] = _r(percentile_rank(lev, lev.iloc[-1]), 1)
    b_last = _f(buy.iloc[-1])
    if b_last is not None:
        out["buy_ratio"] = round(b_last, 2)
        out["buy_pct"] = _r(percentile_rank(buy, b_last), 1)
    tail = j["rzye"].tail(cfg["dd_window"])
    peak = tail.max()
    if peak > 0:
        out["drawdown"] = _r((j["rzye"].iloc[-1] / peak - 1) * 100, 1)
    if len(j) >= 6:
        out["net5"] = _r(j["rzye"].iloc[-1] - j["rzye"].iloc[-6], 0)
    lp, bp = out["lev_pct"], out["buy_pct"]
    if lp is not None and bp is not None:
        out["verdict"] = ("干净" if lp < cfg["margin_clean"] and bp < cfg["margin_clean"]
                          else "拥挤" if lp > cfg["margin_crowd"] and bp > cfg["margin_crowd"] else "中性")
    h = lev.tail(250)
    out["lev_hist"] = {"dates": list(j["trade_date"].tail(250)), "values": [round(float(v), 3) for v in h]}
    return out
