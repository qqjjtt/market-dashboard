"""涨停与申万概念资金追踪引擎（核心数据层与纯函数分析层）。

职责：
1. 获取并缓存：
   - 交易日历 (trade_cal)
   - 申万 2021 行业分类树 (index_classify) 与个股映射 (index_member_all)
   - 申万行业日行情 (sw_daily)
   - 每日涨跌停/炸板明细 (limit_list_d: U=涨停, Z=炸板, D=跌停)
   - 开盘啦题材与净额 (kpl_list: 富集同日、同代码、同标签的涨停和炸板记录)
   - 指定交易日的全市场个股日线 (daily) —— 用于追踪「昨日涨停股今日表现（晋级 / 断板溢价）」
2. 计算并输出供前端交互看板直接消费的结构化 JSON Payload：
   - 每日情绪与资金高度 KPI（最高板、涨停/炸板/跌停、封板率、连板晋级率、昨日涨停溢价、涨停总成交与总封单）
   - 连板天梯（按连板高度分层：5板+ / 4板 / 3板 / 2板 / 首板 + 趋势抱团 N天M板 标记 + 一字/T字/换手板类型）
   - 晋级漏斗与断板追踪（1进2、2进3、3进4、4进5+ 的晋级名单与断板名单及今日涨跌幅）
   - 申万宏源（L1 / L2 / L3 三级）× 连板身位资金矩阵（各行业的涨停数、炸板数、连板身位分布、最高板、涨停成交额、封单额、行业指数涨跌幅与全行业成交额、涨停占行业成交比）
   - 申万行业 × 热门题材概念双维交叉矩阵（哪些申万行业 × 哪个题材概念在共振）
   - 热门题材概念排行（按涨停家数、最高连板、封单与成交额聚合）
   - 近 N 日（如 15–20 个交易日）历史情绪周期、最高板高度演变、申万行业轮动热力矩阵、核心概念轮动矩阵
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

log = logging.getLogger("limit_tracker")
SH = ZoneInfo("Asia/Shanghai")

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "data" / "limit_cache"
RANGE_TRADE_DAYS = 10
# 交易日当天这个时刻（北京时间）前写入的缓存视为盘后数据尚未定稿，联网时再取一次。
SETTLE_HOUR = 18
LIMIT_LIST_MAX_ROWS = 2500
# A conservative split threshold also works if the provider raises its ceiling.
KPL_MAX_ROWS = 2000
SOURCE_SCOPE = (
    "涨停、炸板、跌停与连板统一统计 Tushare limit_list_d（U/Z/D；不含该接口未覆盖的 ST 等股票）。"
    "开盘啦仅为同日、同代码、同状态记录补充题材和净额，不纳入额外股票；净额不等于全行业资金流。"
    "申万行业按可用的最新 SW2021 个股归属回溯，非历史时点成分。"
)


def _text(value) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip()


def _stock_code(value) -> str:
    return _text(value).upper()


def _date_key(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"(?:\d{8}|\d{4}-\d{2}-\d{2})", value):
        raise ValueError("日期须为 YYYYMMDD 或 YYYY-MM-DD")
    return datetime.strptime(value.replace("-", ""), "%Y%m%d").strftime("%Y%m%d")


def strip_roman(x) -> str:
    return _text(x).replace("Ⅰ", "").replace("Ⅱ", "").replace("Ⅲ", "").strip()


def fmt_time(val) -> str:
    """Normalize a source timestamp; do not turn missing/malformed values into a time."""
    s = re.sub(r"\.0+$", "", _text(val))
    if re.fullmatch(r"\d{1,6}", s):
        s = s.zfill(6)
        s = f"{s[:2]}:{s[2:4]}:{s[4:]}"
    elif re.fullmatch(r"\d{1,2}:\d{2}", s):
        s += ":00"
    try:
        normalized = datetime.strptime(s, "%H:%M:%S").strftime("%H:%M:%S")
        return normalized if normalized != "00:00:00" else "—"
    except ValueError:
        return "—"


def classify_board_style(first_time: str, last_time: str, open_times: int | None) -> str:
    """Classify only when the timestamps/open count support the board pattern."""
    ft, lt = fmt_time(first_time), fmt_time(last_time)
    if ft != "—" and ft <= "09:25:05":
        if (open_times is not None and open_times > 0) or (lt != "—" and lt > "09:25:05"):
            return "T字板"
        if lt != "—" and open_times == 0:
            return "一字板"
        return "—"
    if ft != "—" and ft <= "09:32:00" and open_times == 0:
        return "秒板"
    if lt == "—":
        return "—"
    if lt <= "10:00:00":
        return "早盘强板"
    if lt <= "11:30:00":
        return "上午板"
    if lt <= "14:30:00":
        return "午后板"
    return "尾盘板"


def parse_up_stat(up_stat: str | None, limit_times: int | None) -> dict:
    """Read N-days/M-boards independently of the current consecutive height."""
    label = "首板" if limit_times == 1 else (f"{limit_times}连板" if limit_times else "—")
    days = boards = None
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", _text(up_stat))
    if m:
        b, d = map(int, m.groups())
        if 0 < b <= d and (limit_times is None or b >= limit_times):
            days, boards = d, b
    is_trend = 2 <= boards < days if boards is not None else None
    if is_trend:
        label = f"{days}天{boards}板"
    return {"days": days, "boards": boards, "label": label, "is_trend": is_trend}


def parse_concepts(lu_desc: str | None, theme: str | None) -> list[str]:
    """合并开盘啦 lu_desc 与 theme，按顿号/逗号拆分为去重概念列表（首项为主概念）。"""
    out: list[str] = []
    for raw in (lu_desc, theme):
        s = _text(raw)
        if not s or s.lower() in ("nan", "none", "—", "-"):
            continue
        for part in re.split(r"[、,，/；;\r\n]+", s):
            p = part.strip()
            if p and p not in out:
                out.append(p)
    return out


def board_type_tag(ts_code: str, name: str = "") -> str:
    """返回板块涨跌幅属性：20cm (创业/科创)、30cm (北交)、ST (5cm)、10cm (主板)。"""
    if re.match(r"^(?:\*ST|ST)", name, re.I):
        return "ST"
    c = str(ts_code).upper()
    if c.endswith(".BJ"):
        return "30cm"
    if c[:3] in ("300", "301"):
        return "20cm·创"
    if c[:3] in ("688", "689"):
        return "20cm·科"
    return "10cm"


def _num(v, default=None):
    if v is None:
        return default
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _round(v, ndigits=2):
    f = _num(v)
    return round(f, ndigits) if f is not None else None


def _integer(value, minimum=0) -> int | None:
    f = _num(value)
    return int(f) if f is not None and f >= minimum and f.is_integer() else None


def _scaled(value, divisor, digits=2):
    f = _num(value)
    return _round(f / divisor, digits) if f is not None else None


def _sum_field(rows, field):
    """A total over an empty observed population is zero; a partial total is unknown."""
    values = [_num(row.get(field)) for row in rows]
    return round(sum(values), 2) if all(v is not None for v in values) else None


class LimitDataStore:
    """带本地磁盘缓存的 Tushare 涨停与申万数据仓库。"""

    def __init__(self, pro=None, cache_dir: Path = CACHE_DIR, *, offline: bool = False):
        self.cache_dir = Path(cache_dir)
        self.offline = offline
        if not offline:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._pro = pro
        self._sw_map_cache: pd.DataFrame | None = None
        self._sw_tree_cache: pd.DataFrame | None = None

    @property
    def pro(self):
        if self.offline:
            raise RuntimeError("离线模式不能访问 Tushare")
        if self._pro is None:
            import tushare as ts
            token = os.environ.get("TUSHARE_TOKEN")
            self._pro = ts.pro_api(token) if token else ts.pro_api()
        return self._pro

    # ------------------------------------------------------------------ 交易日历
    def get_trade_days(self, end_date: str | None = None, count: int = 60) -> list[str]:
        """获取 <= end_date 的最近 count 个已开市交易日（升序）。"""
        now_sh = datetime.now(SH)
        today = now_sh.strftime("%Y%m%d")
        end = min(_date_key(end_date), today) if end_date else today
        if count <= 0:
            return []
        cal_cache = self.cache_dir / "trade_cal.json"
        cached = {}
        try:
            cached = json.loads(cal_cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not isinstance(cached, dict):
            cached = {}
        days = sorted(set(cached.get("days", [])))
        windows = cached.get("windows", [])
        if not windows and days:  # legacy single-window cache
            windows = [{"start": days[0], "end": cached.get("end_date") or days[-1],
                        "fetched_date": cached.get("fetched_date")}]

        def select(window):
            return [d for d in days if window["start"] <= d <= end][-count:]

        covering = [w for w in windows if w["start"] <= end <= w["end"]]
        fresh = [w for w in covering if w.get("fetched_date") == today and len(select(w)) >= count]
        if fresh:
            return select(min(fresh, key=lambda w: w["start"]))
        if self.offline:
            if covering:
                return select(min(covering, key=lambda w: w["start"]))
            # Permit stale offline data after the most recent cached window, not
            # dates silently bridged across disjoint calendar results.
            if not windows or end < max(w["end"] for w in windows):
                return []
            return select(max(windows, key=lambda w: w["end"]))
        end_dt = datetime.strptime(end, "%Y%m%d")
        start = (end_dt - timedelta(days=max(150, count * 3))).strftime("%Y%m%d")
        future = (end_dt + timedelta(days=15)).strftime("%Y%m%d")
        try:
            df = self.pro.trade_cal(exchange="SSE", start_date=start, end_date=future, is_open=1)
            new_days = sorted({d for d in df["cal_date"].astype(str) if start <= d <= future})
            if new_days:
                # Refresh dates within this interval without erasing older replay windows.
                days = sorted(set(new_days) | {d for d in days if d < start or d > future})
                windows = [w for w in windows if not (start <= w["start"] and w["end"] <= future)]
                windows.append({"start": start, "end": future, "fetched_date": today})
                cal_cache.write_text(json.dumps({"days": days, "windows": windows}), encoding="utf-8")
                return [d for d in new_days if d <= end][-count:]
        except Exception as e:
            log.warning("trade_cal(%s-%s) 获取失败: %s", start, future, e)
        return select(min(covering, key=lambda w: w["start"])) if covering else []

    def resolve_latest_available_day(self, target_date: str | None = None) -> tuple[str, list[str]]:
        """Resolve a replay date; an explicit missing trading day stays selected."""
        days = self.get_trade_days(end_date=target_date, count=65)
        if not days:
            raise RuntimeError("无法获取指定日期的交易日历")
        if target_date:
            return days[-1], days
        recent = days if self.offline else days[-4:]
        for d in reversed(recent):
            if _valid_limit_data(self.get_limit_list(d)):
                return d, [x for x in days if x <= d]
        return days[-1], days

    # ------------------------------------------------------------------ 申万宏源分类体系
    def get_sw_members(self, force: bool = False) -> pd.DataFrame:
        """获取申万 2021 个股最新行业归属：ts_code -> l1_code, l1_name, l2_code, l2_name, l3_code, l3_name。"""
        if self._sw_map_cache is not None and not force:
            return self._sw_map_cache
        cache_file = self.cache_dir / "sw_members.csv"
        if cache_file.exists() and not force:
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime, SH)
            if self.offline or (datetime.now(SH) - mtime).days < 7:
                df = pd.read_csv(cache_file, dtype=str)
                self._sw_map_cache = df
                return df
        frames, limit = [], 3000
        for offset in range(0, 30000, limit) if not self.offline else ():
            try:
                df = self.pro.index_member_all(is_new="Y", limit=limit, offset=offset)
            except Exception as e:
                log.warning("index_member_all 获取失败: %s", e)
                if cache_file.exists():
                    self._sw_map_cache = pd.read_csv(cache_file, dtype=str)
                    return self._sw_map_cache
                break
            if df is None or df.empty:
                break
            if frames and df.equals(frames[-1]):
                break
            frames.append(df)
            if len(df) < limit:
                break
        if not frames:
            return pd.DataFrame(columns=["ts_code", "l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name"])
        out = pd.concat(frames, ignore_index=True)
        for col in ("l1_name", "l2_name", "l3_name"):
            if col in out.columns:
                out[col] = out[col].map(strip_roman)
        # 同一股票若有多条记录，保留 in_date 最新的一条
        if "in_date" in out.columns:
            out = out.sort_values("in_date", kind="stable").drop_duplicates("ts_code", keep="last")
        else:
            out = out.drop_duplicates("ts_code", keep="last")
        cols = ["ts_code", "l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name"]
        out = out[[c for c in cols if c in out.columns]].reset_index(drop=True)
        out.to_csv(cache_file, index=False)
        self._sw_map_cache = out
        return out

    def get_sw_classify(self, force: bool = False) -> pd.DataFrame:
        """获取申万 2021 行业目录 (L1=31, L2=134, L3=346)。"""
        if self._sw_tree_cache is not None and not force:
            return self._sw_tree_cache
        cache_file = self.cache_dir / "sw_classify.csv"
        if cache_file.exists() and not force:
            df = pd.read_csv(cache_file, dtype=str)
            self._sw_tree_cache = df
            return df
        df = None if self.offline else self.pro.index_classify(src="SW2021")
        if df is not None and not df.empty:
            df["industry_name"] = df["industry_name"].map(strip_roman)
            df.to_csv(cache_file, index=False)
            self._sw_tree_cache = df
            return df
        return pd.DataFrame(columns=["index_code", "industry_name", "level", "parent_code"])

    # ------------------------------------------------------------------ 每日与区间行情/涨跌停缓存
    def _provisional(self, day: str, path: Path) -> bool:
        """交易日当天、定稿时刻前写入的缓存可能缺盘后补录，不当作最终数据。"""
        if self.offline:
            return False
        try:
            written = datetime.fromtimestamp(path.stat().st_mtime, SH)
        except OSError:
            return False
        return written.strftime("%Y%m%d") == day and written.hour < SETTLE_HOUR

    @staticmethod
    def _read_daily_cache(path: Path) -> pd.DataFrame:
        try:
            return pd.read_csv(path, dtype={
                "trade_date": str, "ts_code": str, "first_time": str, "last_time": str, "up_stat": str,
            })
        except (OSError, ValueError, pd.errors.ParserError):
            return pd.DataFrame()

    def _load_range(self, trade_days: list[str], source: str, tag: str | None = None,
                    refresh_days=()) -> dict[str, pd.DataFrame]:
        """Return cached/fetched data for requested days, including in-request misses.

        Split saturated responses *before* persisting any rows. A missing date is
        not written to disk because today's file may arrive later or a failed run
        may recover. Missing dates are still memoized for this build.
        """
        days = sorted(set(trade_days))
        prefix, max_rows = ("limit_list", LIMIT_LIST_MAX_ROWS) if tag is None else (
            {"涨停": "kpl_u", "炸板": "kpl_z", "跌停": "kpl_d"}[tag], KPL_MAX_ROWS)
        paths = {d: self.cache_dir / f"{prefix}_{d}.csv" for d in days}
        cached = {d: self._read_daily_cache(path) for d, path in paths.items()}
        copies = dict(cached)
        if self.offline:
            return copies
        refresh_days = set(refresh_days)
        pending = {d for d in days if d in refresh_days or cached[d].empty or self._provisional(d, paths[d]) or
                   (source == "limit_list_d" and not _valid_limit_data(cached[d]))}

        def _split_limit_types(day):
            # If one day still hits the combined cap, try the provider's three
            # disjoint U/Z/D filters. Discard the whole day if any remains capped.
            rows = []
            for typ in ("U", "Z", "D"):
                try:
                    frame = self.pro.limit_list_d(trade_date=day, limit_type=typ)
                except Exception as e:
                    log.warning("limit_list_d(%s, %s) 分类型获取失败: %s", day, typ, e)
                    return None
                if frame is None or len(frame) >= max_rows:
                    return None
                if frame.empty:
                    continue
                if (not {"trade_date", "ts_code", "limit"}.issubset(frame.columns)
                        or not frame["trade_date"].astype(str).eq(day).all()
                        or not frame["limit"].astype(str).eq(typ).all()):
                    return None
                rows.append(frame)
            return pd.concat(rows, ignore_index=True) if rows else None

        def _request(batch):
            if not batch:
                return
            start, end = batch[0], batch[-1]
            kwargs = {"trade_date": start} if len(batch) == 1 else {"start_date": start, "end_date": end}
            if tag is not None:
                kwargs["tag"] = tag
            try:
                df = getattr(self.pro, source)(**kwargs)
                # A full response can truncate in the middle of a day. Never cache it.
                needs_split = df is not None and len(df) >= max_rows
            except Exception as e:
                log.warning("%s(%s, %s-%s) 获取失败: %s", source, tag, start, end, e)
                needs_split = True
                df = None
            if needs_split:
                if len(batch) > 1:
                    mid = len(batch) // 2
                    _request(batch[:mid])
                    _request(batch[mid:])
                    return
                if df is None:
                    return
                df = _split_limit_types(start) if source == "limit_list_d" else None
                if df is None:
                    log.warning("%s(%s, %s) 已达返回行数上限，忽略不完整响应", source, tag, start)
                    return
            if df is None or df.empty or not {"trade_date", "ts_code"}.issubset(df.columns):
                return
            batch_set = set(batch)
            dates = df["trade_date"].astype(str)
            for d, g in df.groupby(dates):
                if d in batch_set:
                    g = g.assign(trade_date=d)
                    if source == "limit_list_d" and not _valid_limit_data(g):
                        continue
                    g.to_csv(paths[d], index=False)
                    copies[d] = g

        # Only consecutive missing sessions are batched. Final cached dates are neither
        # refetched nor overwritten; same-day provisional files are (see _provisional).
        # A successful empty interval is not called again.
        batch = []
        for d in days:
            if d not in pending:
                _request(batch)
                batch = []
                continue
            batch.append(d)
            if len(batch) == RANGE_TRADE_DAYS:
                _request(batch)
                batch = []
        _request(batch)
        return copies

    def get_limit_list(self, trade_date: str, refresh: bool = False) -> pd.DataFrame:
        return self._load_range([trade_date], "limit_list_d", refresh_days=[trade_date] if refresh else ())[trade_date]

    def get_kpl_day(self, trade_date: str, tag: str = "涨停", refresh: bool = False) -> pd.DataFrame:
        return self._load_range([trade_date], "kpl_list", tag, [trade_date] if refresh else ())[trade_date]

    def ensure_limit_range(self, trade_days: list[str], refresh_latest: bool = False):
        """Preload U/Z/D and enrich only tags present in the source universe."""
        if not trade_days:
            return {}, {}, {}
        refresh = [max(trade_days)] if refresh_latest else []
        limits = self._load_range(trade_days, "limit_list_d", refresh_days=refresh)
        return self._enrichment_for_limits(limits, refresh)

    def _enrichment_for_limits(self, limits, refresh=()):
        # Keep the full timeline so consecutive cache holes don't accidentally bridge
        # long date intervals where that tag has no limit_list_d observations.
        def _load_tag(tag, lim_type):
            eligible = {d for d, df in limits.items() if "limit" in df and
                        df["limit"].astype(str).str.strip().str.upper().eq(lim_type).any()}
            out, chunk = {}, []
            for d in sorted(limits):
                if d in eligible:
                    chunk.append(d)
                elif chunk:
                    out.update(self._load_range(chunk, "kpl_list", tag, refresh))
                    chunk = []
            if chunk:
                out.update(self._load_range(chunk, "kpl_list", tag, refresh))
            return out

        return limits, _load_tag("涨停", "U"), _load_tag("炸板", "Z")

    def get_sw_daily(self, trade_date: str, refresh: bool = False) -> pd.DataFrame:
        """获取单日申万行业指数行情 (sw_daily)。"""
        f = self.cache_dir / f"sw_daily_{trade_date}.csv"
        if f.exists() and (self.offline or not (refresh or self._provisional(trade_date, f))):
            try:
                return pd.read_csv(f, dtype={"trade_date": str, "ts_code": str})
            except Exception:
                pass
        if self.offline:
            return pd.DataFrame()
        try:
            df = self.pro.sw_daily(trade_date=trade_date)
            if df is not None and not df.empty:
                df["trade_date"] = df["trade_date"].astype(str)
                df["name"] = df["name"].map(strip_roman)
                df.to_csv(f, index=False)
                return df
        except Exception as e:
            log.warning("sw_daily(%s) 获取失败: %s", trade_date, e)
        return self._read_daily_cache(f) if f.exists() else pd.DataFrame()  # 重取失败时沿用已有缓存

    def get_stock_daily(self, trade_date: str, refresh: bool = False) -> pd.DataFrame:
        """获取单日全市场个股日线 (ts_code, open, close, pre_close, pct_chg, amount)。"""
        f = self.cache_dir / f"daily_{trade_date}.csv"
        if f.exists() and (self.offline or not (refresh or self._provisional(trade_date, f))):
            try:
                return pd.read_csv(f, dtype={"trade_date": str, "ts_code": str})
            except Exception:
                pass
        if self.offline:
            return pd.DataFrame()
        try:
            df = self.pro.daily(
                trade_date=trade_date,
                fields="ts_code,trade_date,open,close,pre_close,pct_chg,amount",
            )
            if df is not None and not df.empty:
                df["trade_date"] = df["trade_date"].astype(str)
                df.to_csv(f, index=False)
                return df
        except Exception as e:
            log.warning("daily(%s) 获取失败: %s", trade_date, e)
        return self._read_daily_cache(f) if f.exists() else pd.DataFrame()  # 重取失败时沿用已有缓存


# ======================================================================
# 分析与组装引擎
# ======================================================================

def _valid_limit_data(df: pd.DataFrame | None) -> bool:
    return (df is not None and not df.empty and {"ts_code", "limit"}.issubset(df.columns)
            and (df["ts_code"].map(_stock_code).ne("") &
                 df["limit"].map(_text).str.upper().isin(("U", "Z", "D"))).any().item())


def _sw_lookup(sw_members: pd.DataFrame | None) -> dict:
    if sw_members is None or "ts_code" not in sw_members:
        return {}
    return {_stock_code(r.ts_code): {
        f"{level}_{part}": (strip_roman if part == "name" else _text)(getattr(r, f"{level}_{part}", ""))
        for level in ("l1", "l2", "l3") for part in ("code", "name")
    } for r in sw_members.itertuples(index=False) if _stock_code(r.ts_code)}


def _board_sort(item):
    return (-(item["limit_times"] or 0), item["first_time"] if item["first_time"] != "—" else "99:99:99",
            item["fd_yi"] is None, -(item["fd_yi"] or 0), item["ts_code"])


def enrich_limit_stocks(
    lim_df: pd.DataFrame,
    kpl_u_df: pd.DataFrame,
    kpl_z_df: pd.DataFrame,
    sw_members: pd.DataFrame,
    include_kpl_extra: bool = False,
    *, sw_lookup: dict | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Return U/Z/D from limit_list_d only; legacy include_kpl_extra has no effect."""
    if not _valid_limit_data(lim_df):
        return [], [], []
    sw_lookup = _sw_lookup(sw_members) if sw_lookup is None else sw_lookup

    def kpl_map(frame, tag):
        if frame is None or "ts_code" not in frame:
            return {}
        return {(_text(r.get("trade_date")), _stock_code(r["ts_code"])): r
                for r in frame.to_dict("records")
                if _stock_code(r["ts_code"]) and _text(r.get("tag")) in ("", tag)}

    enrichment = {"U": kpl_map(kpl_u_df, "涨停"), "Z": kpl_map(kpl_z_df, "炸板"), "D": {}}
    lists = {"U": [], "Z": [], "D": []}
    seen = set()
    for row in lim_df.to_dict("records"):
        code = _stock_code(row.get("ts_code"))
        typ = _text(row.get("limit")).upper()
        if not code or typ not in lists or (code, typ) in seen:
            continue
        seen.add((code, typ))
        name = _text(row.get("name"))
        sw = sw_lookup.get(code, {})
        day = _text(row.get("trade_date"))
        kpl = enrichment[typ].get((day, code), {})
        lu_desc, theme = _text(kpl.get("lu_desc")), _text(kpl.get("theme"))
        concepts = parse_concepts(lu_desc, theme)

        height = _integer(row.get("limit_times"), minimum=1) if typ != "Z" else 0
        up_stat = _text(row.get("up_stat"))
        stat = parse_up_stat(up_stat, height) if typ == "U" else {
            "days": None, "boards": None, "is_trend": None,
            "label": "炸板" if typ == "Z" else (f"{height}连跌" if height else "跌停"),
        }
        amount = _num(row.get("amount"))
        fd = _num(row.get("fd_amount"))
        float_mv = _num(row.get("float_mv"))
        first_t, last_t = fmt_time(row.get("first_time")), fmt_time(row.get("last_time"))
        opens = _integer(row.get("open_times"))
        style = classify_board_style(first_t, last_t, opens) if typ == "U" else stat["label"] if typ == "Z" else "跌停"

        item = {
            "ts_code": code,
            "name": name,
            "board_tag": board_type_tag(code, name),
            "close": _round(row.get("close")),
            "pct_chg": _round(row.get("pct_chg")),
            "amount_yi": _scaled(amount, 1e8),
            "fd_yi": _scaled(fd, 1e8),
            "float_mv_yi": _scaled(float_mv, 1e8),
            "total_mv_yi": _scaled(row.get("total_mv"), 1e8),
            "turnover": _round(row.get("turnover_ratio")),
            "net_change_yi": _scaled(kpl.get("net_change"), 1e8),
            "seal_to_amount": _round(fd / amount * 100, 1) if fd is not None and amount and amount > 0 else None,
            "seal_to_mv": _round(fd / float_mv * 100) if fd is not None and float_mv and float_mv > 0 else None,
            "first_time": first_t,
            "last_time": last_t,
            "open_times": opens,
            "style": style,
            "limit_times": height,
            "up_stat": up_stat,
            "stat_label": stat["label"],
            "is_trend": stat["is_trend"],
            "trend_days": stat["days"],
            "trend_boards": stat["boards"],
            "limit": typ,
            "industry": strip_roman(row.get("industry")),
            **{f"sw_{l}": sw.get(f"{l}_name") or "未分类" for l in ("l1", "l2", "l3")},
            **{f"sw_{l}_code": sw.get(f"{l}_code", "") for l in ("l1", "l2", "l3")},
            "lu_desc": lu_desc,
            "theme": theme,
            "concepts": concepts,
            "primary_concept": concepts[0] if concepts else "",
        }
        lists[typ].append(item)

    lists["U"].sort(key=_board_sort)
    lists["Z"].sort(key=lambda x: (x["amount_yi"] is None, -(x["amount_yi"] or 0), -(x["pct_chg"] or 0)))
    lists["D"].sort(key=_board_sort)
    return lists["U"], lists["Z"], lists["D"]


