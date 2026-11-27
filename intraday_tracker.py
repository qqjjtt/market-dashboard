"""盘中板块 ETF 与强势股分时追踪引擎（东方财富主源 → 同花顺备用 → 腾讯实时行情/分时兜底）。

核心能力：
1. 监控 config.INTRADAY_ETFS 中的 24 只主流行业/主题 ETF：
   - 实时日 K 快照（开高低收、昨收、实时涨跌幅 %、累计成交额亿元、东财板块/ETF主力净流入亿元）
   - 241 个 1 分钟分时序列（09:30–11:30、13:00–15:00）：
     * `pct`：相对昨收的每分钟涨跌幅 % 曲线
     * `flow`：按分钟方向脉冲与东财/同花顺主力净额校准的日内累计资金流（亿元）曲线
     * `amt`：日内累计成交额（亿元）曲线
2. 每个行业 ETF 穿透其权重大市值股（Top 4）：
   - 先取基金披露的前十大重仓，再按总市值挑出其中的大市值权重股；披露缺失时退回配置候选池，只按总市值排序
   - 为每只权重股拉取实时日 K（开高低收、涨跌幅、成交额、主力净额）与当日 1 分钟分时涨跌幅曲线
3. 顶部全市场盘中概览 + 右上角「预测量能 vs 昨日」分时曲线：
   - 沪深两市（上证 `000001.SH` + 深成指 `399001.SZ`）今日与昨日每分钟累计成交额对比
   - 按历史同刻累计成交占比外推今日全天预测量能（万亿元）及每分钟同比昨日量能变化 %
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import re
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

import config

log = logging.getLogger("intraday_tracker")
SH = ZoneInfo("Asia/Shanghai")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")
TIMEOUT = 8


def build_minute_grid() -> list[str]:
    """标准 A 股 241 个分时点：09:30..11:30 (121) + 13:01..15:00 (120)。"""
    grid = [f"{h:02d}:{m:02d}" for h in range(9, 12) for m in range(60)
            if "09:30" <= f"{h:02d}:{m:02d}" <= "11:30"]
    grid += [f"{h:02d}:{m:02d}" for h in range(13, 16) for m in range(60)
             if "13:00" < f"{h:02d}:{m:02d}" <= "15:00"]
    return grid


MINUTE_GRID = build_minute_grid()
_GRID_IDX = {hm: i for i, hm in enumerate(MINUTE_GRID)}


def _http_get(url: str, referer: str | None = None, encoding: str = "utf-8", timeout: float = TIMEOUT) -> str:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(encoding, errors="replace")


def to_secid(code: str) -> str:
    """512480.SH → 1.512480；159755.SZ → 0.159755；BK1036 → 90.BK1036。"""
    c = str(code).strip().upper()
    if c.startswith("BK"):
        return f"90.{c}"
    parts = c.split(".")
    num = parts[0]
    ex = parts[1] if len(parts) > 1 else ("SH" if num.startswith(("5", "6")) else "SZ")
    return f"{1 if ex == 'SH' else 0}.{num}"


def to_tx_sym(code: str) -> str:
    """512480.SH → sh512480；159755.SZ → sz159755。"""
    parts = str(code).strip().upper().split(".")
    num = parts[0]
    ex = parts[1] if len(parts) > 1 else ("SH" if num.startswith(("5", "6")) else "SZ")
    return f"{'sh' if ex == 'SH' else 'sz'}{num}"


def _safe_float(v, default=None) -> float | None:
    if v is None:
        return default
    try:
        x = float(v)
        return default if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 批量实时日 K 快照（东财主 → 腾讯兜底）

def fetch_em_batch_quotes(codes: list[str], http_get=_http_get) -> dict[str, dict]:
    """东方财富 ulist.np/get 批量获取 ETF/个股/板块/指数的实时日 K 与主力净流入。
    返回 {code_or_bk: {name, price, pre_close, open, high, low, pct, amount_yi, net_inflow_yi, up_count, down_count}}。"""
    if not codes:
        return {}
    secid_map = {to_secid(c): c for c in codes}
    secids = list(secid_map.keys())
    out: dict[str, dict] = {}
    for i in range(0, len(secids), 70):
        chunk = ",".join(secids[i:i + 70])
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
               "&fields=f12,f13,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f20,f21,f62,f104,f105,f106"
               f"&secids={chunk}")
        try:
            txt = http_get(url, referer="https://quote.eastmoney.com/")
            diff = ((json.loads(txt).get("data") or {}).get("diff")) or []
        except Exception:  # noqa: BLE001
            continue
        for row in diff:
            f12 = str(row.get("f12") or "")
            f13 = row.get("f13")
            sid = f"{f13}.{f12}"
            orig = secid_map.get(sid) or f12
            price = _safe_float(row.get("f2"))
            pre = _safe_float(row.get("f18"))
            pct = _safe_float(row.get("f3"))
            if pct is None and price and pre and pre > 0:
                pct = round((price / pre - 1) * 100, 2)
            amt = _safe_float(row.get("f6"), 0.0) or 0.0
            f62 = _safe_float(row.get("f62"))
            f20 = _safe_float(row.get("f20"))  # 总市值（元）
            out[orig] = {
                "code": orig,
                "name": str(row.get("f14") or orig),
                "price": price,
                "pre_close": pre,
                "open": _safe_float(row.get("f17")) or pre,
                "high": _safe_float(row.get("f15")) or price,
                "low": _safe_float(row.get("f16")) or price,
                "pct": pct if pct is not None else 0.0,
                "amount_yi": round(amt / 1e8, 3),
                "net_inflow_yi": round(f62 / 1e8, 3) if f62 is not None else None,
                "mv_yi": round(f20 / 1e8, 1) if f20 is not None else None,
                "up_count": int(row["f104"]) if isinstance(row.get("f104"), (int, float)) else None,
                "down_count": int(row["f105"]) if isinstance(row.get("f105"), (int, float)) else None,
                "source": "东财",
            }
    return out


def fetch_tx_batch_quotes(codes: list[str], http_get=_http_get) -> dict[str, dict]:
    """腾讯 qt.gtimg.cn 批量获取实时日 K（东财连接被重置时的可靠后备）。"""
    sym_map = {to_tx_sym(c): c for c in codes if not str(c).upper().startswith("BK")}
    syms = list(sym_map.keys())
    out: dict[str, dict] = {}
    for i in range(0, len(syms), 60):
        chunk = ",".join(syms[i:i + 60])
        url = f"https://qt.gtimg.cn/q={chunk}"
        try:
            txt = http_get(url, encoding="gbk")
        except Exception:  # noqa: BLE001
            continue
        for line in txt.strip().split(";"):
            if "=" not in line:
                continue
            k, val = line.strip().split("=", 1)
            sym = k.replace("v_", "").strip()
            orig = sym_map.get(sym)
            if not orig:
                continue
            f = val.strip('"').split("~")
            if len(f) <= 38 or not f[3]:
                continue
            price = _safe_float(f[3])
            pre = _safe_float(f[4])
            pct = _safe_float(f[32])
            if pct is None and price and pre and pre > 0:
                pct = round((price / pre - 1) * 100, 2)
            # f[37] 为成交额（万元），f[45] 为总市值（亿元）
            amt_wan = _safe_float(f[37], 0.0) or 0.0
            mv_yi = _safe_float(f[45]) if len(f) > 45 else None
            out[orig] = {
                "code": orig,
                "name": f[1] or orig,
                "price": price,
                "pre_close": pre,
                "open": _safe_float(f[5]) or pre,
                "high": _safe_float(f[33]) or price,
                "low": _safe_float(f[34]) or price,
                "pct": pct if pct is not None else 0.0,
                "amount_yi": round(amt_wan / 1e4, 3),
                "net_inflow_yi": None,
                "mv_yi": round(mv_yi, 1) if mv_yi is not None else None,
                "up_count": None,
                "down_count": None,
                "source": "腾讯",
            }
    return out


def fetch_merged_quotes(codes: list[str], http_get=_http_get) -> dict[str, dict]:
    """优先东财批量取日 K + 主力净流入，缺失项用腾讯补齐。"""
    em = fetch_em_batch_quotes(codes, http_get=http_get)
    missing = [c for c in codes if c not in em and not str(c).upper().startswith("BK")]
    if missing:
        tx = fetch_tx_batch_quotes(missing, http_get=http_get)
        for k, v in tx.items():
            em.setdefault(k, v)
    return em


# ------------------------------------------------------------------ 单标的 1 分钟分时（东财主 → 同花顺备 → 腾讯备）

def _parse_em_intraday(txt: str) -> dict | None:
    data = (json.loads(txt).get("data")) or {}
    trends = data.get("trends") or []
    pre = _safe_float(data.get("preClose")) or _safe_float(data.get("prePrice"))
    if not trends:
        return None
    # 只取最新一天
    last_day = str(trends[-1]).split(",")[0][:10]
    bars = []
    for row in trends:
        parts = str(row).split(",")
        if len(parts) < 4 or not parts[0].startswith(last_day):
            continue
        hm = parts[0][11:16]
        price = _safe_float(parts[1]) if len(parts) == 4 else _safe_float(parts[2])
        amt = _safe_float(parts[-1], 0.0) or 0.0
        if price is not None:
            bars.append((hm, price, amt))
    if not bars:
        return None
    return {"date": last_day.replace("-", ""), "pre_close": pre, "bars": bars, "source": "东财"}


def _parse_ths_intraday(txt: str, num: str) -> dict | None:
    if "(" not in txt or ")" not in txt:
        return None
    payload = json.loads(txt[txt.index("(") + 1:txt.rindex(")")])
    node = payload.get(f"hs_{num}") or next(iter(payload.values()), {})
    date = str(node.get("date") or "")
    pre = _safe_float(node.get("pre"))
    raw = str(node.get("data") or "")
    if len(date) != 8 or not raw:
        return None
    bars = []
    for item in raw.split(";"):
        parts = item.split(",")
        if len(parts) < 3:
            continue
        hm_raw = parts[0].strip()
        if len(hm_raw) != 4 or not hm_raw.isdigit():
            continue
        hm = f"{hm_raw[:2]}:{hm_raw[2:]}"
        price = _safe_float(parts[1])
        amt = _safe_float(parts[2], 0.0) or 0.0
        if price is not None:
            bars.append((hm, price, amt))
    if not bars:
        return None
    return {"date": date, "pre_close": pre, "bars": bars, "source": "同花顺"}


def _parse_tx_intraday(txt: str, sym: str) -> dict | None:
    root = ((json.loads(txt).get("data") or {}).get(sym)) or {}
    day_list = root.get("data") or []
    if isinstance(day_list, dict):
        # minute/query 结构
        date = str(day_list.get("date") or "")
        rows = day_list.get("data") or []
        pre = None
    elif isinstance(day_list, list) and day_list:
        # day/query 5日结构，index 0 为最新日
        d0 = day_list[0]
        date = str(d0.get("date") or "")
        rows = d0.get("data") or []
        pre = _safe_float(d0.get("prec"))
    else:
        return None
    if not rows:
        return None
    bars = []
    prev_cum_amt = 0.0
    for r in rows:
        parts = str(r).strip().split(" ")
        if len(parts) < 4:
            continue
        hm_raw = parts[0]
        if len(hm_raw) != 4:
            continue
        hm = f"{hm_raw[:2]}:{hm_raw[2:]}"
        if hm > "15:00":
            continue
        price = _safe_float(parts[1])
        cum_amt = _safe_float(parts[3], prev_cum_amt) or prev_cum_amt
        delta_amt = max(0.0, cum_amt - prev_cum_amt)
        prev_cum_amt = cum_amt
        if price is not None:
            bars.append((hm, price, delta_amt))
    if not bars:
        return None
    return {"date": date, "pre_close": pre, "bars": bars, "source": "腾讯"}


def fetch_symbol_intraday(code: str, http_get=_http_get, prefer: str = "em") -> dict | None:
    """获取单只 ETF 或个股的当日 1 分钟分时：东财（主）→ 同花顺（备）→ 腾讯（兜底）。
    返回 {date: YYYYMMDD, pre_close: float|None, bars: [(HH:MM, price, minute_amount_yuan), ...], source: str}。"""
    secid = to_secid(code)
    num = str(code).split(".")[0]
    sym = to_tx_sym(code)

    def try_em():
        for host in ("push2.eastmoney.com", "push2his.eastmoney.com"):
            url = (f"https://{host}/api/qt/stock/trends2/get?"
                   f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
                   "&fields2=f51,f53,f56,f57&ndays=1&iscr=0")
            try:
                res = _parse_em_intraday(http_get(url, referer="https://quote.eastmoney.com/", timeout=4.5))
                if res:
                    return res
            except Exception:  # noqa: BLE001
                continue
        return None

    def try_ths():
        for ver, page in (("v4", "today.js"), ("v6", "last.js")):
            url = f"https://d.10jqka.com.cn/{ver}/time/hs_{num}/{page}"
            try:
                res = _parse_ths_intraday(http_get(url, referer="https://stockpage.10jqka.com.cn/", timeout=4.5), num)
                if res:
                    return res
            except Exception:  # noqa: BLE001
                continue
        return None

    def try_tx():
        url = f"https://web.ifzq.gtimg.cn/appstock/app/day/query?code={sym}"
        try:
            return _parse_tx_intraday(http_get(url, timeout=5.0), sym)
        except Exception:  # noqa: BLE001
            return None

    order = (try_em, try_ths, try_tx) if prefer == "em" else (try_ths, try_tx, try_em)
    for fn in order:
        res = fn()
        if res:
            return res
    return None


def align_intraday_curves(bars: list[tuple[str, float, float]], pre_close: float | None,
                          target_net_flow_yi: float | None = None) -> dict:
    """将 [(HH:MM, price, minute_amount_yuan), ...] 对齐到 MINUTE_GRID（截至最后一个有效分钟点）。

    生成三条等长分时曲线（长度 = n_points，未到的时间点不补）：
    - `pct`：相对昨收的每分钟涨跌幅 %（2 位小数）
    - `amt`：从 09:30 起算的日内累计成交额（亿元，2 位小数）
    - `flow`：从 09:30 起算的日内累计资金净流入（亿元，2 位小数）。
      计算方法：每分钟带符号成交脉冲 = sign(Δprice) × minute_amount；若已知东财/同花顺当前主力净流入总额
      `target_net_flow_yi`，则将累计脉冲曲线平滑缩放至终点等于 `target_net_flow_yi`，既保留真实的盘中放量拉升/跳水拐点，
      又与官方板块/ETF净流入总额精确对齐。
    """
    if not bars:
        return {"last_idx": -1, "pct": [], "amt": [], "flow": [], "prices": []}
    price_arr = [None] * len(MINUTE_GRID)
    amt_arr = [0.0] * len(MINUTE_GRID)
    max_idx = -1
    for hm, px, amt in bars:
        idx = _GRID_IDX.get(hm)
        if idx is None:
            if hm < "09:30":
                idx = 0
            elif "11:30" < hm <= "13:00":
                idx = _GRID_IDX["11:30"]
            elif hm > "15:00":
                idx = _GRID_IDX["15:00"]
            else:
                continue
        price_arr[idx] = float(px)
        amt_arr[idx] += float(amt or 0.0)
        if idx > max_idx:
            max_idx = idx
    if max_idx < 0:
        return {"last_idx": -1, "pct": [], "amt": [], "flow": [], "prices": []}

    first_px = next((p for p in price_arr if p is not None), None)
    base_px = float(pre_close) if (pre_close and pre_close > 0) else (first_px or 1.0)

    prices: list[float] = []
    pcts: list[float] = []
    cum_amts: list[float] = []
    raw_pulses: list[float] = []

    cur_px = first_px or base_px
    prev_px = base_px
    running_amt = 0.0
    for i in range(max_idx + 1):
        if price_arr[i] is not None:
            cur_px = price_arr[i]
        prices.append(round(cur_px, 4))
        pct = (cur_px / base_px - 1.0) * 100.0 if base_px > 0 else 0.0
        pcts.append(round(pct, 2))
        m_amt_yi = amt_arr[i] / 1e8
        running_amt += m_amt_yi
        cum_amts.append(round(running_amt, 2))

        d_pct = (cur_px / prev_px - 1.0) * 100.0 if prev_px > 0 else 0.0
        # 方向权重：价格上涨分钟记正流入，下跌记流出，横盘按当日总体方向微倾
        weight = math.tanh(d_pct * 3.5) if abs(d_pct) > 1e-6 else math.tanh(pct * 0.25) * 0.15
        raw_pulses.append(m_amt_yi * weight)
        prev_px = cur_px

    cum_flow = np.cumsum(raw_pulses)
    if target_net_flow_yi is not None and len(cum_flow) > 0:
        # 将分时脉冲累计线与官方主力净额平滑对齐：保留分时波动形态 + 线性残差校准至终点 target_net_flow_yi
        raw_end = float(cum_flow[-1])
        t_frac = np.linspace(1.0 / len(cum_flow), 1.0, len(cum_flow))
        if abs(raw_end) > 1e-4 and (raw_end * target_net_flow_yi) > 0:
            scale = min(3.5, max(0.25, target_net_flow_yi / raw_end))
            scaled = cum_flow * scale
            adj = scaled + (target_net_flow_yi - float(scaled[-1])) * t_frac
        else:
            adj = cum_flow + (target_net_flow_yi - raw_end) * t_frac
        flows = [round(float(x), 2) for x in adj]
    else:
        flows = [round(float(x), 2) for x in cum_flow]

    return {
        "last_idx": max_idx,
        "pct": pcts,
        "amt": cum_amts,
        "flow": flows,
        "prices": prices,
    }


# ------------------------------------------------------------------ 全市场量能预测（今日 vs 昨日每分钟）

def fetch_market_volume_curve(http_get=_http_get) -> dict:
    """用上证指数（sh000001）+ 深证成指（sz399001）近 2 日 1 分钟成交额合成全市场日内量能与预测量能曲线。
    返回：
    {
        "date": "20261127",
        "yday_date": "20261126",
        "today_total_yi": 8227.0,
        "yday_total_yi": 14900.0,
        "pred_total_yi": 14600.0,
        "pred_vs_yday_pct": -2.01,
        "curve_pct": [-8.1, -7.5, ..., -2.01],   # 每个已走分钟点的「预测量能 vs 昨日全天 %」
        "today_cum_yi": [...],
        "yday_cum_yi": [...],
    }
    """
    day_maps: dict[str, dict[str, list[float]]] = {}  # sym -> {date: [cum_amt_yi_per_grid]}
    for sym in ("sh000001", "sz399001"):
        url = f"https://web.ifzq.gtimg.cn/appstock/app/day/query?code={sym}"
        try:
            txt = http_get(url, timeout=5.0)
            days = (((json.loads(txt).get("data") or {}).get(sym) or {}).get("data")) or []
        except Exception:  # noqa: BLE001
            days = []
        sym_days: dict[str, list[float]] = {}
        for d_obj in days[:2]:
            dt = str(d_obj.get("date") or "")
            rows = d_obj.get("data") or []
            if not dt or not rows:
                continue
            arr = [None] * len(MINUTE_GRID)
            max_i = -1
            for r in rows:
                parts = str(r).strip().split(" ")
                if len(parts) < 4 or len(parts[0]) != 4:
                    continue
                hm = f"{parts[0][:2]}:{parts[0][2:]}"
                idx = _GRID_IDX.get(hm)
                if idx is None:
                    continue
                v = _safe_float(parts[3])
                if v is not None:
                    arr[idx] = v / 1e8  # 亿元
                    if idx > max_i:
                        max_i = idx
            if max_i >= 0:
                cur = 0.0
                filled = []
                for i in range(max_i + 1):
                    if arr[i] is not None:
                        cur = arr[i]
                    filled.append(cur)
                sym_days[dt] = filled
        day_maps[sym] = sym_days

    all_dates = sorted(set(day_maps.get("sh000001", {}).keys()) & set(day_maps.get("sz399001", {}).keys()), reverse=True)
    if not all_dates:
        return {
            "date": None, "yday_date": None,
            "today_total_yi": None, "yday_total_yi": None,
            "pred_total_yi": None, "pred_vs_yday_pct": None,
            "curve_pct": [], "today_cum_yi": [], "yday_cum_yi": [],
        }
    today_dt = all_dates[0]
    yday_dt = all_dates[1] if len(all_dates) > 1 else None

    t_sh, t_sz = day_maps["sh000001"][today_dt], day_maps["sz399001"][today_dt]
    n_t = min(len(t_sh), len(t_sz))
    today_cum = [round(t_sh[i] + t_sz[i], 1) for i in range(n_t)]
    today_total = today_cum[-1] if today_cum else 0.0

    yday_cum: list[float] = []
    yday_total = None
    curve_pct: list[float] = []
    pred_total = today_total
    pred_pct = None

    if yday_dt:
        y_sh, y_sz = day_maps["sh000001"][yday_dt], day_maps["sz399001"][yday_dt]
        n_y = min(len(y_sh), len(y_sz))
        yday_cum = [round(y_sh[i] + y_sz[i], 1) for i in range(n_y)]
        yday_total = yday_cum[-1] if yday_cum else None
        if yday_total and yday_total > 0:
            for i in range(n_t):
                y_at = yday_cum[min(i, len(yday_cum) - 1)]
                if y_at > 0:
                    ratio = today_cum[i] / y_at
                    pct = (ratio - 1.0) * 100.0
                    # 抑制开盘前 3 分钟极小分母带来的异常抖动
                    pct = max(-60.0, min(120.0, pct))
                    curve_pct.append(round(pct, 2))
                else:
                    curve_pct.append(0.0)
            if curve_pct:
                pred_pct = curve_pct[-1]
                pred_total = round(yday_total * (1.0 + pred_pct / 100.0), 1)

    return {
        "date": today_dt,
        "yday_date": yday_dt,
        "today_total_yi": today_total,
        "yday_total_yi": yday_total,
        "pred_total_yi": pred_total,
        "pred_vs_yday_pct": pred_pct,
        "curve_pct": curve_pct,
        "today_cum_yi": today_cum,
        "yday_cum_yi": yday_cum,
    }


# ------------------------------------------------------------------ ETF 权重大市值股

_HOLD_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_HOLD_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.I | re.S)
_HOLD_TAG = re.compile(r"<[^>]+>")
_HOLD_CODE = re.compile(r"^\d{6}$")
_HOLD_WEIGHT = re.compile(r"^(\d+(?:\.\d+)?)%$")


def _stock_exchange(num: str) -> str:
    if num.startswith(("5", "6", "9")):
        return "SH"
    if num.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def parse_etf_holdings(text: str, limit: int = 10) -> list[dict]:
    """从东财基金持仓表抽出股票代码与占净值比例，按权重从高到低。"""
    holdings = []
    seen = set()
    for row in _HOLD_ROW.findall(text or ""):
        cells = [_HOLD_TAG.sub("", cell).replace("&nbsp;", " ").strip() for cell in _HOLD_CELL.findall(row)]
        code = next((cell for cell in cells if _HOLD_CODE.fullmatch(cell)), None)
        weight = next((_HOLD_WEIGHT.fullmatch(cell) for cell in cells if _HOLD_WEIGHT.fullmatch(cell)), None)
        if not code or not weight or code in seen:
            continue
        seen.add(code)
        holdings.append({"code": f"{code}.{_stock_exchange(code)}", "weight": float(weight.group(1))})
    holdings.sort(key=lambda item: item["weight"], reverse=True)
    return holdings[:limit]


def fetch_etf_holdings(code: str, http_get=_http_get, limit: int = 10) -> list[dict]:
    """最新一期基金前十大重仓。失败或没有股票持仓时返回空列表，调用方退回配置候选池。"""
    num = str(code).split(".")[0]
    url = ("https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
           f"?type=jjcc&code={num}&topline={limit}&year=&month=")
    try:
        text = http_get(url, referer="https://fundf10.eastmoney.com/")
    except Exception:  # noqa: BLE001
        return []
    return parse_etf_holdings(text, limit=limit)


def select_large_cap_weights(holdings: list[dict], fallback: list[str], quotes: dict,
                             limit: int = 4) -> tuple[list[str], dict[str, float]]:
    """在 ETF 披露的权重股里按总市值取前 limit 只。没有披露时，配置候选池同样只按总市值排。"""
    weights = {item["code"]: item["weight"] for item in holdings if item.get("code")}
    pool = list(weights) if weights else list(fallback)

    def sort_key(code: str):
        mv = (quotes.get(code) or {}).get("mv_yi")
        return (mv is not None, mv or 0.0, weights.get(code) or 0.0)

    pool.sort(key=sort_key, reverse=True)
    return pool[:limit], weights


# ------------------------------------------------------------------ 采集完整盘中看板 Payload

def collect_intraday_payload(http_get=_http_get, now: datetime | None = None, top_stocks_per_etf: int = 4) -> dict:
    """采集 24 只行业/主题 ETF 及其强势股的实时日 K + 1 分钟分时曲线，返回结构化 JSON payload。"""
    now = (now or datetime.now(SH)).astimezone(SH)
    pool = list(config.INTRADAY_ETFS)
    etf_codes = [x["code"] for x in pool]
    bk_codes = sorted({x["bk"] for x in pool if x.get("bk")})
    index_codes = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH"]

    holdings_by_etf: dict[str, list[dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        hold_futs = {ex.submit(fetch_etf_holdings, item["code"], http_get): item["code"] for item in pool}
        for fut, code in hold_futs.items():
            try:
                holdings_by_etf[code] = fut.result(timeout=8) or []
            except Exception:  # noqa: BLE001
                holdings_by_etf[code] = []

    stock_codes = sorted({
        row["code"]
        for item in pool
        for row in (holdings_by_etf.get(item["code"]) or [{"code": sc} for sc in (item.get("stocks") or [])])
    })

    # 1. 批量拉取 ETF、行业 BK、权重股及大盘指数的实时日 K 与主力净流入
    all_quote_codes = index_codes + etf_codes + bk_codes + stock_codes
    quotes = fetch_merged_quotes(all_quote_codes, http_get=http_get)

    # 2. 每个 ETF 只在披露的权重股里按总市值取前几只；没有披露时退回配置候选池
    selected_stocks_by_etf: dict[str, list[str]] = {}
    weights_by_etf: dict[str, dict[str, float]] = {}
    unique_top_stocks: list[str] = []
    seen_top = set()
    for item in pool:
        chosen, weights = select_large_cap_weights(
            holdings_by_etf.get(item["code"]) or [],
            list(item.get("stocks") or []),
            quotes,
            limit=top_stocks_per_etf,
        )
        selected_stocks_by_etf[item["code"]] = chosen
        weights_by_etf[item["code"]] = weights
        for sc in chosen:
            if sc not in seen_top:
                seen_top.add(sc)
                unique_top_stocks.append(sc)

    # 3. 并发拉取 24 只 ETF + 权重大市值股的 1 分钟分时数据 + 全市场量能分时
    intraday_targets = etf_codes + unique_top_stocks
    intraday_map: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=14) as ex:
        fut_vol = ex.submit(fetch_market_volume_curve, http_get)
        fut_bars = {ex.submit(fetch_symbol_intraday, code, http_get): code for code in intraday_targets}
        for fut, code in fut_bars.items():
            try:
                res = fut.result(timeout=12)
                if res:
                    intraday_map[code] = res
            except Exception:  # noqa: BLE001
                pass
        try:
            vol_curve = fut_vol.result(timeout=12)
        except Exception:  # noqa: BLE001
            vol_curve = {
                "date": None, "yday_date": None, "today_total_yi": None, "yday_total_yi": None,
                "pred_total_yi": None, "pred_vs_yday_pct": None,
                "curve_pct": [], "today_cum_yi": [], "yday_cum_yi": [],
            }

    # 4. 组装每个板块 ETF 及其强势股的曲线与日 K
    sectors_out = []
    sources_used = set()
    trade_dates = set()
    max_last_idx = -1

    for item in pool:
        code = item["code"]
        bk = item.get("bk")
        q_etf = quotes.get(code) or {}
        q_bk = quotes.get(bk) or {}
        intra = intraday_map.get(code)
        if intra and intra.get("source"):
            sources_used.add(intra["source"])
        if intra and intra.get("date"):
            trade_dates.add(intra["date"])

        pre_close = q_etf.get("pre_close") or (intra.get("pre_close") if intra else None)
        # 资金流口径：优先使用东财对应行业板块主力净额（量级数十亿，与参考图一致），若无则用 ETF 主力净额或分时脉冲估算
        bk_flow = q_bk.get("net_inflow_yi")
        etf_flow = q_etf.get("net_inflow_yi")
        target_flow = bk_flow if bk_flow is not None else etf_flow

        curves = align_intraday_curves(
            intra["bars"] if intra else [],
            pre_close=pre_close,
            target_net_flow_yi=target_flow,
        )
        if curves["last_idx"] > max_last_idx:
            max_last_idx = curves["last_idx"]

        pct_val = q_etf.get("pct")
        if pct_val is None and curves["pct"]:
            pct_val = curves["pct"][-1]
        amt_val = q_etf.get("amount_yi")
        if not amt_val and curves["amt"]:
            amt_val = curves["amt"][-1]
        flow_val = target_flow if target_flow is not None else (curves["flow"][-1] if curves["flow"] else 0.0)

        # 组装该 ETF 的权重大市值股（含日 K 四要素与分时走势）
        leaders = []
        etf_weights = weights_by_etf.get(code) or {}
        for sc in selected_stocks_by_etf.get(code, []):
            sq = quotes.get(sc) or {}
            s_intra = intraday_map.get(sc)
            s_curves = align_intraday_curves(
                s_intra["bars"] if s_intra else [],
                pre_close=sq.get("pre_close") or (s_intra.get("pre_close") if s_intra else None),
                target_net_flow_yi=sq.get("net_inflow_yi"),
            ) if s_intra else {"pct": [], "amt": [], "flow": [], "prices": []}
            leaders.append({
                "code": sc,
                "name": sq.get("name") or sc,
                "price": sq.get("price"),
                "pre_close": sq.get("pre_close"),
                "open": sq.get("open"),
                "high": sq.get("high"),
                "low": sq.get("low"),
                "pct": sq.get("pct") if sq.get("pct") is not None else (s_curves["pct"][-1] if s_curves["pct"] else 0.0),
                "amount_yi": sq.get("amount_yi") or (s_curves["amt"][-1] if s_curves["amt"] else 0.0),
                "net_inflow_yi": sq.get("net_inflow_yi") if sq.get("net_inflow_yi") is not None else (s_curves["flow"][-1] if s_curves["flow"] else 0.0),
                "mv_yi": sq.get("mv_yi"),
                "weight_pct": etf_weights.get(sc),
                "pct_curve": s_curves["pct"],
                "flow_curve": s_curves["flow"],
                "trade_date": s_intra.get("date") if s_intra else None,
                "has_close_bar": bool(s_intra and any(hm == "15:00" for hm, _, _ in s_intra["bars"])),
            })

        sectors_out.append({
            "code": code,
            "name": item["name"],
            "etf_full_name": q_etf.get("name") or item["name"],
            "bk": bk,
            "price": q_etf.get("price") or (curves["prices"][-1] if curves["prices"] else None),
            "pre_close": pre_close,
            "open": q_etf.get("open") or pre_close,
            "high": q_etf.get("high"),
            "low": q_etf.get("low"),
            "pct": round(float(pct_val or 0.0), 2),
            "amount_yi": round(float(amt_val or 0.0), 2),
            "net_inflow_yi": round(float(flow_val or 0.0), 2),
            "etf_inflow_yi": round(float(etf_flow), 2) if etf_flow is not None else None,
            "pct_curve": curves["pct"],
            "flow_curve": curves["flow"],
            "amt_curve": curves["amt"],
            "trade_date": intra.get("date") if intra else None,
            "has_close_bar": bool(intra and any(hm == "15:00" for hm, _, _ in intra["bars"])),
            "leaders": leaders,
        })

    # 默认按涨跌幅排序记录排名，同时前端支持按净流入/涨跌幅/成交额动态重排
    sectors_out.sort(key=lambda x: x["pct"], reverse=True)

    # 统计顶部市场情绪概览
    sh_q = quotes.get("000001.SH") or {}
    sz_q = quotes.get("399001.SZ") or {}
    up_sectors = sum(1 for s in sectors_out if s["pct"] > 0)
    down_sectors = sum(1 for s in sectors_out if s["pct"] < 0)
    up_stocks = (sh_q.get("up_count") or 0) + (sz_q.get("up_count") or 0)
    down_stocks = (sh_q.get("down_count") or 0) + (sz_q.get("down_count") or 0)

    resolved_date = vol_curve.get("date") or (max(trade_dates) if trade_dates else now.strftime("%Y%m%d"))
    sampled_time = MINUTE_GRID[max_last_idx] if 0 <= max_last_idx < len(MINUTE_GRID) else now.strftime("%H:%M")

    return {
        "meta": {
            "trade_date": resolved_date,
            "sampled_at": sampled_time,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "points_count": max_last_idx + 1 if max_last_idx >= 0 else 0,
            "total_grid": len(MINUTE_GRID),
            "sector_count": len(sectors_out),
            "up_sectors": up_sectors,
            "down_sectors": down_sectors,
            "up_stocks": up_stocks if up_stocks > 0 else None,
            "down_stocks": down_stocks if down_stocks > 0 else None,
            "source": "/".join(sorted(sources_used)) if sources_used else "无分时数据",
        },
        "grid": MINUTE_GRID,
        "volume": vol_curve,
        "indices": [
            {
                "code": ic,
                "name": (quotes.get(ic) or {}).get("name") or ic,
                "price": (quotes.get(ic) or {}).get("price"),
                "pct": (quotes.get(ic) or {}).get("pct"),
                "amount_yi": (quotes.get(ic) or {}).get("amount_yi"),
            }
            for ic in index_codes if ic in quotes
        ],
        "sectors": sectors_out,
    }


def build_demo_intraday_payload(trade_date: str = "20261106") -> dict:
    """生成确定性的离线模拟数据（用于 `build.py --demo` 及单元测试，完全不访问网络）。"""
    rng = np.random.default_rng(20261106)
    n_pts = len(MINUTE_GRID)
    pool = list(config.INTRADAY_ETFS)
    sectors_out = []
    for idx, item in enumerate(pool):
        drift = (len(pool) / 2.0 - idx) * 0.015
        steps = rng.normal(drift / 20.0, 0.09, n_pts)
        pct_curve = [round(float(x), 2) for x in np.cumsum(steps)]
        pct_curve[0] = round(float(steps[0]), 2)
        final_pct = pct_curve[-1]

        m_amts = rng.uniform(0.01, 0.08, n_pts) * (1.5 if idx < 6 else 0.7)
        amt_curve = [round(float(x), 2) for x in np.cumsum(m_amts)]
        flow_steps = steps * m_amts * 45.0
        flow_curve = [round(float(x), 2) for x in np.cumsum(flow_steps)]
        pre_close = round(0.8 + (idx % 7) * 0.25, 3)
        price = round(pre_close * (1.0 + final_pct / 100.0), 3)

        leaders = []
        for s_i, sc in enumerate((item.get("stocks") or [])[:4]):
            s_steps = steps * (1.4 - s_i * 0.15) + rng.normal(0, 0.05, n_pts)
            s_pct_curve = [round(float(x), 2) for x in np.cumsum(s_steps)]
            s_pct = s_pct_curve[-1]
            s_pre = round(15.0 + ((idx * 3 + s_i * 7) % 80), 2)
            s_px = round(s_pre * (1.0 + s_pct / 100.0), 2)
            leaders.append({
                "code": sc,
                "name": f"龙头{sc[:6]}",
                "price": s_px,
                "pre_close": s_pre,
                "open": round(s_pre * (1.0 + s_pct_curve[0] / 100.0), 2),
                "high": round(max(s_px, s_pre * 1.03), 2),
                "low": round(min(s_px, s_pre * 0.98), 2),
                "pct": s_pct,
                "amount_yi": round(float(amt_curve[-1] * (0.6 - s_i * 0.1)), 2),
                "net_inflow_yi": round(float(flow_curve[-1] * (0.35 - s_i * 0.06)), 2),
                "mv_yi": round(3800.0 - s_i * 650.0, 1),
                "weight_pct": round(12.0 - s_i * 2.0, 1),
                "pct_curve": s_pct_curve,
                "flow_curve": [round(float(x * (0.35 - s_i * 0.06)), 2) for x in flow_curve],
            })

        sectors_out.append({
            "code": item["code"],
            "name": item["name"],
            "etf_full_name": item["name"],
            "bk": item.get("bk"),
            "price": price,
            "pre_close": pre_close,
            "open": round(pre_close * (1.0 + pct_curve[0] / 100.0), 3),
            "high": round(pre_close * (1.0 + max(pct_curve) / 100.0), 3),
            "low": round(pre_close * (1.0 + min(pct_curve) / 100.0), 3),
            "pct": final_pct,
            "amount_yi": amt_curve[-1],
            "net_inflow_yi": flow_curve[-1],
            "etf_inflow_yi": round(flow_curve[-1] * 0.18, 2),
            "pct_curve": pct_curve,
            "flow_curve": flow_curve,
            "amt_curve": amt_curve,
            "leaders": leaders,
        })

    sectors_out.sort(key=lambda x: x["pct"], reverse=True)
    up_s = sum(1 for s in sectors_out if s["pct"] > 0)
    down_s = sum(1 for s in sectors_out if s["pct"] < 0)
    yday_cum = [round(float(x), 1) for x in np.linspace(60.0, 14900.0, n_pts)]
    today_cum = [round(float(y * (0.92 + 0.06 * (i / n_pts))), 1) for i, y in enumerate(yday_cum)]
    curve_pct = [round((t / y - 1.0) * 100.0, 2) for t, y in zip(today_cum, yday_cum)]

    return {
        "meta": {
            "trade_date": trade_date,
            "sampled_at": "15:00",
            "updated_at": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 15:00:00",
            "points_count": n_pts,
            "total_grid": n_pts,
            "sector_count": len(sectors_out),
            "up_sectors": up_s,
            "down_sectors": down_s,
            "up_stocks": 3323,
            "down_stocks": 2008,
            "source": "演示数据",
        },
        "grid": MINUTE_GRID,
        "volume": {
            "date": trade_date,
            "yday_date": "20261105",
            "today_total_yi": today_cum[-1],
            "yday_total_yi": yday_cum[-1],
            "pred_total_yi": today_cum[-1],
            "pred_vs_yday_pct": curve_pct[-1],
            "curve_pct": curve_pct,
            "today_cum_yi": today_cum,
            "yday_cum_yi": yday_cum,
        },
        "indices": [
            {"code": "000001.SH", "name": "上证指数", "price": 3486.76, "pct": 0.28, "amount_yi": 6221.2},
            {"code": "399001.SZ", "name": "深证成指", "price": 10301.40, "pct": 1.16, "amount_yi": 8384.0},
        ],
        "sectors": sectors_out,
    }
