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
_NEWS_MIN_WINDOW = timedelta(minutes=10)
_NEWS_INIT_WINDOW = timedelta(hours=3)


class PermissionDenied(Exception):
    """账号没有该接口的访问权限。"""


class SourceError(Exception):
    """接口调用重试后仍失败，或配置缺失。"""


def _empty(cols):
    return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})


class TushareSource:
    def __init__(self, pro, sleep=time.sleep, clock=time.monotonic,
                 max_calls_per_min=config.MAX_CALLS_PER_MIN):
        self.pro = pro
        self._sleep = sleep
        self._clock = clock
        self._max_calls = max_calls_per_min
        self._times = deque()  # 最近 60 秒内的调用时刻

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

    def call(self, api, **kw):
        """节流 + 重试。权限错误立即抛 PermissionDenied；其余异常（含频率超限）
        等 2/4/8 秒重试，共 4 次，仍失败抛 SourceError。"""
        last = None
        for attempt in range(len(_RETRY_WAITS) + 1):
            self._throttle()
            try:
                df = getattr(self.pro, api)(**kw)
                return pd.DataFrame() if df is None else df
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                # 频率超限的提示里也带「权限」二字，必须先排除
                if "频率超限" not in msg and ("没有接口" in msg or "权限" in msg):
                    log.warning("接口无权限: %s", api)
                    raise PermissionDenied(f"{api}: 无访问权限") from e
                last = e
                if attempt < len(_RETRY_WAITS):
                    self._sleep(_RETRY_WAITS[attempt])
        raise SourceError(f"{api} 调用失败（已重试 {len(_RETRY_WAITS)} 次）: {type(last).__name__}") from last

    def _fetch(self, api, cols, rename=None, **kw):
        """调用并规整：选列、trade_date 转 str；空表返回带列的空 DataFrame。"""
        df = self.call(api, **kw)
        if rename:
            df = df.rename(columns=rename)
        if df.empty or not all(c in df.columns for c in cols):
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

    def etf_minutes(self, code, trade_date):
        """ETF 1 分钟线 09:30–15:00；amount 保持 tushare 原值。无权限返回 None。"""
        d = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        try:
            return self._fetch("stk_mins", ["trade_time", "amount"], ts_code=code, freq="1min",
                               start_date=f"{d} 09:30:00", end_date=f"{d} 15:00:00")
        except PermissionDenied:
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
        """两融余额。balance：亿元（rzrqye 原值元，三个交易所按日求和后 ÷1e8）。"""
        df = self._fetch("margin", ["trade_date", "rzrqye"], start_date=start, end_date=end)
        if df.empty:
            return _empty(["trade_date", "balance"])
        out = df.groupby("trade_date", as_index=False)["rzrqye"].sum()
        out["balance"] = out["rzrqye"] / 1e8
        return out[["trade_date", "balance"]].sort_values("trade_date").reset_index(drop=True)

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

    def fx(self, code, start, end):
        """外汇/商品（UKOIL、USDOLLAR、FED30D）。close = bid_close。"""
        return self._fetch("fx_daily", ["trade_date", "close"], rename={"bid_close": "close"},
                           ts_code=code, start_date=start, end_date=end)

    def global_index(self, start, end):
        return self._fetch("index_global", ["ts_code", "trade_date", "close", "pct_chg"],
                           start_date=start, end_date=end)

    def us_yields(self, start, end):
        return self._fetch("us_tycr", ["date", "y2", "y10"], start_date=start, end_date=end)

    # ------------------------------------------------------------ 快讯

    def major_news(self, start_dt, end_dt):
        """重大新闻。起始 3 小时窗口；某窗口返回 ≥800 行就二分重取，直到 <800 或窗口 ≤10 分钟；
        按 title 去重。"""
        cols = ["pub_time", "src", "title", "url"]
        s, e = datetime.strptime(start_dt, _NEWS_FMT), datetime.strptime(end_dt, _NEWS_FMT)
        frames = []

        def fetch(a, b):
            df = self.call("major_news", start_date=a.strftime(_NEWS_FMT), end_date=b.strftime(_NEWS_FMT),
                           fields="title,pub_time,src,url")
            if len(df) >= _NEWS_PAGE_LIMIT and (b - a) > _NEWS_MIN_WINDOW:
                mid = a + (b - a) / 2
                fetch(a, mid)
                fetch(mid, b)
            elif not df.empty and all(c in df.columns for c in cols):
                frames.append(df[cols])

        a = s
        while a < e:
            b = min(a + _NEWS_INIT_WINDOW, e)
            fetch(a, b)
            a = b
        if not frames:
            return _empty(cols)
        out = pd.concat(frames, ignore_index=True).drop_duplicates("title")
        return out.sort_values("pub_time").reset_index(drop=True)