# None means the identity or height data cannot establish promotion, not a failure.
def _promotion_match(previous: dict, current: dict | None) -> bool | None:
    before = _integer(previous.get("limit_times"), minimum=1)
    if before is None:
        return None
    if current is None:
        return False
    after = _integer(current.get("limit_times"), minimum=1)
    return after == before + 1 if after is not None else None


def _promotion_rates(today_up: list[dict], prev_up: list[dict]) -> dict:
    today_by_code = {x["ts_code"]: x for x in today_up}
    outcomes = [(s.get("limit_times"), _promotion_match(s, today_by_code.get(s["ts_code"]))) for s in prev_up]

    def rate(height=None):
        eligible = [ok for h, ok in outcomes if ok is not None and (height is None or h == height)]
        return _round(sum(eligible) / len(eligible) * 100, 1) if eligible else None

    return {"promo_rate": rate(), "b1_to_b2_rate": rate(1), "b2_to_b3_rate": rate(2)}


def compute_promotion_and_yesterday_effect(
    today_up: list[dict],
    today_zha: list[dict],
    prev_up: list[dict],
    today_daily_df: pd.DataFrame,
    *,
    available: bool = True,
    today_down: list[dict] = (),
) -> dict:
    """Evaluate the immediately preceding trading day's limit-up cohort."""
    result = {
        "available": available,
        "prev_count": len(prev_up) if available else None,
        "eligible_count": 0 if available else None,
        "unknown_count": 0 if available else None,
        "promoted_count": 0 if available else None,
        "overall_promo_rate": None,
        "avg_pct_chg": None,
        "avg_open_pct": None,
        "up_close_ratio": None,
        "up_Close_ratio": None,  # remains an alias for older snapshots
        "big_loss_count": None,
        "big_loss_ratio": None,
        "tiers": [],
        "broken_high_boards": [],
    }
    if not available or not prev_up:
        return result

    up_map = {x["ts_code"]: x for x in today_up}
    zha_map = {x["ts_code"]: x for x in today_zha}
    down_map = {x["ts_code"]: x for x in today_down}
    prior_codes = {x["ts_code"] for x in prev_up}
    daily_map = {}
    if today_daily_df is not None and "ts_code" in today_daily_df:
        for r in today_daily_df[today_daily_df["ts_code"].isin(prior_codes)].to_dict("records"):
            code = _stock_code(r["ts_code"])
            open_p, pre_p = _num(r.get("open")), _num(r.get("pre_close"))
            daily_map[code] = {
                "pct_chg": _num(r.get("pct_chg")),
                "open_pct": (open_p / pre_p - 1) * 100 if open_p is not None and pre_p and pre_p > 0 else None,
                "amount_yi": _scaled(r.get("amount"), 1e5),  # 千元 → 亿元
            }

    tier_defs = (
        ("1to2", "一进二 (首板晋级)"), ("2to3", "二进三 (二板晋级)"),
        ("3to4", "三进四 (三板晋级)"), ("4plus", "四板及以上晋级"),
    )
    tiers = {i: [] for i in range(1, 5)}
    entries = []

    def first_number(*values):
        return next((v for v in map(_num, values) if v is not None), None)

    def mean(values):
        return _round(float(np.mean(values))) if values else None

    for prev in prev_up:
        code = prev["ts_code"]
        info = daily_map.get(code, {})
        up, zha, down = up_map.get(code), zha_map.get(code), down_map.get(code)
        source = up or zha or down or {}
        before = _integer(prev.get("limit_times"), minimum=1)
        promoted = _promotion_match(prev, up)
        entry = {
            "ts_code": code,
            "name": prev["name"],
            "prev_boards": before,
            "today_boards": up.get("limit_times") if up else 0,
            "promoted": promoted,
            "is_zha": zha is not None,
            "today_pct": first_number(info.get("pct_chg"), source.get("pct_chg")),
            "today_open_pct": info.get("open_pct"),
            "today_amount_yi": first_number(source.get("amount_yi"), info.get("amount_yi")),
            "today_first_time": source.get("first_time", "—"),
            "today_style": up.get("style", "—") if up else ("冲板被砸" if zha else "断板"),
            **{k: prev.get(k, "") for k in ("sw_l1", "sw_l2", "sw_l3", "primary_concept")},
            "concepts": prev.get("concepts", []),
        }
        entries.append(entry)
        if before is not None:
            tiers[min(4, before)].append(entry)

    pcts = [x["today_pct"] for x in entries if x["today_pct"] is not None]
    open_pcts = [x["today_open_pct"] for x in entries if x["today_open_pct"] is not None]
    broken_high = []
    for i, (key, label) in enumerate(tier_defs, 1):
        cohort = tiers[i]
        success = [x for x in cohort if x["promoted"] is True]
        failed = [x for x in cohort if x["promoted"] is False]
        unknown = [x for x in cohort if x["promoted"] is None]
        n = len(success) + len(failed)
        cohort_pcts = [x["today_pct"] for x in cohort if x["today_pct"] is not None]
        for x in cohort:
            x["today_pct"] = _round(x["today_pct"])
            x["today_open_pct"] = _round(x["today_open_pct"])
        result["tiers"].append({
            "key": key,
            "label": label,
            "from_board": i,
            "prev_count": len(cohort),
            "eligible_count": n,
            "success_count": len(success),
            "failed_count": len(failed),
            "unknown_count": len(unknown),
            "rate": _round(len(success) / n * 100, 1) if n else None,
            "avg_pct": mean(cohort_pcts),
            "success": sorted(success, key=lambda x: (-x["today_boards"], x["today_first_time"])),
            "failed": sorted(failed, key=lambda x: (-x["prev_boards"], x["today_pct"] is None, -(x["today_pct"] or 0))),
            "unknown": unknown,
        })
        if i >= 2:
            broken_high.extend(failed)

    up_cnt = sum(x > 0 for x in pcts)
    big_loss_cnt = sum(x <= -5 for x in pcts)
    ups_ratio = _round(up_cnt / len(pcts) * 100, 1) if pcts else None
    result.update(
        eligible_count=sum(x["promoted"] is not None for x in entries),
        unknown_count=sum(x["promoted"] is None for x in entries),
        promoted_count=sum(x["promoted"] is True for x in entries),
        overall_promo_rate=_promotion_rates(today_up, prev_up)["promo_rate"],
        avg_pct_chg=mean(pcts),
        avg_open_pct=mean(open_pcts),
        quote_count=len(pcts),
        up_close_ratio=ups_ratio,
        up_Close_ratio=ups_ratio,
        big_loss_count=big_loss_cnt if pcts else None,
        big_loss_ratio=_round(big_loss_cnt / len(pcts) * 100, 1) if pcts else None,
        broken_high_boards=sorted(broken_high, key=lambda x: (-x["prev_boards"], x["today_pct"] is None, x["today_pct"] or 0)),
    )
    return result


