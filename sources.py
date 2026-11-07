"""TushareSource：tushare 数据访问层（节流、重试、单位换算）。

约定：
- 每个方法返回 DataFrame，无数据返回带固定列的空 DataFrame（绝不 0 填充）；
  个别方法按约定返回 None / dict。
- ``trade_date`` 列一律为 str(YYYYMMDD)。
- token 只从环境变量 TUSHARE_TOKEN 读取，不打印、不记录、不落盘。
"""
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta

import pandas as pd

import config

log = logging.getLogger(__name__)

_RETRY_WAITS = (2, 4, 8)  # 共 4 次尝试，失败后依次等待
_NEWS_FMT = "%Y-%m-%d %H:%M:%S"
_NEWS_PAGE_LIMIT = 800  # major_news 单次最多 800 行


class PermissionDenied(Exception):
    """账号没有该接口的访问权限。"""


class SourceError(Exception):
    """接口调用重试后仍失败，或配置缺失。"""


class AuthError(SourceError):
    """token 无效 / 缺失（接口提示含 token）：重试没有意义，立即失败。"""


class RateLimited(SourceError):
    """频率超限且该接口不做重试（见 config.API_MIN_INTERVAL）。"""


def _empty(cols):
    return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})


class TushareSource:
    def __init__(self, pro, sleep=time.sleep, clock=time.monotonic,
                 max_calls_per_min=config.MAX_CALLS_PER_MIN, api_min_interval=None):
        self.pro = pro
        self._min_interval = config.API_MIN_INTERVAL if api_min_interval is None else api_min_interval
        self._last_call = {}  # api -> 上次调用时刻
        self._sleep = sleep
        self._clock = clock
        self._max_calls = max_calls_per_min
        self._times = deque()  # 最近 60 秒内的调用时刻
        self._news_cov = {"status": "failed", "since": None}  # 尚未调用过 major_news

    @classmethod
    def from_env(cls):
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise SourceError("环境变量 TUSHARE_TOKEN 未设置")
        import tushare as ts
        return cls(ts.pro_api(token))

    # ------------------------------------------------------------ 核心调用

    def _throttle(self):
        now = self._clock()
        while self._times and now - self._times[0] >= 60:
            self._times.popleft()
        if len(self._times) >= self._max_calls:
            wait = 60 - (now - self._times[0])
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
            self._times.popleft()
        self._times.append(now)

    def _api_gap(self, api):
        """同一接口两次调用的最小间隔（config.API_MIN_INTERVAL），叠加在全局节流之上。"""
        gap = self._min_interval.get(api)
        if gap and api in self._last_call:
            wait = gap - (self._clock() - self._last_call[api])
            if wait > 0:
                self._sleep(wait)
        self._last_call[api] = self._clock()

    def call(self, api, **kw):
        """节流 + 重试。权限错误立即抛 PermissionDenied；其余异常（含频率超限）
        等 2/4/8 秒重试，共 4 次，仍失败抛 SourceError。
        例外：消息含 "token" 的错误（token 不对）立即抛 AuthError（SourceError 子类），不重试；config.API_MIN_INTERVAL 中的接口（stk_mins），以及提示为「次/天」「次/小时」配额的频率超限，
        不重试，直接抛 RateLimited。"""
        last = None
        for attempt in range(len(_RETRY_WAITS) + 1):
            self._api_gap(api)
            self._throttle()
            try:
                df = getattr(self.pro, api)(**kw)
                return pd.DataFrame() if df is None else df
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "token" in msg.lower():
                    # 「token不对」等：重试无意义，立即失败（不记录消息，避免泄露 token）
                    log.error("%s 调用失败：token 无效或缺失（%s），不重试", api, type(e).__name__)
                    raise AuthError(f"{api}: token 无效或缺失") from e
                # 频率超限的提示里也带「权限」二字，必须先排除
                if "频率超限" not in msg and ("没有接口" in msg or "权限" in msg):
                    log.warning("接口无权限: %s", api)
                    raise PermissionDenied(f"{api}: 无访问权限") from e
                if "频率超限" in msg and (api in self._min_interval or "次/天" in msg or "次/小时" in msg):
                    # 按小时 / 按天的配额，几秒后重试没有意义（major_news 40 次/天、30 次/小时）
                    log.warning("接口频率超限，不重试: %s", api)
                    raise RateLimited(f"{api}: 频率超限") from e
                last = e
                if attempt < len(_RETRY_WAITS):
                    # 只记录异常类型，不记录消息（避免泄露 token 等信息）
                    log.warning("%s 调用失败（%s），第 %d 次重试", api, type(e).__name__, attempt + 1)
                    self._sleep(_RETRY_WAITS[attempt])
        raise SourceError(f"{api} 调用失败（已重试 {len(_RETRY_WAITS)} 次）: {type(last).__name__}") from last

    def _fetch(self, api, cols, rename=None, **kw):
        """调用并规整：选列、trade_date 转 str；空表返回带列的空 DataFrame。"""
        df = self.call(api, **kw)
        if rename:
            df = df.rename(columns=rename)
        if df.empty:
            return _empty(cols)
        if not all(c in df.columns for c in cols):
            log.warning("%s 返回缺少预期列 %s，按空数据处理", api, [c for c in cols if c not in df.columns])
            return _empty(cols)
        df = df[cols].copy()
        if "trade_date" in df.columns:
            df["trade_date"] = df["trade_date"].astype(str)
        for key in ("trade_date", "date", "month", "trade_time"):  # tushare 多为倒序，统一升序
            if key in df.columns:
                df = df.sort_values(key, kind="stable")
                break
        return df.reset_index(drop=True)

    # ------------------------------------------------------------ 日历与股票

    def trade_days(self, end, n):
        """≤ end 的最近 n 个开市日（SSE），升序，YYYYMMDD。"""
        end_dt = datetime.strptime(end, "%Y%m%d")
        start = (end_dt - timedelta(days=n * 2 + 20)).strftime("%Y%m%d")
        df = self.call("trade_cal", exchange="SSE", start_date=start, end_date=end, is_open=1)
        if df.empty:
            return []
        days = sorted(df["cal_date"].astype(str))
        return days[-n:]

    def stock_daily(self, trade_date):
        """全市场日线。amount：亿元（tushare 原值为千元，÷1e5）。"""
        df = self._fetch("daily", ["ts_code", "pct_chg", "amount"], trade_date=trade_date,
                         fields="ts_code,pct_chg,amount")
        if not df.empty:
            df["amount"] = df["amount"] / 1e5
        return df

    def stock_basic(self):
        return self._fetch("stock_basic", ["ts_code", "name", "industry"], list_status="L",
                           fields="ts_code,name,industry")

    def moneyflow(self, trade_date):
        """个股资金流向，保持万元（tushare 原值）。"""
        cols = ["ts_code"] + [f"{d}_{lv}_amount" for lv in ("sm", "md", "lg", "elg") for d in ("buy", "sell")]
        return self._fetch("moneyflow", cols, trade_date=trade_date, fields=",".join(cols))

    def sw_members(self):
        """申万行业成分（最新口径）。无权限返回 None。"""
        cols = ["ts_code", "l1_name", "l2_name", "l3_name"]
        frames, offset, limit = [], 0, 3000
        try:
            while True:
                df = self.call("index_member_all", is_new="Y", limit=limit, offset=offset)
                if df.empty:
                    break
                frames.append(df)
                if len(df) < limit:
                    break
                offset += limit
        except PermissionDenied:
            return None
        if not frames:
            return _empty(cols)
        out = pd.concat(frames, ignore_index=True)
        if not all(c in out.columns for c in cols):
            log.warning("index_member_all 返回缺少预期列，按空数据处理")
            return _empty(cols)
        return out[cols].reset_index(drop=True)

    # ------------------------------------------------------------ ETF / 指数

    def etf_share(self, code, start, end):
        """ETF 份额。share：亿份（fd_share 为万份，÷1e4）。"""
        df = self._fetch("fund_share", ["trade_date", "fd_share"], ts_code=code, start_date=start, end_date=end)
        df = df.rename(columns={"fd_share": "share"})
        if not df.empty:
            df["share"] = df["share"] / 1e4
        return df

    def etf_daily(self, code, start, end):
        """ETF 日线。amount：亿元（原值千元，÷1e5）。"""
        df = self._fetch("fund_daily", ["trade_date", "close", "amount"], ts_code=code,
                         start_date=start, end_date=end)
        if not df.empty:
            df["amount"] = df["amount"] / 1e5
        return df

    def etf_names(self, codes):
        df = self.call("fund_basic", market="E")
        if df.empty or "ts_code" not in df.columns or "name" not in df.columns:
            return {}
        want = set(codes)
        return {r.ts_code: r.name for r in df[["ts_code", "name"]].itertuples() if r.ts_code in want}

    def index_daily(self, code, start, end):
        return self._fetch("index_daily", ["trade_date", "close", "pct_chg"], ts_code=code,
                           start_date=start, end_date=end)

    def etf_minutes(self, code, start, end):
        """ETF 1 分钟线，一次 stk_mins 调用覆盖 start 09:30:00 至 end 15:00:00（start/end 为 YYYYMMDD，含两端）。
        列 [trade_time, amount]，升序；amount 保持 tushare 原值。
        无权限或频率超限（stk_mins 账号限额 1 次/分钟，且不重试）→ None（频率超限记 WARNING）。"""
        fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"  # noqa: E731
        try:
            return self._fetch("stk_mins", ["trade_time", "amount"], ts_code=code, freq="1min",
                               start_date=f"{fmt(start)} 09:30:00", end_date=f"{fmt(end)} 15:00:00")
        except PermissionDenied:
            return None
        except RateLimited:
            log.warning("etf_minutes(%s) 频率超限，返回 None", code)
            return None

    # ------------------------------------------------------------ 资金与情绪

    def sector_flow(self, start, end):
        """同花顺行业资金流（moneyflow_ind_ths）。net_amount：亿元（tushare 文档原单位即亿元，
        实测 半导体 -35.26 量级吻合，不换算）；pct_change：%。"""
        return self._fetch("moneyflow_ind_ths", ["trade_date", "industry", "net_amount", "pct_change"],
                           start_date=start, end_date=end)

    def market_flow(self, start, end):
        """大盘资金流（moneyflow_mkt_dc）。net_amount：亿元（原值元，÷1e8）。"""
        df = self._fetch("moneyflow_mkt_dc", ["trade_date", "net_amount"], start_date=start, end_date=end)
        if not df.empty:
            df["net_amount"] = df["net_amount"] / 1e8
        return df

    def margin(self, start, end):
        """两融余额。balance：亿元（rzrqye 原值元，三个交易所按日求和后 ÷1e8）。
        某日交易所数少于结果中出现过的最大数（如最新一天尚未发布完）→ 丢弃该日并记 WARNING。"""
        df = self._fetch("margin", ["trade_date", "rzrqye"], start_date=start, end_date=end)
        if df.empty:
            return _empty(["trade_date", "balance"])
        g = df.groupby("trade_date")["rzrqye"]
        agg = pd.DataFrame({"total": g.sum(min_count=1), "n": g.count()})
        full = agg["n"].max()
        bad = agg[(agg["n"] < full) | agg["total"].isna()]
        if len(bad):
            log.warning("margin 丢弃交易所数不足 %d 的日期: %s", full, list(bad.index))
        agg = agg.drop(bad.index)
        out = pd.DataFrame({"trade_date": agg.index, "balance": agg["total"].values / 1e8})
        return out.sort_values("trade_date").reset_index(drop=True)

    def limit_counts(self, trade_date):
        """涨跌停家数 {up, down}；空表 → 均为 None。"""
        df = self.call("limit_list_d", trade_date=trade_date)
        if df.empty or "limit" not in df.columns:
            return {"up": None, "down": None}
        return {"up": int((df["limit"] == "U").sum()), "down": int((df["limit"] == "D").sum())}

    def inst_net_buy(self, trade_date):
        """龙虎榜机构专用席位净买入合计，亿元。top_inst 的 buy/sell/net_buy 原值单位为元
        （已与 top_list 同股 l_buy/l_sell 对照核实），÷1e8。同一席位在买入榜/卖出榜
        （side 0/1）或多个上榜原因下会重复出现，按 (ts_code, exalter, buy, sell) 去重。
        top_inst 无数据 → None；有数据但无机构席位 → 0.0（确无机构成交）。"""
        df = self.call("top_inst", trade_date=trade_date)
        if df.empty or not {"ts_code", "exalter", "buy", "sell", "net_buy"} <= set(df.columns):
            return None
        df = df.drop_duplicates(["ts_code", "exalter", "buy", "sell"])
        inst = df[df["exalter"] == "机构专用"]
        return float(inst["net_buy"].sum()) / 1e8

    # ------------------------------------------------------------ 宏观与跨市场

    def shibor(self, start, end):
        return self._fetch("shibor", ["date", "on", "w1"], rename={"1w": "w1"}, start_date=start, end_date=end)

    def m2(self, start_m, end_m):
        return self._fetch("cn_m", ["month", "m2_yoy"], start_m=start_m, end_m=end_m, fields="month,m2_yoy")

    def bond_fut_10y(self, start, end):
        return self._fetch("fut_daily", ["trade_date", "close"], ts_code="T.CFX",
                           start_date=start, end_date=end)

    def gold_spot(self, start, end):
        """上海黄金交易所 Au99.99 现货日线（sge_daily），close：元/克。每日 18–19 点更新。"""
        return self._fetch("sge_daily", ["trade_date", "close"], ts_code=config.GOLD_CODE,
                           start_date=start, end_date=end)

    def crude_fut(self, start, end):
        """上海国际能源交易中心原油主力（fut_daily），close：元/桶。每个交易日 17–18 点更新。"""
        return self._fetch("fut_daily", ["trade_date", "close"], ts_code=config.OIL_CODE,
                           start_date=start, end_date=end)

    def fx(self, code, start, end):
        """外汇/商品（UKOIL、USDOLLAR、FED30D）。close = bid_close。
        tushare 的最新一根常是刚开盘、尚未收完的残缺日线（实测 UKOIL 20261105 仅 3 笔 tick，
        开高低收几乎相同）。最新一根的 tick_qty 低于其前 3 根中位数的 config.FX_MIN_TICK_RATIO 倍时丢弃并记
        WARNING，取前一根。只看最新一根、只和紧邻的前几根比：USDOLLAR 的 tick_qty 在 10/30 前后整体
        降了一个数量级，拿整个窗口的中位数比会把正常日线误判成残缺。"""
        cols = ["trade_date", "close"]
        df = self.call("fx_daily", ts_code=code, start_date=start, end_date=end)
        if df.empty:
            return _empty(cols)
        df = df.rename(columns={"bid_close": "close"})
        if not all(c in df.columns for c in cols):
            log.warning("fx_daily 返回缺少预期列 %s，按空数据处理", [c for c in cols if c not in df.columns])
            return _empty(cols)
        df = df.copy()
        df["trade_date"] = df["trade_date"].astype(str)
        df = df.sort_values("trade_date", kind="stable")
        if "tick_qty" in df.columns and len(df) >= 2:
            tq = pd.to_numeric(df["tick_qty"], errors="coerce")
            ref = tq.iloc[-4:-1].median()
            if pd.notna(tq.iloc[-1]) and pd.notna(ref) and tq.iloc[-1] < ref * config.FX_MIN_TICK_RATIO:
                log.warning("fx_daily %s 最新日线 %s 不完整（尚未收完，tick_qty=%s，前几根中位数 %.0f），已丢弃，取前一根",
                            code, df["trade_date"].iloc[-1], int(tq.iloc[-1]), ref)
                df = df.iloc[:-1]
        return df[cols].reset_index(drop=True)

    def global_index(self, start, end):
        return self._fetch("index_global", ["ts_code", "trade_date", "close", "pct_chg"],
                           start_date=start, end_date=end)

    def us_yields(self, start, end):
        return self._fetch("us_tycr", ["date", "y2", "y10"], start_date=start, end_date=end)

    # ------------------------------------------------------------ 快讯

    def major_news(self, start_dt, end_dt, max_calls=None):
        """重大新闻（pub_time 倒序、单次最多 800 行，保留的是窗口内最新的 800 行）。
        从 end_dt 起向前翻页：每页取 [start_dt, 当前终点]，满 800 行就把终点挪到该页最早一条的时间继续，
        不足 800 行即窗口取完；按 title 去重、pub_time 升序返回。
        账号限额 major_news 40 次/天、30 次/小时，所以调用数以 max_calls（缺省 config.NEWS_MAX_CALLS）封顶：
        用完预算、撞到配额或某页重试后仍失败（SourceError），就返回已取到的（最新的）部分并记 WARNING，不抛异常。"""
        cols = ["pub_time", "src", "title", "url"]
        max_calls = config.NEWS_MAX_CALLS if max_calls is None else max_calls
        s, e = datetime.strptime(start_dt, _NEWS_FMT), datetime.strptime(end_dt, _NEWS_FMT)
        self._news_cov = {"status": "failed", "since": None}
        frames, calls, cur, complete = [], 0, e, False
        while calls < max_calls:
            try:
                df = self.call("major_news", start_date=s.strftime(_NEWS_FMT), end_date=cur.strftime(_NEWS_FMT),
                               fields="title,pub_time,src,url")
            except RateLimited:
                log.warning("major_news 接口配额用尽（账号 40 次/天、30 次/小时），新闻%s",
                            f"仅覆盖 {cur.strftime(_NEWS_FMT)} 之后" if frames else "留空")
                return self._news_result(frames, cols, cur)
            except SourceError as err:  # 重试后仍失败：保留已取到的页，不让后面的页拖垮前面的
                log.warning("major_news 取数失败（%s），新闻%s", type(err.__cause__ or err).__name__,
                            f"仅覆盖 {cur.strftime(_NEWS_FMT)} 之后" if frames else "留空")
                return self._news_result(frames, cols, cur)
            calls += 1
            if df.empty:
                complete = True
                break
            if not all(c in df.columns for c in cols):
                log.warning("major_news 返回缺少预期列，按空数据处理")
                complete = True
                break
            frames.append(df[cols])
            if len(df) < _NEWS_PAGE_LIMIT:
                complete = True
                break
            oldest = datetime.strptime(str(df["pub_time"].min()), _NEWS_FMT)
            cur = oldest if oldest < cur else cur - timedelta(seconds=1)
            if cur <= s:
                complete = True
                break
        if not complete:
            log.warning("major_news 已用满本次 %d 次调用预算，新闻仅覆盖 %s 之后", max_calls, cur.strftime(_NEWS_FMT))
            return self._news_result(frames, cols, cur)
        self._news_cov = {"status": "ok", "since": None}
        return self._news_result(frames, cols)

    def last_news_coverage(self):
        """最近一次 major_news 的覆盖情况：{status: "ok"|"partial"|"failed", since: "YYYY-MM-DD HH:MM:SS"|None}。
        ok = 窗口取完（含确无新闻）；partial = 只取到 since 之后的部分（配额 / 预算 / 某页失败）；
        failed = 一条都没取到（配额或错误）。"""
        return dict(self._news_cov)

    def _news_result(self, frames, cols, since=None):
        """since 非 None 表示窗口没取完：有数据 → partial，无数据 → failed。"""
        if since is not None:
            self._news_cov = ({"status": "partial", "since": since.strftime(_NEWS_FMT)} if frames
                              else {"status": "failed", "since": None})
        if not frames:
            return _empty(cols)
        out = pd.concat(frames, ignore_index=True).drop_duplicates("title")
        return out.sort_values("pub_time").reset_index(drop=True)
