"""构建入口：解析参数 → 交易日判定 → 取数编排 → 组装 payload → 渲染单文件 HTML。

降级原则：任何单个数据源 / 面板失败只在局部留空并记 WARNING（汇总进 payload.meta.warnings），
构建不中断、退出码 0。唯一的致命情况是「整体取数失败」：交易日历取不到，或候选开市日的日线全为空
（典型：token 无效、接口整体不可用）——此时记 ERROR、退出码 3、不写输出文件，
让 CI 不部署、保留上一份好页面。token 只从环境变量 TUSHARE_TOKEN 读取，日志与页面里不出现。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config
import metrics_etf as me
import metrics_liquidity as ml
import metrics_sentiment as ms
from render import render
from summary import build_conclusions, signed

log = logging.getLogger("build")


class FatalBuildError(Exception):
    """整体取数失败（交易日历 / 日线全无）：不应生成页面覆盖上一份好页面。"""


SH = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "template.html"
POLICY_CSV = ROOT / "data" / "policy_ops.csv"
DEFAULT_OUT = "site/index.html"
LOOKBACK_FRED_DAYS = 400
ROUND_BACK_DAYS = 38
INST_SPARK_DAYS = 5  # 龙虎榜机构净买入逐日取数，限制调用量

CORE5_NAME, BROAD10_NAME, IND6_NAME, ALL16_NAME = "核心五通道", "宽基十只", "行业对照六只", "全部十六只"


# ------------------------------------------------------------------ 小工具

def _scrub(msg: str) -> str:
    for name in ("TUSHARE_TOKEN", "FRED_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            msg = msg.replace(secret, "***")
    return msg


def _short(e: Exception, n: int = 80) -> str:
    return _scrub(str(e)).replace("\n", " ")[:n]


def _shift(d: str, days: int) -> str:
    return (datetime.strptime(d, "%Y%m%d") + timedelta(days=days)).strftime("%Y%m%d")


def _month_start(d: str, back: int) -> str:
    """d 所在月往前 back 个月的 1 号（YYYYMMDD）。"""
    total = int(d[:4]) * 12 + int(d[4:6]) - 1 - back
    return f"{total // 12:04d}{total % 12 + 1:02d}01"


def _md(d: str | None) -> str:
    return f"{int(d[4:6])}/{int(d[6:8])}" if d else "—"


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def _empty_series() -> pd.Series:
    return pd.Series(dtype=float)


def _idx_strings(col: pd.Series) -> list[str]:
    if pd.api.types.is_datetime64_any_dtype(col):
        return list(col.dt.strftime("%Y%m%d"))
    return [str(v).replace("-", "")[:8] for v in col]


def _ser(df, idx: str, val: str) -> pd.Series:
    """DataFrame 两列 → 按日期（字符串）升序、去重、去 NaN 的 float Series；输入缺列 / 空 → 空 Series。"""
    if not isinstance(df, pd.DataFrame) or df.empty or idx not in df.columns or val not in df.columns:
        return _empty_series()
    s = pd.Series(pd.to_numeric(df[val], errors="coerce").to_numpy(dtype=float), index=_idx_strings(df[idx]))
    return s[~s.index.duplicated(keep="last")].sort_index().dropna()


def _fetch(label: str, fn, *args, note: str = "", **kw):
    """调用数据源；异常 → WARNING 并返回 None；空结果 → WARNING 并原样返回。"""
    try:
        r = fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("%s 取数失败（%s: %s）%s", label, type(e).__name__, _short(e), note)
        return None
    if r is None or (isinstance(r, pd.DataFrame) and r.empty):
        log.warning("%s 无数据%s", label, note)
    return r


def _safe(name: str, fn, default):
    """面板级兜底：任何异常 → WARNING + 默认值（default 为可调用则调用）。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.warning("%s 生成失败（%s: %s），该部分留空", name, type(e).__name__, _short(e))
        return default() if callable(default) else default