def build_ladder(up_list: list[dict]) -> list[dict]:
    """构建连板天梯：按最高连板逐级往下到首板。"""
    if not up_list:
        return []
    by_board: dict[int, list[dict]] = {}
    for item in up_list:
        b = item.get("limit_times")
        if b is not None:
            by_board.setdefault(b, []).append(item)

    ladder = []
    for b in sorted(by_board.keys(), reverse=True):
        stocks = by_board[b]
        # 统计该层的主要申万二级行业与核心概念
        sw_counts: dict[str, int] = {}
        cpt_counts: dict[str, int] = {}
        for s in stocks:
            sw_counts[s["sw_l2"]] = sw_counts.get(s["sw_l2"], 0) + 1
            for c in s["concepts"]:
                cpt_counts[c] = cpt_counts.get(c, 0) + 1
        top_sw = sorted(sw_counts.items(), key=lambda kv: -kv[1])[:4]
        top_cpt = sorted(cpt_counts.items(), key=lambda kv: -kv[1])[:4]

        ladder.append({
            "board": b,
            "label": "首板 (1板)" if b == 1 else f"{b}连板",
            "count": len(stocks),
            "amount_yi": _sum_field(stocks, "amount_yi"),
            "fd_yi": _sum_field(stocks, "fd_yi"),
            "avg_turnover": _round(float(np.mean(turnovers))) if (turnovers := [s["turnover"] for s in stocks if s["turnover"] is not None]) else None,
            "top_sw": [{"name": k, "count": v} for k, v in top_sw],
            "top_concepts": [{"name": k, "count": v} for k, v in top_cpt],
            "stocks": stocks,
        })
    return ladder


def build_sw_capital_matrix(
    up_list: list[dict],
    zha_list: list[dict],
    down_list: list[dict],
    sw_daily_df: pd.DataFrame,
    level: str = "l2",
) -> list[dict]:
    """按申万行业 (l1 / l2 / l3) 聚合资金与连板身位分布：
    - 涨停家数、炸板家数、跌停家数、封板率
    - 身位拆解：首板 (b1)、二板 (b2)、三板 (b3)、四板及以上 (b4p)、连板家数 (b2+)
    - 最高连板高度与龙头股
    - 涨停股总成交额 (亿元)、涨停股总封单额 (亿元)、主力净流入合计 (亿元)
    - 申万行业指数当日涨跌幅 (%)、行业全天总成交额 (亿元)、涨停股成交占行业总成交比例 (%)
    - 该行业内部关联度最高的前 4 个热门概念
    - 综合资金进攻得分 capital_score（用于排序与定位资金主攻方向）
    """
    name_key = f"sw_{level}"
    code_key = f"sw_{level}_code"

    sw_idx_map: dict[str, dict] = {}
    if sw_daily_df is not None and not sw_daily_df.empty:
        for _, r in sw_daily_df.iterrows():
            c = str(r.get("ts_code", ""))
            nm = strip_roman(r.get("name", ""))
            # sw_daily.amount 单位为万元，÷ 1e4 为亿元；float_mv 单位万元，÷ 1e4 为亿元
            info = {
                "code": c,
                "name": nm,
                "pct_change": _round(r.get("pct_change"), 2),
                "index_amount_yi": _scaled(r.get("amount"), 1e4),
                "index_mv_yi": _scaled(r.get("float_mv"), 1e4, 1),
            }
            if c:
                sw_idx_map[c] = info

    groups: dict[str, dict] = {}

    def _ensure_group(item: dict) -> dict:
        ind_name = item.get(name_key) or "其他"
        ind_code = item.get(code_key) or ""
        key = ind_code or ind_name
        if key not in groups:
            idx_info = sw_idx_map.get(ind_code, {})
            groups[key] = {
                "name": ind_name,
                "code": ind_code or idx_info.get("code", ""),
                "level": level.upper(),
                "l1_name": item.get("sw_l1", ""),
                "l2_name": item.get("sw_l2", ""),
                "up_count": 0,
                "zha_count": 0,
                "down_count": 0,
                "b1": 0,
                "b2": 0,
                "b3": 0,
                "b4p": 0,
                "multi_count": 0,  # >=2板
                "trend_count": 0,  # N天M板
                "max_board": None,
                "leader": None,
                "index_pct": idx_info.get("pct_change"),
                "index_amount_yi": idx_info.get("index_amount_yi"),
                "index_mv_yi": idx_info.get("index_mv_yi"),
                "concepts": {},
                "stocks": [],
                "zha_stocks": [],
            }
        return groups[key]

    for s in up_list:
        g = _ensure_group(s)
        g["up_count"] += 1
        b = s.get("limit_times")
        if b == 1:
            g["b1"] += 1
        elif b == 2:
            g["b2"] += 1
        elif b == 3:
            g["b3"] += 1
        elif b is not None and b >= 4:
            g["b4p"] += 1
        if b is not None and b >= 2:
            g["multi_count"] += 1
        if s.get("is_trend"):
            g["trend_count"] += 1
        if b is not None and (g["max_board"] is None or b > g["max_board"]):
            g["max_board"] = b
            g["leader"] = {
                "ts_code": s["ts_code"],
                "name": s["name"],
                "board": b,
                "stat_label": s["stat_label"],
                "first_time": s["first_time"],
                "primary_concept": s["primary_concept"],
            }
        for c in s.get("concepts", []):
            if c:
                g["concepts"][c] = g["concepts"].get(c, 0) + 1
        g["stocks"].append(s)

    for s in zha_list:
        g = _ensure_group(s)
        g["zha_count"] += 1
        g["zha_stocks"].append(s)

    for s in down_list:
        _ensure_group(s)["down_count"] += 1

    out: list[dict] = []
    for g in groups.values():
        total_attempt = g["up_count"] + g["zha_count"]
        seal_rate = _round(g["up_count"] / total_attempt * 100, 1) if total_attempt > 0 else None
        up_amt = _sum_field(g["stocks"], "amount_yi")
        fd_amt = _sum_field(g["stocks"], "fd_yi")
        idx_amt = g["index_amount_yi"]
        amt_concentration = _round(up_amt / idx_amt * 100, 1) if up_amt is not None and idx_amt and idx_amt > 0 else None
        board_stocks = {"b1": [], "b2": [], "b3": [], "b4p": [], "unknown": []}
        for s in g["stocks"]:
            height = s["limit_times"]
            bucket = "unknown" if height is None else (f"b{height}" if height < 4 else "b4p")
            board_stocks[bucket].append(s)

        top_cpts = sorted(g["concepts"].items(), key=lambda kv: -kv[1])[:5]
        # 资金主攻强度评分：涨停家数 × 10 + 连板家数 × 15 + 最高板 × 8 + 封单额(亿) × 2 + 涨停成交额(亿) × 0.4
        capital_score = round(
            g["up_count"] * 10.0
            + g["multi_count"] * 15.0
            + (g["max_board"] or 0) * 8.0
            + fd_amt * 2.0
            + up_amt * 0.4,
            1,
        ) if fd_amt is not None and up_amt is not None and not board_stocks["unknown"] else None

        # 判断资金身位位置：主升期(多连板梯队完整) / 发酵期(首板二板密集) / 高位抱团(仅高板独苗) / 分歧炸板
        if g["up_count"] == 0 and g["zha_count"] > 0:
            stage = "冲高分歧"
            stage_tone = "cold"
        elif g["multi_count"] >= 2 and g["b1"] >= 2:
            stage = "主升梯队"
            stage_tone = "up"
        elif (g["max_board"] or 0) >= 3 and g["b1"] <= 1:
            stage = "高位领涨"
            stage_tone = "warm"
        elif g["b1"] >= 3 and g["multi_count"] <= 1:
            stage = "首板发酵"
            stage_tone = "up"
        elif g["up_count"] == 0 and g["down_count"]:
            stage = "仅跌停"
            stage_tone = "cold"
        elif g["up_count"] >= 2:
            stage = "局部活跃"
            stage_tone = "warm"
        else:
            stage = "零星试盘"
            stage_tone = "muted"

        out.append({
            "name": g["name"],
            "code": g["code"],
            "level": g["level"],
            "l1_name": g["l1_name"],
            "l2_name": g["l2_name"],
            "up_count": g["up_count"],
            "zha_count": g["zha_count"],
            "down_count": g["down_count"],
            "seal_rate": seal_rate,
            "b1": g["b1"],
            "b2": g["b2"],
            "b3": g["b3"],
            "b4p": g["b4p"],
            "multi_count": g["multi_count"],
            "trend_count": g["trend_count"],
            "max_board": g["max_board"] if g["up_count"] else 0,
            "leader": g["leader"],
            "board_capital": {
                b: {"count": len(stocks), "amount_yi": _sum_field(stocks, "amount_yi"),
                    "fd_yi": _sum_field(stocks, "fd_yi")}
                for b, stocks in board_stocks.items()
            },
            "up_amount_yi": up_amt,
            "fd_yi": fd_amt,
            "net_change_yi": _sum_field(g["stocks"], "net_change_yi"),
            "index_pct": g["index_pct"],
            "index_amount_yi": idx_amt,
            "index_mv_yi": g["index_mv_yi"],
            "amt_concentration": amt_concentration,
            "top_concepts": [{"name": k, "count": v} for k, v in top_cpts],
            "capital_score": capital_score,
            "stage": stage,
            "stage_tone": stage_tone,
            "stocks": g["stocks"],
            "zha_stocks": g["zha_stocks"],
        })

    out.sort(key=lambda x: (-x["up_count"], -x["multi_count"], -(x["max_board"] or 0),
                            x["fd_yi"] is None, -(x["fd_yi"] or 0), -(x["up_amount_yi"] or 0), x["name"]))
    return out