def _mk_card(key, label, series, unit, **kw) -> dict:
    try:
        return ml.make_card(key, label, series, unit, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("卡片 %s 生成失败（%s: %s）", label, type(e).__name__, _short(e))
        return ml.make_card(key, label, _empty_series(), unit)


class _WarnCollector(logging.Handler):
    """收集运行期间所有 WARNING 及以上日志（去重、擦除 token）。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        try:
            msg = _scrub(record.getMessage())
        except Exception:  # noqa: BLE001
            return
        if msg not in self.messages:
            self.messages.append(msg)


@dataclass
class Ctx:
    src: object
    fred: object
    D: str
    days: list            # ≤ D 的最近若干开市日（升序）
    daily: dict           # trade_date -> stock_daily DataFrame（非空）
    names: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)   # trade_date -> limit_flags DataFrame
    now: datetime = None
    exact_limits: dict = field(default_factory=dict)   # trade_date -> {"up","down"}（limit_list_d 精确值），每日只取一次


# ------------------------------------------------------------------ 日期

def resolve_date(arg: str | None, now: datetime | None = None) -> str:
    """--date 原样返回；缺省 → Asia/Shanghai 当日 YYYYMMDD。"""
    if arg:
        return arg
    now = now or datetime.now(SH)
    now = now.astimezone(SH) if now.tzinfo else now.replace(tzinfo=SH)
    return now.strftime("%Y%m%d")


def _fallback_is_expected(date: str, now: datetime) -> bool:
    """「当天日线还没出」属预期：北京时间 date 当日 17:00 之前（含早于 date 的时刻）。"""
    return (now.strftime("%Y%m%d"), now.hour) < (date, 17)


def _resolve_trade_day(src, date: str, daily: dict, now: datetime | None = None) -> str:
    """D = ≤ date 的最近开市日中 stock_daily 非空的最新一个（最多回看 3 个开市日）。

    交易日历取不到 / 为空，或候选日的日线全为空 → FatalBuildError（整体取数失败，调用方不得写输出）。
    """
    now = (now or datetime.now(SH)).astimezone(SH)
    try:
        cand = list(src.trade_days(date, 3))
    except Exception as e:  # noqa: BLE001
        raise FatalBuildError(f"交易日历获取失败（{type(e).__name__}: {_short(e)}）") from e
    if not cand:
        raise FatalBuildError("交易日历为空，无法确定交易日")
    for d in reversed(cand):
        df = _fetch(f"{_md(d)} 全市场日线", src.stock_daily, d)
        if isinstance(df, pd.DataFrame) and not df.empty:
            daily[d] = df
            if d != cand[-1]:
                # 预期内的回退：退到前一个开市日，且 date 不是开市日，或是开市日但还没到 17:00
                expected = d == cand[-2] and (cand[-1] != date or _fallback_is_expected(date, now))
                log.log(logging.INFO if expected else logging.WARNING,
                        "交易日回退：%s 日线未出，使用 %s", _md(cand[-1]), _md(d))
            return d
    raise FatalBuildError(f"最近 {len(cand)} 个开市日（{_md(cand[0])}–{_md(cand[-1])}）全市场日线均为空")


def _range_latest(label: str, fn, start: str, ends: list[str]):
    """区间接口：从 ends[0] 起依次尝试（最多 len(ends) 次），返回首个非空结果；异常即放弃。"""
    for e in ends:
        try:
            df = fn(start, e)
        except Exception as ex:  # noqa: BLE001
            log.warning("%s 取数失败（%s: %s）", label, type(ex).__name__, _short(ex))
            return None
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df
    log.warning("%s 最近 %d 个开市日均无数据", label, len(ends))
    return None


# ------------------------------------------------------------------ 面板

def _day_total(df: pd.DataFrame) -> float:
    d = df[~df["ts_code"].astype(str).str.upper().str.endswith(".BJ")]
    return float(pd.to_numeric(d["amount"], errors="coerce").sum(min_count=1)) if len(d) else np.nan


def _indices(c: Ctx) -> list[dict]:
    out = []
    start = c.days[max(0, len(c.days) - 10)] if c.days else c.D
    for code, name in config.A_INDICES:
        close = pct = date = None
        df = _fetch(f"指数 {name}", c.src.index_daily, code, start, c.D)
        s = _ser(df, "trade_date", "close")
        p = _ser(df, "trade_date", "pct_chg")
        if len(s):
            close = float(s.iloc[-1])
            pct = _num(p.get(s.index[-1]))
            date = str(s.index[-1])
        out.append({"code": code, "name": name, "close": close, "pct_chg": pct, "date": date})
    return out


def _vote_inputs(totals: pd.Series, D: str, margin: pd.Series, flow: pd.Series, w1: pd.Series):
    turnover = None
    if D in totals.index and pd.notna(totals[D]):
        prev = totals[totals.index < D].dropna().tail(config.AMOUNT_MA)
        if len(prev) >= 5 and prev.mean() > 0:
            turnover = float(totals[D] / prev.mean())
    margin_chg = None
    if len(margin) >= 6 and margin.iloc[-6] != 0:
        margin_chg = float((margin.iloc[-1] / margin.iloc[-6] - 1) * 100)
    flow_5d = float(flow.iloc[-5:].sum()) if len(flow) >= 5 else None
    shibor_bp = (float((w1.iloc[-1] - w1.iloc[-config.AMOUNT_MA:].mean()) * 100)
                 if len(w1) >= config.AMOUNT_MA else None)
    return turnover, margin_chg, flow_5d, shibor_bp


def _inst_series(src, days: list[str]) -> pd.Series:
    """龙虎榜机构净买入逐日取数（亿元）；全部失败只记一条 WARNING。"""
    vals, errs = {}, 0
    for d in days:
        try:
            v = src.inst_net_buy(d)
        except Exception as e:  # noqa: BLE001
            errs += 1
            log.warning("龙虎榜机构净买入 %s 取数失败（%s: %s）", _md(d), type(e).__name__, _short(e))
            continue
        if _num(v) is not None:
            vals[d] = float(v)
    if not vals and not errs:
        log.warning("龙虎榜机构净买入 最近 %d 个开市日无数据", len(days))
    return pd.Series(vals, dtype=float).sort_index()


def _exact_limits(c: Ctx, d: str) -> dict:
    """limit_list_d 的精确涨跌停家数 {up, down}（取不到 → 均为 None）；每个交易日只调用一次。"""
    if d not in c.exact_limits:
        lc = _fetch(f"涨跌停家数 {_md(d)}", c.src.limit_counts, d)
        c.exact_limits[d] = lc if isinstance(lc, dict) else {"up": None, "down": None}
    return c.exact_limits[d]


def _liquidity(c: Ctx) -> dict:
    src, D = c.src, c.D
    start = c.days[0]
    totals = pd.Series({d: _day_total(df) for d, df in c.daily.items()}, dtype=float).sort_index()

    margin_df = _fetch("两融余额", src.margin, start, D)
    margin = _ser(margin_df, "trade_date", "balance")
    mflow_df = _range_latest("大盘主力净流入", src.market_flow, start, [D] + c.days[-3:-1][::-1])
    mflow = _ser(mflow_df, "trade_date", "net_amount")
    if len(mflow) and mflow.index[-1] != D:
        log.warning("大盘主力净流入 数据截至 %s", _md(mflow.index[-1]))
    sh_df = _fetch("Shibor", src.shibor, start, D)
    on, w1 = _ser(sh_df, "date", "on"), _ser(sh_df, "date", "w1")

    turnover, margin_chg, flow_5d, shibor_bp = _vote_inputs(totals, D, margin, mflow, w1)

    # 涨停/跌停：逐日近似序列（按涨跌幅判定）；D 与前一日以 limit_list_d 精确值覆盖，使「较前值变化」同口径
    approx_up = pd.Series({d: float(f["up_limit"].sum()) for d, f in c.flags.items()}, dtype=float).sort_index()
    approx_dn = pd.Series({d: float(f["down_limit"].sum()) for d, f in c.flags.items()}, dtype=float).sort_index()
    down = int(approx_dn[D]) if D in approx_dn.index else None
    for d in [x for x in c.days if x < D][-1:] + [D]:
        lc = _exact_limits(c, d)
        if lc.get("up") is not None:
            approx_up[d] = float(lc["up"])
            if d == D and lc.get("down") is not None:
                down = int(lc["down"])
    inst = _inst_series(src, c.days[-INST_SPARK_DAYS:])

    ratio_note = f"÷20日均 {turnover:.2f}" if turnover is not None else ""
    domestic = [
        _mk_card("turnover", "全市场成交额", totals, "亿元", change="pct", note=ratio_note),
        _mk_card("margin", "两融余额", margin, "亿元"),
        _mk_card("main_flow", "大盘主力净流入", mflow, "亿元"),
        _mk_card("limit", "涨停/跌停", approx_up.sort_index(), "家",
                 note=f"跌停 {down}" if down is not None else ""),
        _mk_card("inst", "龙虎榜机构净买入", inst, "亿元"),
    ]

    m2_df = _fetch("M2", src.m2, _month_start(D, 12)[:6], D[:6])
    bond = _ser(_fetch("10年国债期货", src.bond_fut_10y, start, D), "trade_date", "close")
    brent = _ser(_fetch("布伦特原油", src.fx, "UKOIL", start, D), "trade_date", "close")
    macro = [
        _mk_card("shibor_on", "Shibor隔夜", on, "%"),
        _mk_card("shibor_1w", "Shibor 1W", w1, "%"),
        _mk_card("m2", "M2同比", _ser(m2_df, "month", "m2_yoy"), "%"),
        _mk_card("bond_fut", "10年国债期货", bond, "元", note="价格涨=收益率降"),
        _mk_card("brent", "布伦特原油", brent, "美元/桶", change="pct"),
    ]

    external, rate = _external(c, start)
    vote = ml.liquidity_vote(turnover, margin_chg, flow_5d, shibor_bp)
    return {"domestic": domestic, "macro": macro, "external": external, "vote": vote, "rate_expect": rate}


def _external(c: Ctx, start: str):
    src, D = c.src, c.D
    fred_start = _shift(D, -LOOKBACK_FRED_DAYS)
    fred_start = f"{fred_start[:4]}-{fred_start[4:6]}-{fred_start[6:]}"
    cut = pd.Timestamp(D)
    missing, raw = [], {}
    for sid in config.FRED_SERIES:
        try:
            s = c.fred.series(sid, fred_start)
        except Exception as e:  # noqa: BLE001
            log.warning("FRED %s 获取失败（%s）", sid, type(e).__name__)
            s = None
        if s is None or len(s) == 0:
            missing.append(sid)
            raw[sid] = _empty_series()
        else:
            s = pd.to_numeric(s, errors="coerce")
            raw[sid] = s[s.index <= cut]
    if missing:
        log.warning("FRED 序列 %s 无数据，相关外围卡片留空", "、".join(missing))

    fed = ml.fed_net_liquidity(raw["WALCL"], raw["WTREGEN"], raw["RRPONTSYD"])
    yields = _fetch("美债收益率", src.us_yields, start, D)
    usd = _ser(_fetch("美元指数", src.fx, "USDOLLAR", start, D), "trade_date", "close")
    gl = _fetch("美股指数", src.global_index, start, D)
    cards = [
        _mk_card("fed_net", "美联储净流动性", fed, "十亿美元", change_lag=4),
        _mk_card("reserves", "银行准备金", raw["WRESBAL"] / 1000.0, "十亿美元"),
        _mk_card("us2y", "美债2年", _ser(yields, "date", "y2"), "%"),
        _mk_card("us10y", "美债10年", _ser(yields, "date", "y10"), "%"),
        _mk_card("usd", "美元指数", usd, "点", change="pct", note="FXCM 美元指数，非 DXY"),
    ]
    for code, name in config.US_INDICES:
        sub = gl[gl["ts_code"] == code] if isinstance(gl, pd.DataFrame) and "ts_code" in gl.columns else None
        cards.append(_mk_card(code.lower(), name, _ser(sub, "trade_date", "close"), "点", change="pct"))

    dff = raw["DFF"].dropna()
    f30 = _ser(_fetch("联邦基金期货 FED30D", src.fx, "FED30D", start, D), "trade_date", "close")
    rate = ml.rate_expectation(float(dff.iloc[-1]) if len(dff) else None, float(f30.iloc[-1]) if len(f30) else None)
    return cards, rate


def _empty_rank():
    return {"inflow": [], "outflow": []}


def _sector_flow(c: Ctx) -> dict:
    D = c.D
    start = c.days[max(0, len(c.days) - 7)]
    ends = [D] + c.days[-3:-1][::-1]
    df = _range_latest("板块资金流向", c.src.sector_flow, start, ends)
    if df is None:
        return {"date": None, "day": _empty_rank(), "d5": _empty_rank()}
    df = df[df["trade_date"].astype(str) <= D]
    if df.empty:
        return {"date": None, "day": _empty_rank(), "d5": _empty_rank()}
    d = str(df["trade_date"].astype(str).max())
    if d != D:
        log.warning("板块资金流向 数据截至 %s", _md(d))
    return {"date": d, "day": ml.sector_flow_rank(df, d, 1), "d5": ml.sector_flow_rank(df, d, 5)}


def _recent_limit_codes(flags: dict, day: str) -> set:
    ds = sorted(d for d in flags if d <= day)[-config.RECENT_LIMIT_UP_DAYS:]
    out = set()
    for d in ds:
        f = flags[d]
        out |= set(f.loc[f["up_limit"], "ts_code"])
    return out


def _default_sentiment():
    return {"temperature": {"series": [], "kpis": {}, "dims": {}, "limit_up": None, "limit_down": None,
                            "range": None, "six_dim": None},
            "concentration": {"date": None, "market": None, "market_yday": None, "mode": None, "rows": []},
            "kpis": {}, "market": None, "market_yday": None, "mode": None}


def _sentiment(c: Ctx) -> dict:
    out = _default_sentiment()
    D = c.D

    # --- 韭菜浓度：moneyflow 从 D 往前找最近非空日（最多 3 个开市日）
    conc = _safe("韭菜浓度", lambda: _concentration(c), lambda: dict(out["concentration"], _mode=None))
    out["concentration"] = {k: v for k, v in conc.items() if not k.startswith("_")}
    out["mode"] = conc.get("_mode")
    out["market"], out["market_yday"] = conc.get("market"), conc.get("market_yday")

    # --- 市场体温
    def temp():
        ser = ms.temperature_series(c.daily, c.names, config.AMOUNT_MA)
        if ser.empty:
            return out["temperature"], {}
        kpis = ms.temperature_kpis(ser, config.TEMP_WINDOW)
        win = ser.tail(config.TEMP_WINDOW)
        row = ser.iloc[-1]
        f = c.flags.get(D)
        # 展示的涨停/跌停家数用 limit_list_d 精确值（与流动性卡片同口径）；五维分仍用涨跌幅近似（PROMPT §4.1）
        ex = _exact_limits(c, D)
        lim_up, lim_dn = ex.get("up"), ex.get("down")
        if lim_up is None or lim_dn is None:
            log.warning("涨跌停精确家数缺失，市场体温面板的涨停/跌停回退为按涨跌幅近似判定")
            lim_up = int(f["up_limit"].sum()) if f is not None else None
            lim_dn = int(f["down_limit"].sum()) if f is not None else None
        t = {
            "series": [{"date": str(d), "score": _num(v)} for d, v in win["score"].items()],
            "kpis": kpis,
            "dims": {k: _num(row[k]) for k in ("up_ratio", "median", "limit", "volume", "profit")},
            "limit_up": lim_up,
            "limit_down": lim_dn,
            "range": {"start": str(win.index[0]), "end": str(win.index[-1]), "n": int(len(win))},
            # 六维 = 五维 + 全市场浓度：只有两者同为 D 当日才合成，否则留空（不把不同日的数混在一起）
            "six_dim": ms.six_dim(_num(row["score"]),
                                  out["market"] if out["concentration"].get("date") == D else None),
        }
        return t, kpis

    t, kpis = _safe("市场体温", temp, lambda: (out["temperature"], {}))
    out["temperature"], out["kpis"] = t, kpis
    return out


def _concentration(c: Ctx) -> dict:
    D, src = c.D, c.src
    basic = _fetch("股票列表", src.stock_basic, note="，板块匹配降级")
    if not isinstance(basic, pd.DataFrame) or basic.empty:
        basic = pd.DataFrame({"ts_code": pd.Series(dtype=object), "name": pd.Series(dtype=object),
                              "industry": pd.Series(dtype=object)})
    sw = _fetch("申万行业成分", src.sw_members, note="，降级为 stock_basic.industry")

    Dc, mf = None, None
    for d in c.days[-3:][::-1]:
        df = _fetch(f"个股资金流向 {_md(d)}", src.moneyflow, d)
        if isinstance(df, pd.DataFrame) and not df.empty:
            Dc, mf = d, df
            break
    if Dc is None:
        log.warning("个股资金流向 最近 3 个开市日均无数据，韭菜浓度面板留空")
        return {"date": None, "market": None, "market_yday": None, "mode": None, "rows": [], "_mode": None}
    if Dc != D:
        log.warning("个股资金流向 数据截至 %s", _md(Dc))

    i = c.days.index(Dc) if Dc in c.days else -1
    Dy = c.days[i - 1] if i > 0 else None
    mf_y = None
    if Dy:
        df = _fetch(f"个股资金流向 {_md(Dy)}", src.moneyflow, Dy)
        mf_y = df if isinstance(df, pd.DataFrame) and not df.empty else None

    sectors, mode = ms.match_sectors(basic, sw, _recent_limit_codes(c.flags, Dc))
    sectors_y = None
    if mf_y is not None:
        sectors_y, _ = ms.match_sectors(basic, sw, _recent_limit_codes(c.flags, Dy))
    rows = ms.concentration_wall(mf, mf_y, sectors, sectors_y)
    market = ms.concentration(mf)
    market_y = ms.concentration(mf_y) if mf_y is not None else None
    return {"date": Dc, "market": market, "market_yday": market_y, "mode": mode, "rows": rows, "_mode": mode}


# ------------------------------------------------------------------ ETF / 国家队

def _wide(frames: dict, col: str, index: list[str]) -> pd.DataFrame:
    data = {code: _ser(df, "trade_date", col) for code, df in frames.items()}
    return pd.DataFrame(data).reindex(index).astype(float)


def _tail_ratios(c: Ctx, yday: str | None):
    """尾盘占比：stk_mins 账号限额 1 次/小时，所以只用一只代表 ETF（config.TAIL_CODE）、一次 etf_minutes(yday→D)。
    返回 None（无权限/限频）→ 「无分钟数据」。"""
    D = c.D
    code = config.TAIL_CODE
    res = {"today": None, "yday": None, "source": "无分钟数据"}
    try:
        df = c.src.etf_minutes(code, yday or D, D)
    except Exception as e:  # noqa: BLE001
        log.warning("分钟线 %s 获取失败（%s: %s）", code, type(e).__name__, _short(e))
        df = None
    if df is None:
        log.warning("分钟线 %s 无权限或频率受限（stk_mins 账号限额 1 次/小时），尾盘占比留空", code)
        return res
    for key, day in (("today", D), ("yday", yday)):
        if not day:
            continue
        prefix = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        sub = df[df["trade_time"].astype(str).str[:10] == prefix] if len(df) else df
        if len(sub) == 0:
            log.warning("分钟线 %s 缺 %s 数据，该日尾盘占比留空", code, _md(day))
            continue
        res[key] = me.tail_ratio(sub, config.TAIL_START)
    if res["today"] is not None or res["yday"] is not None:
        res["source"] = f"stk_mins·{code.split('.')[0]}"
    return res


def _default_etf():
    return {"etf_flow": {"dates": [], "daily": [], "cum": [], "index_close": [], "per_etf": []},
            "round": {"count": 0, "start": None, "end": None, "total": None, "low": None, "high": None,
                      "mid": None, "last3": [], "attribution": [float(x) for x in config.ATTRIBUTION]},
            "monthly": [],
            "fingerprint": {"date": None, "groups": [], "tail": {"today": None, "yday": None, "source": "无分钟数据"},
                            "conclusion": {"big": None, "small": "", "text": ""}, "footnote": ""},
            "team_last3": [], "team_asof": None, "round_total": None, "tail_source": "无分钟数据"}


def _etf(c: Ctx, round_start: str | None) -> dict:
    out = _default_etf()
    D, src = c.D, c.src
    months = max(config.FLOW_MONTHS, config.MONTHLY_MONTHS)
    win_start = _month_start(D, months - 1)
    fetch_start = _shift(win_start, -10)
    try:
        all_days = [d for d in src.trade_days(D, months * 23 + 15) if d >= fetch_start]
    except Exception as e:  # noqa: BLE001
        log.warning("ETF 交易日历获取失败（%s: %s），仅用近期交易日", type(e).__name__, _short(e))
        all_days = list(c.days)
    if not all_days:
        all_days = list(c.days)

    codes = list(dict.fromkeys(config.FIXED_BROAD + config.BROAD10 + config.INDUSTRY6))
    shares, closes = {}, {}
    for code in codes:
        sh = _fetch(f"ETF {code} 份额", src.etf_share, code, fetch_start, D, note="，该只份额已跳过")
        dy = _fetch(f"ETF {code} 日线", src.etf_daily, code, fetch_start, D, note="，该只日线已跳过")
        if isinstance(sh, pd.DataFrame) and not sh.empty:
            shares[code] = sh
        if isinstance(dy, pd.DataFrame) and not dy.empty:
            closes[code] = dy
    names = _fetch("ETF 名称", src.etf_names, codes) or {}

    both = {k: v for k, v in shares.items() if k in closes}
    share_w = _wide(both, "share", all_days)
    close_w = _wide({k: closes[k] for k in both}, "close", all_days)
    net = me.net_flow(share_w, close_w) if len(both) else pd.DataFrame(index=all_days)
    net = net[net.index >= win_start]

    fixed = [x for x in config.FIXED_BROAD if x in net.columns]
    sh11 = [x for x in config.SH11 if x in net.columns]
    daily_fixed = me.aggregate(net[fixed], config.DISCLOSURE_MIN) if fixed else pd.Series(np.nan, index=net.index)
    daily_sh = me.aggregate(net[sh11], config.DISCLOSURE_MIN) if sh11 else pd.Series(np.nan, index=net.index)

    # --- 本轮
    start = round_start or config.ROUND_START
    if not start:
        thr = _shift(D, -ROUND_BACK_DAYS)
        start = next((d for d in all_days if d >= thr), thr)
    rnd = me.round_summary(daily_fixed, start, config.ATTRIBUTION, len(fixed))
    if rnd["total"] is None or rnd["total"] <= 0:
        rnd["low"] = rnd["high"] = rnd["mid"] = None
    out["round"] = rnd
    out["round_total"] = rnd["total"]
    out["monthly"] = me.monthly_totals(daily_fixed, config.MONTHLY_MONTHS)

    # --- 国家队（沪市 11 只）
    s = daily_sh.dropna()
    out["team_last3"] = [float(v) for v in s.tail(3).tolist()][::-1]
    out["team_asof"] = str(s.index[-1]) if len(s) else None

    flow_start = _month_start(D, config.FLOW_MONTHS - 1)
    dsh = daily_sh[daily_sh.index >= flow_start]
    idx_df = _fetch(f"指数 {config.DIVERGENCE_INDEX}", src.index_daily, config.DIVERGENCE_INDEX, flow_start, D)
    idx_close = _ser(idx_df, "trade_date", "close").reindex(list(dsh.index))
    out["etf_flow"] = {
        "dates": [str(d) for d in dsh.index],
        "daily": [_num(v) for v in dsh],
        "cum": [_num(v) for v in dsh.cumsum()],
        "index_close": [_num(v) for v in idx_close],
        "per_etf": [{"code": code, "name": names.get(code, code),
                     "share": [_num(v) for v in share_w.loc[share_w.index >= flow_start, code]]}
                    for code in config.SH11 if code in share_w.columns],
    }

    # --- 成交指纹（尾盘占比与指纹各自兜底：任何一块失败都不能拖垮上面已算好的国家队净申赎）
    prev = [d for d in all_days if d < D]
    yday = prev[-1] if prev else None
    out["fingerprint"] = _safe("成交指纹", lambda: _fingerprint(c, closes, all_days, yday),
                               lambda: _default_etf()["fingerprint"])
    out["tail_source"] = out["fingerprint"]["tail"]["source"]
    return out


def _fingerprint(c: Ctx, closes: dict, all_days: list[str], yday: str | None) -> dict:
    D = c.D
    amount_w = _wide(closes, "amount", all_days)
    groups_def = {CORE5_NAME: config.CORE5, BROAD10_NAME: config.BROAD10, IND6_NAME: config.INDUSTRY6,
                  ALL16_NAME: config.BROAD10 + config.INDUSTRY6}
    groups = me.amount_groups(amount_w, D, yday or "", groups_def)
    g = {x["name"]: x for x in groups}
    core_chg = g[CORE5_NAME]["chg_pct"]
    tail = _safe("尾盘占比", lambda: _tail_ratios(c, yday),
                 {"today": None, "yday": None, "source": "无分钟数据"})
    return {
        "date": D,
        "groups": groups,
        "tail": tail,
        "conclusion": {"big": g[BROAD10_NAME]["chg_pct"],
                       "small": f"{CORE5_NAME} {signed(core_chg, 0)}%" if core_chg is not None else f"{CORE5_NAME} —",
                       "text": me.fingerprint_conclusion(g[BROAD10_NAME]["chg_pct"], g[IND6_NAME]["chg_pct"])},
        "footnote": me.ratio_footnote(groups),
    }


# ------------------------------------------------------------------ 新闻

def _news(c: Ctx, date: str) -> dict:
    if not config.NEWS_ENABLED:
        return {"domestic": [], "external": [], "status": "disabled", "since": None}
    end = min(c.now.replace(tzinfo=None), datetime.strptime(date, "%Y%m%d") + timedelta(days=1, seconds=-1))
    start = end - timedelta(days=config.NEWS_DAYS)
    fmt = "%Y-%m-%d %H:%M:%S"
    df = _fetch("新闻", c.src.major_news, start.strftime(fmt), end.strftime(fmt))
    out = ml.filter_news(df if isinstance(df, pd.DataFrame) else pd.DataFrame(),
                         config.NEWS_KW_DOMESTIC, config.NEWS_KW_EXTERNAL,
                         exclude=config.NEWS_EXCLUDE, us_markers=config.NEWS_US_MARKERS,
                         foreign_markers=config.NEWS_FOREIGN_MARKERS)
    # 取数覆盖：区分「取数失败 / 只覆盖了一部分」与「真的没有匹配新闻」
    status, since = "failed", None
    if df is not None:
        try:
            cov = c.src.last_news_coverage()
            status, since = cov.get("status"), cov.get("since")
        except Exception as e:  # noqa: BLE001
            log.warning("新闻覆盖信息获取失败（%s: %s）", type(e).__name__, _short(e))
            status = "ok"
        if status not in ("ok", "partial", "failed"):
            status, since = "ok", None
    out["status"], out["since"] = status, since
    return out


# ------------------------------------------------------------------ 组装

def collect(src, fred, date: str, round_start: str | None, demo: bool, now: datetime | None = None) -> dict:
    """取数并组装 payload（契约见计划「payload 契约」）。任何局部失败只降级，不抛异常。"""
    now = (now or datetime.now(SH)).astimezone(SH)
    handler = _WarnCollector()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        payload = _collect(src, fred, date, round_start, demo, now)
    finally:
        root.removeHandler(handler)
    payload["meta"]["warnings"] = list(handler.messages)
    return payload


def _collect(src, fred, date, round_start, demo, now) -> dict:
    daily: dict = {}
    D = _resolve_trade_day(src, date, daily, now)
    n_days = config.TEMP_WINDOW + config.AMOUNT_MA + 2
    try:
        days = list(src.trade_days(D, n_days))
    except Exception as e:  # noqa: BLE001
        log.warning("交易日历获取失败（%s: %s）", type(e).__name__, _short(e))
        days = []
    if D not in days:
        days.append(D)
    for d in days:
        if d not in daily:
            df = _fetch(f"{_md(d)} 全市场日线", src.stock_daily, d, note="，该日已跳过")
            if isinstance(df, pd.DataFrame) and not df.empty:
                daily[d] = df
    daily = {d: daily[d] for d in sorted(daily) if d <= D}

    basic = _fetch("股票列表", src.stock_basic)
    names = dict(zip(basic["ts_code"], basic["name"])) if isinstance(basic, pd.DataFrame) and not basic.empty else {}
    flags = _safe("涨跌停判定", lambda: {d: ms.limit_flags(df, names) for d, df in daily.items()}, dict)
    c = Ctx(src=src, fred=fred, D=D, days=days, daily=daily, names=names, flags=flags, now=now)

    liquidity = _safe("流动性", lambda: _liquidity(c), lambda: {
        "domestic": [], "macro": [], "external": [],
        "vote": ml.liquidity_vote(None, None, None, None), "rate_expect": ml.rate_expectation(None, None)})
    sector = _safe("板块资金流向", lambda: _sector_flow(c),
                   lambda: {"date": None, "day": _empty_rank(), "d5": _empty_rank()})
    sent = _safe("散户情绪", lambda: _sentiment(c), _default_sentiment)
    etf = _safe("ETF", lambda: _etf(c, round_start), _default_etf)
    news = _safe("新闻", lambda: _news(c, date), lambda: {"domestic": [], "external": [], "status": "failed", "since": None})
    indices = _safe("指数", lambda: _indices(c), list)
    policy = _safe("政策工具表", lambda: ml.load_policy_ops(str(POLICY_CSV)), list)

    fed_card = next((x for x in liquidity["external"] if x["key"] == "fed_net"), {})
    conclusions = _safe("结论条", lambda: build_conclusions(
        liquidity["vote"], liquidity["rate_expect"], fed_card, sector["day"], etf["team_last3"], etf["team_asof"],
        etf["round_total"], sent["kpis"], sent["market"], sent["market_yday"],
        trade_date=D, sector_date=sector.get("date"), conc_date=sent["concentration"].get("date")), list)

    return {
        "meta": {"trade_date": D, "generated_at": now.strftime("%Y-%m-%d %H:%M"), "demo": bool(demo),
                 "sector_mode": sent["mode"], "tail_source": etf["tail_source"], "warnings": []},
        "conclusions": conclusions,
        "indices": indices,
        "liquidity": liquidity,
        "policy_ops": policy,
        "news": news,
        "sector_flow": sector,
        "etf_flow": etf["etf_flow"],
        "round": etf["round"],
        "monthly": etf["monthly"],
        "fingerprint": etf["fingerprint"],
        "temperature": sent["temperature"],
        "concentration": sent["concentration"],
    }


# ------------------------------------------------------------------ CLI

def _make_sources(demo: bool):
    if demo:
        from demo import DemoFred, DemoSource
        return DemoSource(), DemoFred()
    from fred import FredSource
    from sources import TushareSource
    return TushareSource.from_env(), FredSource.from_env()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 A 股盘后看板单文件 HTML")
    ap.add_argument("--date", help="目标日期 YYYYMMDD，默认北京时间当日")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"输出路径，默认 {DEFAULT_OUT}")
    ap.add_argument("--round-start", dest="round_start", help="本轮起始日 YYYYMMDD")
    ap.add_argument("--demo", action="store_true", help="使用随机模拟数据")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    date = resolve_date(args.date)
    try:
        src, fred = _make_sources(args.demo)
    except Exception as e:  # noqa: BLE001
        log.error("数据源初始化失败（%s: %s）", type(e).__name__, _short(e))
        return 2
    try:
        payload = collect(src, fred, date, args.round_start, args.demo)
    except FatalBuildError as e:
        log.error("整体取数失败，不生成页面（保留上一份）：%s", _scrub(str(e)))
        return 3
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    render(str(TEMPLATE), payload, args.out)
    ws = payload["meta"]["warnings"]
    log.info("已生成 %s（交易日 %s，demo=%s，%d 条警告）", args.out, payload["meta"]["trade_date"], args.demo, len(ws))
    for w in ws:
        log.info("  警告：%s", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