def build_concept_ranking_and_cross(
    up_list: list[dict],
    zha_list: list[dict],
) -> tuple[list[dict], dict]:
    """Rank sourced concepts and build L1/L2/L3 × concept matrices."""
    stocks_by_concept: dict[str, list[dict]] = {}
    for s in up_list:
        for c in s["concepts"]:
            stocks_by_concept.setdefault(c, []).append(s)
    zha_by_concept: dict[str, int] = {}
    for s in zha_list:
        for c in s["concepts"]:
            zha_by_concept[c] = zha_by_concept.get(c, 0) + 1

    def _counts(stocks, key):
        counts = {}
        for s in stocks:
            value = s[key]
            if value != "未分类":
                counts[value] = counts.get(value, 0) + 1
        return counts

    concepts = []
    for c, stocks in stocks_by_concept.items():
        stocks = sorted(stocks, key=_board_sort)
        boards = [s["limit_times"] for s in stocks if s["limit_times"] is not None]
        leader = stocks[0] if boards else None
        concepts.append({
            "name": c,
            "up_count": len(stocks),
            "primary_count": sum(s["primary_concept"] == c for s in stocks),
            "zha_count": zha_by_concept.get(c, 0),
            "b1": boards.count(1), "b2": boards.count(2), "b3": boards.count(3),
            "b4p": sum(n >= 4 for n in boards), "multi_count": sum(n >= 2 for n in boards),
            "unknown_height_count": len(stocks) - len(boards),
            "max_board": max(boards, default=None),
            "leader": {**{k: leader[k] for k in ("ts_code", "name", "stat_label", "sw_l2")},
                       "board": leader["limit_times"]} if leader else None,
            "up_amount_yi": _sum_field(stocks, "amount_yi"),
            "fd_yi": _sum_field(stocks, "fd_yi"),
            **{f"sw_{l}_breakdown": [
                {"name": k, "count": v} for k, v in sorted(_counts(stocks, f"sw_{l}").items(), key=lambda p: (-p[1], p[0]))[:size]
            ] for l, size in (("l1", 3), ("l2", 4), ("l3", 4))},
            "stocks": stocks,
        })
    concepts.sort(key=lambda x: (-x["up_count"], -x["primary_count"], -(x["max_board"] or 0),
                                 x["fd_yi"] is None, -(x["fd_yi"] or 0), x["name"]))
    top_concepts = concepts[:14]
    top_names = {c["name"] for c in top_concepts}
    concept_list = [{"name": c["name"], "total": c["up_count"]} for c in top_concepts]

    levels = {}
    for level in ("l1", "l2", "l3"):
        key = f"sw_{level}"
        counts = _counts(up_list, key)
        industries = sorted(counts, key=lambda industry: (-counts[industry], industry))[:14]
        cell_map: dict[tuple[str, str], list[dict]] = {}
        for s in up_list:
            industry = s[key]
            if industry in industries:
                for concept in s["concepts"]:
                    if concept in top_names:
                        cell_map.setdefault((industry, concept), []).append(s)
        cells = []
        for industry in industries:
            for concept in top_concepts:
                name = concept["name"]
                stocks = cell_map.get((industry, name))
                if not stocks:
                    continue
                cells.append({
                    key: industry, "industry": industry, "concept": name, "count": len(stocks),
                    "max_board": max((s["limit_times"] for s in stocks if s["limit_times"] is not None), default=None),
                    "amount_yi": _sum_field(stocks, "amount_yi"), "fd_yi": _sum_field(stocks, "fd_yi"),
                    "stocks": [{k: s[k] for k in ("ts_code", "name", "limit_times", "stat_label", "first_time", "fd_yi", "amount_yi")}
                               for s in sorted(stocks, key=_board_sort)],
                })
        industry_list = [{"name": i, "total": counts[i]} for i in industries]
        levels[level.upper()] = {"level": level.upper(), "industry_list": industry_list,
                                 f"{key}_list": industry_list, "concept_list": concept_list, "cells": cells}
    return concepts, {**levels["L2"], "levels": levels}


def build_capital_position_diagnosis(
    overview: dict,
    ladder: list[dict],
    sw_l2_matrix: list[dict],
    concepts: list[dict],
    promo: dict,
) -> dict:
    """生成规则化的「资金位置与情绪周期诊断」（不调 AI，完全基于量化规则与盘面客观结构）。"""
    up_n = overview.get("up_count") or 0
    zha_n = overview.get("zha_count") or 0
    down_n = overview.get("down_count") or 0
    seal_rate = overview.get("seal_rate")
    max_b = overview.get("max_board")
    seal_display = f"{seal_rate:.1f}%" if seal_rate is not None else "—"
    height_display = f"{max_b} 板" if max_b is not None else "—"
    multi_n = overview.get("multi_count") or 0
    b1_n = overview.get("b1_count") or 0
    avg_premium = promo.get("avg_pct_chg")
    promo_rate = promo.get("overall_promo_rate")

    # 1. 情绪周期阶段判定
    if up_n < 30 or down_n >= 25 or (avg_premium is not None and avg_premium < -1.5):
        cycle_phase = "退潮冰点期"
        cycle_tone = "cold"
    elif (max_b or 0) >= 6 and seal_rate is not None and seal_rate >= 72 and multi_n >= 12 and (avg_premium is None or avg_premium >= 1.5):
        cycle_phase = "主升亢奋期"
        cycle_tone = "up"
    elif zha_n >= 35 or (seal_rate is not None and seal_rate < 62) or (promo.get("big_loss_ratio") or 0) >= 20:
        cycle_phase = "高位分歧期"
        cycle_tone = "warm"
    elif (max_b or 0) >= 4 and multi_n >= 8:
        cycle_phase = "结构发酵期"
        cycle_tone = "up"
    else:
        cycle_phase = "轮动试盘期"
        cycle_tone = "warm"

    # 2. 资金身位重心判定（高位抱团 vs 中位接力 vs 首板高低切）
    b1_ratio = (b1_n / up_n * 100) if up_n > 0 else 0
    b3p_n = sum(x["count"] for x in ladder if x["board"] >= 3)
    if b1_ratio >= 78 and b3p_n <= 4:
        position_focus = "高低切 · 首板挖掘"
        position_desc = f"首板占比达 {b1_ratio:.0f}%（{b1_n} 家），3板及以上仅 {b3p_n} 家，资金偏向低位首板轮动与高低切换。"
    elif b3p_n >= 8 and (promo_rate or 0) >= 25:
        position_focus = "高位接力 · 连板强趋势"
        position_desc = f"3板及以上高标达 {b3p_n} 家，连板池共 {multi_n} 家，接力资金活跃，中高位晋级意愿强。"
    elif multi_n >= 10:
        position_focus = "中位进阶 · 1进2/2进3主战场"
        position_desc = f"连板股 {multi_n} 家（占涨停 {multi_n / up_n * 100:.0f}%），资金围绕二板、三板构建主线梯队。"
    else:
        position_focus = "散点试盘 · 缺乏连板中军"
        position_desc = f"连板股仅 {multi_n} 家，最高 {height_display}，资金以日内首板套利为主。"

    # 3. 申万行业主线集中度
    top3_sw = [r for r in sw_l2_matrix if r["up_count"] and r["name"] != "未分类"][:3]
    top3_sw_up = sum(x["up_count"] for x in top3_sw)
    top3_sw_share = _round(top3_sw_up / up_n * 100, 1) if top3_sw else None
    share_display = f"{top3_sw_share:.1f}%" if top3_sw_share is not None else "—"
    top3_names = "、".join(f"{x['name']}({x['up_count']}家)" for x in top3_sw)

    # 4. 热门概念共振方向
    top3_cpt = concepts[:3]
    top3_cpt_names = "、".join(f"{x['name']}({x['up_count']}家)" for x in top3_cpt)

    bullets = [
        f"【情绪与接力】今日封板率 {seal_display}（涨停 {up_n} / 炸板 {zha_n} / 跌停 {down_n}），最高空间板 {height_display}"
        + (f"，昨日涨停今日平均溢价 {avg_premium:+.2f}%、总晋级率 {promo_rate:.1f}%。" if avg_premium is not None and promo_rate is not None else "。"),
        f"【资金身位位置】{position_focus} —— {position_desc}",
        f"【申万行业主阵地】前三大申万二级行业为 {top3_names or '—'}，合计占本口径涨停 {share_display}。",
        f"【题材概念风口】最强催化概念为 {top3_cpt_names or '—'}。",
    ]

    return {
        "cycle_phase": cycle_phase,
        "cycle_tone": cycle_tone,
        "position_focus": position_focus,
        "position_desc": position_desc,
        "top3_sw_share": top3_sw_share,
        "top3_sw_summary": top3_names,
        "top3_concept_summary": top3_cpt_names,
        "bullets": bullets,
    }


def _daily_board_summary(up, zha, down, *, available=True):
    boards = [x["limit_times"] for x in up if x["limit_times"] is not None]
    max_b = max(boards, default=None if up else 0)
    n, z = len(up), len(zha)
    leaders = [x for x in up if x["limit_times"] == max_b] if max_b else []
    summary = {
        "up_count": n, "zha_count": z, "down_count": len(down),
        "seal_rate": _round(n / (n + z) * 100, 1) if n + z else None,
        "b1": boards.count(1), "b2": boards.count(2), "b3": boards.count(3),
        "b4p": sum(x >= 4 for x in boards), "multi_count": sum(x >= 2 for x in boards),
        "unknown_height_count": n - len(boards),
        "trend_count": sum(x["is_trend"] is True for x in up),
        "unknown_trend_count": sum(x["is_trend"] is None for x in up),
        "max_board": max_b, "up_amount_yi": _sum_field(up, "amount_yi"),
        "fd_yi": _sum_field(up, "fd_yi"),
    }
    if not available:
        summary = dict.fromkeys(summary)
    return summary, leaders


def _date_md(d):
    return f"{int(d[4:6])}/{int(d[6:8])}"


def _format_date(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d else None


def _snapshots(limits, kpl_up, kpl_z, sw_members):
    sw_lookup = _sw_lookup(sw_members)
    return {d: enrich_limit_stocks(df, kpl_up.get(d), kpl_z.get(d), sw_members, sw_lookup=sw_lookup)
            if _valid_limit_data(df) else None for d, df in limits.items()}


def build_multi_day_history(
    store: LimitDataStore,
    history_days: list[str],
    sw_members: pd.DataFrame,
    *,
    snapshots=None,
    previous_day: str | None = None,
) -> dict:
    """Summarize adjacent trading days. Missing observations remain explicit gaps."""
    history_days = sorted(set(history_days))
    if snapshots is None:
        requested = ([previous_day] if previous_day else []) + history_days
        snapshots = _snapshots(*store.ensure_limit_range(requested), sw_members)

    def _rotation_rows(axis, complete_days, totals, top_n):
        def score(name):
            latest = axis.get(history_days[-1], {}).get(name, 0)
            recent = sum(axis.get(d, {}).get(name, 0) for d in history_days[-3:])
            return (-(3 * latest + 2 * recent + totals[name]), -latest, -totals[name], name)

        rows = []
        for name in sorted(totals, key=score)[:top_n]:
            series = [axis.get(d, {}).get(name, 0 if d in complete_days else None) for d in history_days]
            recent = [n for n in series[-3:] if n is not None]
            observed = [n for n in series if n is not None]
            rows.append({
                "name": name, "total": totals[name], "latest": series[-1],
                "recent3": sum(recent) if recent else None,
                "max_day": max(observed, default=None), "active_days": sum(n > 0 for n in observed),
                "series": series,
            })
        return rows

    axes = {k: {} for k in ("l1", "l2", "l3", "concept")}
    totals_by_axis = {k: {} for k in axes}
    complete_by_axis = {k: set() for k in axes}
    previous = snapshots.get(previous_day)
    records, missing = [], []
    for d in history_days:
        current = snapshots.get(d)
        available = current is not None
        up, zha, down = current if available else ([], [], [])
        summary, leaders = _daily_board_summary(up, zha, down, available=available)
        summary.update(
            date=d, date_md=_date_md(d), available=available,
            leader_names=" / ".join(x["name"] for x in leaders[:2]) or "—",
            leader_sw=leaders[0]["sw_l2"] if leaders else "—",
            leader_concept=leaders[0]["primary_concept"] if leaders else "—",
            **(_promotion_rates(up, previous[0]) if available and previous is not None else
               dict.fromkeys(("promo_rate", "b1_to_b2_rate", "b2_to_b3_rate"))),
        )
        records.append(summary)
        if available:
            for k, axis in axes.items():
                # If any limit-up has no sourced concept/industry, an observed
                # positive count is still useful, but an unmatched cell is unknown.
                counts = {}
                complete = True
                for s in up:
                    values = s["concepts"] if k == "concept" else [s[f"sw_{k}"]]
                    values = [v for v in values if v and v != "未分类"]
                    complete = complete and bool(values)
                    for value in values:
                        counts[value] = counts.get(value, 0) + 1
                        totals_by_axis[k][value] = totals_by_axis[k].get(value, 0) + 1
                axis[d] = counts
                if complete:
                    complete_by_axis[k].add(d)
        else:
            missing.append(d)
        previous = current

    return {
        "dates": history_days,
        "dates_md": [_date_md(d) for d in history_days],
        "daily": records,
        "unavailable_dates": missing,
        **{f"sw_{key}_rotation": _rotation_rows(axes[key], complete_by_axis[key], totals_by_axis[key], n)
           for key, n in (("l1", 20), ("l2", 24), ("l3", 24))},
        "concept_rotation": _rotation_rows(axes["concept"], complete_by_axis["concept"], totals_by_axis["concept"], 24),
    }


def build_stock_history(store: LimitDataStore, ts_code: str, end_date: str | None = None, days: int = 30) -> dict:
    """查询单只股票近 N 个交易日的涨跌停/炸板轨迹与申万行业、概念归属。"""
    code = str(ts_code).strip().upper()
    avail_days = store.get_trade_days(end_date=end_date, count=max(1, min(60, int(days))))
    limits = store._load_range(avail_days, "limit_list_d")
    missing = [d for d, df in limits.items() if not _valid_limit_data(df)]
    matching = {d: df[df["ts_code"].map(_stock_code).eq(code)] if "ts_code" in df else df
                for d, df in limits.items()}
    sw_members = store.get_sw_members()
    snapshots = _snapshots(*store._enrichment_for_limits(matching), sw_members)

    sw_info = {"l1_name": "未分类", "l2_name": "未分类", "l3_name": "未分类"}
    if sw_members is not None and not sw_members.empty:
        m = sw_members[sw_members["ts_code"] == code]
        if not m.empty:
            r = m.iloc[-1]
            sw_info = {
                "l1_name": strip_roman(r.get("l1_name", "")) or "未分类",
                "l2_name": strip_roman(r.get("l2_name", "")) or "未分类",
                "l3_name": strip_roman(r.get("l3_name", "")) or "未分类",
            }

    events: list[dict] = []
    stock_name = code
    all_concepts: list[str] = []

    for d in avail_days:
        up_l, zha_l, down_l = snapshots.get(d) or ([], [], [])
        for item in up_l + zha_l + down_l:
            if item["ts_code"] == code:
                stock_name = item["name"] or stock_name
                for c in item.get("concepts", []):
                    if c and c not in all_concepts:
                        all_concepts.append(c)
                events.append({
                    "trade_date": d,
                    "date_fmt": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                    "limit": item["limit"],
                    "limit_times": item["limit_times"],
                    "stat_label": item["stat_label"],
                    "style": item["style"],
                    "pct_chg": item["pct_chg"],
                    "close": item["close"],
                    "amount_yi": item["amount_yi"],
                    "fd_yi": item["fd_yi"],
                    "turnover": item["turnover"],
                    "first_time": item["first_time"],
                    "last_time": item["last_time"],
                    "open_times": item["open_times"],
                    "lu_desc": item["lu_desc"],
                    "concepts": item["concepts"],
                })

    return {
        "ts_code": code,
        "name": stock_name,
        "board_tag": board_type_tag(code, stock_name),
        "sw_l1": sw_info["l1_name"],
        "sw_l2": sw_info["l2_name"],
        "sw_l3": sw_info["l3_name"],
        "concepts": all_concepts,
        "window_days": len(avail_days),
        "observed_days": len(avail_days) - len(missing),
        "unavailable_dates": missing,
        "source_scope": SOURCE_SCOPE,
        "up_days": sum(1 for e in events if e["limit"] == "U"),
        "zha_days": sum(1 for e in events if e["limit"] == "Z"),
        "down_days": sum(1 for e in events if e["limit"] == "D"),
        "max_board": max((e["limit_times"] for e in events if e["limit"] == "U" and e["limit_times"] is not None),
                         default=None if any(e["limit"] == "U" for e in events) or missing else 0),
        "events": list(reversed(events)),
    }


def build_dashboard_payload(
    store: LimitDataStore,
    target_date: str | None = None,
    lookback_days: int = 15,
    refresh: bool = False,
) -> dict:
    """构建指定交易日的完整涨停·连板·申万概念研究看板 Payload。"""
    t0 = time.monotonic()
    recent = store.get_trade_days(count=65)
    if not recent:
        raise RuntimeError("无法获取交易日历")
    target = _date_key(target_date) if target_date else None
    avail_days = [d for d in recent if not target or d <= target]
    if target and len(avail_days) < min(65, max(5, lookback_days) + 1):
        avail_days = store.get_trade_days(end_date=target, count=65)
    if not avail_days:
        raise RuntimeError("无法获取指定日期的交易日历")
    replay_date = avail_days[-1] if target else None

    # Probe one latest window for the date-picker, keeping frames (and misses)
    # for reuse. Older requested dates are resolved by the calendar, not rolled back.
    latest = None
    probe = {}
    probe_dates = recent if store.offline else recent[-4:]
    for d in reversed(probe_dates):
        update = refresh and (d == replay_date or (not target and d == recent[-1]))
        df = store.get_limit_list(d, refresh=update)
        probe[d] = df
        if _valid_limit_data(df):
            latest = d
            break

    resolved_date = replay_date or latest or recent[-1]
    avail_days = [d for d in avail_days if d <= resolved_date]
    window = max(5, min(40, int(lookback_days)))
    hist_days = avail_days[-window:]
    prior_to_window = avail_days[-window - 1] if len(avail_days) > window else None
    prev_date = avail_days[-2] if len(avail_days) >= 2 else None
    request_days = ([prior_to_window] if prior_to_window else []) + hist_days
    refreshed = [resolved_date] if refresh else []
    limits = {d: probe[d] for d in request_days if d in probe}
    limits.update(store._load_range([d for d in request_days if d not in limits], "limit_list_d", refresh_days=refreshed))
    sw_members = store.get_sw_members()
    limits, kpl_u, kpl_z = store._enrichment_for_limits(limits, refreshed)
    snapshots = _snapshots(limits, kpl_u, kpl_z, sw_members)
    today = snapshots.get(resolved_date)
    prev = snapshots.get(prev_date)
    up, zha, down = today or ([], [], [])
    available = today is not None
    sw_daily = store.get_sw_daily(resolved_date, refresh=refresh) if available else pd.DataFrame()
    stock_daily = store.get_stock_daily(resolved_date, refresh=refresh) if available else pd.DataFrame()
    promo = compute_promotion_and_yesterday_effect(
        up, zha, prev[0] if prev is not None else [], stock_daily,
        available=available and prev is not None, today_down=down,
    )
    ladder = build_ladder(up)
    sw_matrix = {l.upper(): build_sw_capital_matrix(up, zha, down, sw_daily, level=l) for l in ("l1", "l2", "l3")}
    concepts, cross = build_concept_ranking_and_cross(up, zha)
    history = build_multi_day_history(store, hist_days, sw_members, snapshots=snapshots, previous_day=prior_to_window)
    summary, leaders = _daily_board_summary(up, zha, down, available=available)
    for height in ("b1", "b2", "b3", "b4p"):
        summary[f"{height}_count"] = summary.pop(height)

    market_amount = None
    if not stock_daily.empty and "amount" in stock_daily.columns:
        market_amount = _scaled(pd.to_numeric(stock_daily["amount"], errors="coerce").sum(min_count=len(stock_daily)), 1e5, 1)
    up_amount = summary["up_amount_yi"]
    time_buckets = {"一字/T字 (09:25)": 0, "早盘 (09:30-10:00)": 0, "上午 (10:00-11:30)": 0,
                    "午后 (13:00-14:30)": 0, "尾盘 (14:30-15:00)": 0, "时间缺失": 0}
    for s in up:
        lt = s["last_time"]
        if s["style"] in ("一字板", "T字板") or (lt != "—" and lt <= "09:25:05"):
            time_buckets["一字/T字 (09:25)"] += 1
        elif lt == "—":
            time_buckets["时间缺失"] += 1
        elif lt <= "10:00:00":
            time_buckets["早盘 (09:30-10:00)"] += 1
        elif lt <= "11:30:00":
            time_buckets["上午 (10:00-11:30)"] += 1
        elif lt <= "14:30:00":
            time_buckets["午后 (13:00-14:30)"] += 1
        else:
            time_buckets["尾盘 (14:30-15:00)"] += 1
    overview = {
        **summary,
        "available": available,
        "trade_date": resolved_date, "trade_date_fmt": _format_date(resolved_date),
        "prev_date": prev_date, "prev_date_fmt": _format_date(prev_date),
        "top_leaders": [{**{k: s[k] for k in ("ts_code", "name", "stat_label", "sw_l2", "primary_concept", "fd_yi", "amount_yi")},
                         "board": s["limit_times"]} for s in leaders],
        "zha_amount_yi": _sum_field(zha, "amount_yi") if available else None,
        "market_amount_yi": market_amount,
        "up_amount_share_pct": _round(up_amount / market_amount * 100) if up_amount is not None and market_amount and market_amount > 0 else None,
        "time_distribution": [{"label": k, "count": v} for k, v in time_buckets.items()] if available else [],
    }
    diagnosis = (build_capital_position_diagnosis(overview, ladder, sw_matrix["L2"], concepts, promo) if available else {
        "cycle_phase": "数据缺失", "cycle_tone": "muted", "position_focus": "—", "position_desc": "",
        "top3_sw_share": None, "top3_sw_summary": "", "top3_concept_summary": "",
        "bullets": ["该交易日缺少 limit_list_d 数据，不推断市场情绪或晋级。"],
    })
    selectable = {d for d in recent[-60:] if d <= (latest or resolved_date)} | set(hist_days)
    return {
        "meta": {
            "trade_date": resolved_date, "trade_date_fmt": overview["trade_date_fmt"],
            "latest_available_date": latest, "generated_at": datetime.now(SH).strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": round(time.monotonic() - t0, 2), "lookback_days": len(hist_days),
            "available_dates": sorted(selectable, reverse=True),
            "unavailable_dates": history["unavailable_dates"],
            "source_scope": SOURCE_SCOPE,
            "coverage": {
                "limit_list_d": available,
                "kpl_up": resolved_date in kpl_u and not kpl_u[resolved_date].empty,
                "kpl_zha": resolved_date in kpl_z and not kpl_z[resolved_date].empty,
                "limit_up_with_concepts": sum(bool(s["concepts"]) for s in up) if available else None,
                "limit_up_with_sw": sum(bool(s["sw_l1_code"]) for s in up) if available else None,
                "sw_daily": not sw_daily.empty, "stock_daily": not stock_daily.empty,
            },
        },
        "overview": overview, "diagnosis": diagnosis, "promotion": promo, "ladder": ladder,
        "sw_matrix": sw_matrix, "concepts": concepts, "cross_matrix": cross,
        "stocks": {"up": up, "zha": zha, "down": down}, "history": history,
    }
