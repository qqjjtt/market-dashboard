"""Demo 数据源：随机数据，接口（方法名、签名、返回列、单位、升序、None/空表约定）与
TushareSource / FredSource 完全一致，供 ``--demo`` 与端到端测试使用。
数据只由 seed 决定（同 seed 同结果），量级与真实数据相近。"""
import zlib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config

_BDAYS = [d.strftime("%Y%m%d") for d in pd.bdate_range("2024-01-01", "2030-12-31")]
_BDAY_SET = set(_BDAYS)
_ETF_NAMES = {
    "510300.SH": "沪深300ETF", "510310.SH": "沪深300ETF易方达", "510330.SH": "沪深300ETF华夏",
    "159919.SZ": "沪深300ETF嘉实", "510050.SH": "上证50ETF", "510500.SH": "中证500ETF",
    "512100.SH": "中证1000ETF", "588000.SH": "科创50ETF", "159915.SZ": "创业板ETF",
    "510180.SH": "上证180ETF", "512880.SH": "证券ETF", "512800.SH": "银行ETF",
    "512480.SH": "半导体ETF", "515790.SH": "光伏ETF", "159755.SZ": "电池ETF", "515070.SH": "人工智能ETF",
}
_INDEX_BASE = {"000001.SH": 3900, "399001.SZ": 13200, "399006.SZ": 3000, "000688.SH": 1050, "000300.SH": 4600}
_FLOW_INDUSTRIES = ["半导体", "银行", "证券", "软件开发", "白酒", "化学制药", "电池", "光伏设备", "工业金属",
                    "通信设备", "汽车零部件", "房地产开发", "煤炭开采", "电力", "消费电子"]
_FX_BASE = {"UKOIL": (80.0, 0.018), "USDOLLAR": (100.0, 0.004), "FED30D": (96.1, 0.0003),
            "USDCNH.FXCM": (6.9, 0.002)}
_REPO_BASE = {"DR001.IB": 1.30, "DR007.IB": 1.35, "R007.IB": 1.39}
_GLOBAL_BASE = {"SPX": 5800.0, "IXIC": 18500.0, "DJI": 42000.0, "HSI": 23000.0}
_NEWS_HITS = [  # (距 end_dt 的小时数, 来源, 标题)：命中 NEWS_KW_*
    (2, "演示快讯", "华福证券中标央行第三批互换便利操作12亿元"),
    (26, "演示快讯", "英国央行三年多来首度加息"),
    (50, "演示快讯", "美联储隔夜逆回购使用规模下降"),
]
_NEWS_FILLER = ["两市成交额小幅回升", "多家上市公司披露三季报", "新能源车10月销量数据公布",
                "多地发布文旅促销政策", "科创板新股申购开启", "机构调研热度上升"]
_FMT = "%Y-%m-%d %H:%M:%S"
# 不在固定板块里的申万行业（热门动态行的候选）：(名称, 级别, 父级名称)
_EXTRA_SW = [("工程机械", "L2", ""), ("医疗器械", "L2", ""), ("玻璃玻纤", "L2", ""), ("物流", "L2", ""),
             ("造纸", "L2", ""), ("白色家电", "L2", ""), ("医疗设备", "L3", "医疗器械"), ("快递", "L3", "物流")]


def _empty(cols):
    return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})


def _hash(*key):
    return zlib.crc32("|".join(str(k) for k in key).encode())


def _days_between(start, end):
    return [d for d in _BDAYS if start <= d <= end]


class DemoSource:
    def __init__(self, seed=7):
        self.seed = seed
        self._cache = {}
        self._build_universe()

    # ------------------------------------------------------------ 内部工具

    def _rng(self, *key):
        return np.random.default_rng([self.seed, _hash(*key)])

    def _walk(self, key, base, sd):
        """覆盖全部 _BDAYS 的乘性随机游走（按日期整体生成再切片，保证不同区间取数一致）。"""
        if key not in self._cache:
            steps = self._rng("walk", key).normal(0, sd, len(_BDAYS))
            self._cache[key] = pd.Series(base * np.exp(np.cumsum(steps)), index=_BDAYS)
        return self._cache[key]

    def _slice(self, s, start, end):
        return s[(s.index >= start) & (s.index <= end)]

    def _build_universe(self):
        rng = self._rng("universe")
        sectors = [s for s in config.SECTORS if not s["dynamic"]]
        boards = [("600", ".SH", 100), ("000", ".SZ", 80), ("300", ".SZ", 70), ("688", ".SH", 50)]
        rows, members = [], []
        n = 0
        for prefix, suffix, count in boards:
            for i in sorted(rng.choice(1000, size=count, replace=False)):
                n += 1
                code = f"{prefix}{i:03d}{suffix}"
                name = f"演示{n:03d}"
                if n % 50 == 7:
                    name = "ST" + name
                elif n % 83 == 5:
                    name = "*ST" + name
                sec = sectors[int(rng.integers(len(sectors)))]
                rows.append((code, name, str(rng.choice(sec["basic"]))))
                if rng.random() > 0.05:  # 少数股票没有申万成分记录
                    members.append((code, str(rng.choice(sec["sw"])),
                                    str(rng.choice(sec["sw"])) + "Ⅱ", str(rng.choice(sec["sw"])) + "Ⅲ"))
        # 一部分股票改属不在固定板块里的行业，保证热门行算得出浓度与获利盘
        extra_l2 = [n for n, lv, _ in _EXTRA_SW if lv == "L2"]
        l3_of = {p: n for n, lv, p in _EXTRA_SW if lv == "L3"}
        by_code = {m[0]: m for m in members}
        for k, (code, *_rest) in enumerate(rows):
            if k % 5 != 3:
                continue
            l2 = extra_l2[(k // 5) % len(extra_l2)]
            l3 = l3_of.get(l2) if (k // 5) % 2 == 0 and l2 in l3_of else l2
            by_code[code] = (code, "演示一级", l2 + "Ⅱ", l3 + "Ⅲ")
        members = [by_code[r[0]] for r in rows if r[0] in by_code]
        self._stocks = pd.DataFrame(rows, columns=["ts_code", "name", "industry"])
        self._members = pd.DataFrame(members, columns=["ts_code", "l1_name", "l2_name", "l3_name"])
        names = dict(zip(self._stocks["ts_code"], self._stocks["name"]))
        self._limit = {c: 5.0 if "ST" in names[c] else 20.0 if c[:3] in ("300", "688") else 10.0
                       for c in names}

    # ------------------------------------------------------------ 日历与股票

    def trade_days(self, end, n):
        days = [d for d in _BDAYS if d <= end]
        return days[-n:]

    def trade_days_between(self, start, end):
        return _days_between(start, end)

    def fut_mapping(self, code, start, end):
        """主力合约每季（3、6、9、12 月第二个周五）换一次，代码形如 T2612.CFX / SC2612.INE。"""
        cols = ["trade_date", "mapping_ts_code"]
        head, _, tail = code.partition(".")
        switches = [("20000101", "2312")]
        for y in range(2024, 2031):
            for m in (3, 6, 9, 12):
                d = pd.Timestamp(y, m, 1)
                d += pd.Timedelta(days=(4 - d.weekday()) % 7 + 7)
                switches.append((d.strftime("%Y%m%d"), f"{y % 100:02d}{m:02d}"))
        days = _days_between(start, end)
        if not days:
            return _empty(cols)
        codes = [f"{head}{[tag for eff, tag in switches if eff <= d][-1]}.{tail}" for d in days]
        return pd.DataFrame({"trade_date": days, "mapping_ts_code": codes})

    def stock_daily(self, trade_date):
        cols = ["ts_code", "pct_chg", "amount"]
        if trade_date not in _BDAY_SET:
            return _empty(cols)
        rng = self._rng("daily", trade_date)
        codes = self._stocks["ts_code"]
        lim = codes.map(self._limit).to_numpy()
        n = len(codes)
        pct = np.clip(rng.normal(0, 2.2, n), -lim, lim)
        r = rng.random(n)
        pct = np.where(r < 0.03, lim, np.where(r > 0.995, -lim, pct))
        amount = np.clip(np.exp(rng.normal(0.4, 1.0, n)), 0.01, 150)
        return pd.DataFrame({"ts_code": codes.to_numpy(), "pct_chg": pct.round(2), "amount": amount.round(4)})

    def stock_basic(self):
        return self._stocks.copy()

    def moneyflow(self, trade_date):
        cols = ["ts_code"] + [f"{d}_{lv}_amount" for lv in ("sm", "md", "lg", "elg") for d in ("buy", "sell")]
        if trade_date not in _BDAY_SET:
            return _empty(cols)
        rng = self._rng("moneyflow", trade_date)
        n = len(self._stocks)
        out = {"ts_code": self._stocks["ts_code"].to_numpy()}
        scale = {"sm": 600, "md": 900, "lg": 1100, "elg": 800}  # 万元
        for lv in ("sm", "md", "lg", "elg"):
            for d in ("buy", "sell"):
                out[f"{d}_{lv}_amount"] = np.exp(rng.normal(np.log(scale[lv]), 1.1, n)).round(2)
        return pd.DataFrame(out)[cols]

    def sw_members(self):
        return self._members.copy()

    def _sw_codes(self):
        """(code, 名称, 级别, 父级代码)：固定板块用到的申万名称按 L2、_EXTRA_SW 按其级别。"""
        fixed = list(dict.fromkeys(n for s in config.SECTORS if not s["dynamic"] for n in s["sw"]))
        out = [("801000.SI", "演示一级", "L1", "")]
        out += [(f"8010{i:02d}.SI", n + "Ⅱ", "L2", "801000.SI") for i, n in enumerate(fixed)]
        l2code = {}
        for i, (n, lv, parent) in enumerate(_EXTRA_SW):
            code = f"8020{i:02d}.SI"
            if lv == "L2":
                l2code[n] = code
        for i, (n, lv, parent) in enumerate(_EXTRA_SW):
            code = f"8020{i:02d}.SI"
            out.append((code, n + ("Ⅱ" if lv == "L2" else "Ⅲ"), lv, l2code[parent] if lv == "L3" else "801000.SI"))
        return out

    def sw_classify(self):
        return pd.DataFrame(self._sw_codes(), columns=["index_code", "industry_name", "level", "parent_code"])

    def sw_daily_range(self, start, end):
        cols = ["ts_code", "trade_date", "name", "pct_change", "amount", "float_mv"]
        days = _days_between(start, end)
        if not days:
            return _empty(cols)
        frames = []
        for code, name, _lv, _p in self._sw_codes():
            h = _hash("swbase", code)
            amt = self._slice(self._walk(("swamt", code), 1e5 * (1 + h % 40), 0.12), start, end)
            rng = self._rng("swpct", code)
            pct_all = pd.Series(rng.normal(0, 1.2, len(_BDAYS)), index=_BDAYS)
            frames.append(pd.DataFrame({
                "ts_code": code, "trade_date": list(amt.index), "name": name,
                "pct_change": pct_all.reindex(amt.index).to_numpy().round(4),
                "amount": amt.to_numpy().round(2),
                "float_mv": round(4e5 + (h % 50) * 4e5 + 1e5 * (h % 7), 2)}))
        return pd.concat(frames, ignore_index=True)[cols]

    def winner_rate(self, trade_date):
        if trade_date not in _BDAY_SET:
            return _empty(["ts_code", "winner_rate"])
        rng = self._rng("winner", trade_date)
        return pd.DataFrame({"ts_code": self._stocks["ts_code"].to_numpy(),
                             "winner_rate": rng.uniform(20, 90, len(self._stocks)).round(2)})

    def circ_mv(self, trade_date):
        if trade_date not in _BDAY_SET:
            return _empty(["ts_code", "circ_mv"])
        rng = self._rng("circmv", trade_date)
        return pd.DataFrame({"ts_code": self._stocks["ts_code"].to_numpy(),
                             "circ_mv": np.exp(rng.normal(np.log(3e5), 1.0, len(self._stocks))).round(2)})

    # ------------------------------------------------------------ ETF / 指数

    def etf_share(self, code, start, end):
        base = 30 + _hash("share", code) % 600  # 亿份
        s = self._slice(self._walk(("share", code), base, 0.004), start, end)
        if code.endswith(".SZ"):
            s = s[s.index != end]  # 深市最新一日尚未披露：该日无记录
        return pd.DataFrame({"trade_date": list(s.index), "share": s.to_numpy().round(4)})

    def etf_daily(self, code, start, end):
        px = self._slice(self._walk(("etfpx", code), 1.0 + (_hash("px", code) % 400) / 100, 0.009), start, end)
        amt_base = 1 + _hash("amt", code) % 30
        noise = np.exp(np.array([self._rng("etfamt", code, d).normal(0, 0.3) for d in px.index]))
        return pd.DataFrame({"trade_date": list(px.index), "close": px.to_numpy().round(4),
                             "amount": (amt_base * noise).round(4)})

    def etf_names(self, codes):
        return {c: _ETF_NAMES.get(c, f"演示ETF{c[:6]}") for c in codes}

    def index_daily(self, code, start, end):
        close = self._walk(("idx", code), _INDEX_BASE.get(code, 3000), 0.009)
        pct = close.pct_change().fillna(0) * 100
        close, pct = self._slice(close, start, end), self._slice(pct, start, end)
        return pd.DataFrame({"trade_date": list(close.index), "close": close.to_numpy().round(2),
                             "pct_chg": pct.to_numpy().round(4)})

    def etf_minutes(self, code, start, end):
        days = _days_between(start, end)
        if not days:
            return _empty(["trade_time", "amount"])
        mins = [f"{h:02d}:{m:02d}" for h in range(9, 12) for m in range(60) if "09:30" < f"{h:02d}:{m:02d}" <= "11:30"]
        mins += [f"{h:02d}:{m:02d}" for h in range(13, 16) for m in range(60) if "13:00" < f"{h:02d}:{m:02d}" <= "15:00"]
        k = np.arange(len(mins))
        shape = 1 + 2.0 * np.exp(-k / 15) + np.where(np.array(mins) >= "14:30", 0.8, 0.0)  # 开盘与尾盘放量
        base = 5e6 + (_hash("minamt", code) % 40) * 1e6  # 元/分钟
        times, amounts = [], []
        for d in days:
            noise = np.exp(self._rng("etfmin", code, d).normal(0, 0.4, len(mins)))
            ds = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            times += [f"{ds} {m}:00" for m in mins]
            amounts += list(base * shape * noise)
        return pd.DataFrame({"trade_time": times, "amount": np.round(amounts, 2)})

    # ------------------------------------------------------------ 资金与情绪

    def sector_flow(self, start, end):
        rows = []
        for d in _days_between(start, end):
            rng = self._rng("sectorflow", d)
            net = rng.normal(0, 15, len(_FLOW_INDUSTRIES)).round(2)
            pct = rng.normal(0, 1.5, len(_FLOW_INDUSTRIES)).round(2)
            rows += [(d, ind, a, p) for ind, a, p in zip(_FLOW_INDUSTRIES, net, pct)]
        return pd.DataFrame(rows, columns=["trade_date", "industry", "net_amount", "pct_change"])

    def market_flow(self, start, end):
        days = _days_between(start, end)
        vals = [round(float(self._rng("mktflow", d).normal(-50, 200)), 2) for d in days]
        return pd.DataFrame({"trade_date": days, "net_amount": vals})

    def margin(self, start, end):
        s = self._slice(self._walk("margin", 18500.0, 0.003), start, end)
        return pd.DataFrame({"trade_date": list(s.index), "balance": s.to_numpy().round(2)})

    def index_pb(self, code, start, end):
        base = 1.0 + _hash("pb", code) % 300 / 100  # 1.0–4.0
        s = self._slice(self._walk(("pb", code), base, 0.006), start, end)
        return pd.DataFrame({"trade_date": list(s.index), "pb": s.to_numpy().round(4)})

    def market_stats(self, start, end):
        mv = self._slice(self._walk("mkt_mv", 900000.0, 0.002), start, end)
        amt = self._slice(self._walk("mkt_amt", 16000.0, 0.03), start, end)
        return pd.DataFrame({"trade_date": list(mv.index), "float_mv": mv.to_numpy().round(2),
                             "amount": amt.to_numpy().round(2)})

    def margin_detail(self, start, end):
        ye = self._slice(self._walk("md_rzye", 25000.0, 0.003), start, end)
        mre = self._slice(self._walk("md_rzmre", 1500.0, 0.05), start, end)
        return pd.DataFrame({"trade_date": list(ye.index), "rzye": ye.to_numpy().round(2),
                             "rzmre": mre.to_numpy().round(2)})

    def limit_counts(self, trade_date):
        rng = self._rng("limits", trade_date)
        return {"up": int(rng.integers(40, 120)), "down": int(rng.integers(2, 30))}

    def inst_net_buy(self, trade_date):
        return float(round(self._rng("inst", trade_date).normal(0, 5), 4))

    # ------------------------------------------------------------ 宏观与跨市场

    def shibor(self, start, end):
        on = self._slice(self._walk("shibor_on", 1.3, 0.005), start, end)
        w1 = self._slice(self._walk("shibor_w1", 1.4, 0.005), start, end)
        y1 = self._slice(self._walk("shibor_y1", 1.6, 0.003), start, end)
        return pd.DataFrame({"date": list(on.index), "on": on.to_numpy().round(4), "w1": w1.to_numpy().round(4),
                             "y1": y1.to_numpy().round(4)})

    def repo(self, code, start, end):
        if code not in _REPO_BASE:
            return _empty(["trade_date", "rate"])
        s = self._slice(self._walk(("repo", code), _REPO_BASE[code], 0.005), start, end)
        return pd.DataFrame({"trade_date": list(s.index), "rate": s.to_numpy().round(4)})

    def social_financing(self, start_m, end_m):
        months, y, m = [], int(start_m[:4]), int(start_m[4:])
        while f"{y}{m:02d}" <= end_m:
            months.append(f"{y}{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        # 存量从 4.0e5 起每月约 +0.6%（带小噪声），与起始月无关地按「距 2024-01 的月数」生成，保证区间一致
        vals = []
        for mo in months:
            k = (int(mo[:4]) - 2024) * 12 + int(mo[4:]) - 1
            vals.append(round(float(4.0e5 * 1.006 ** k * (1 + self._rng("sf", mo).normal(0, 0.0005))), 1))
        return pd.DataFrame({"month": months, "stk_endval": vals})

    def m2(self, start_m, end_m):
        months, y, m = [], int(start_m[:4]), int(start_m[4:])
        while f"{y}{m:02d}" <= end_m:
            months.append(f"{y}{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        vals = [round(float(8.0 + self._rng("m2", mo).normal(0, 0.4)), 1) for mo in months]
        m1 = [round(float(5.0 + self._rng("m1", mo).normal(0, 0.4)), 1) for mo in months]
        return pd.DataFrame({"month": months, "m1_yoy": m1, "m2_yoy": vals})

    def bond_fut_10y(self, start, end):
        s = self._slice(self._walk("bondfut", 108.5, 0.0015), start, end)
        return pd.DataFrame({"trade_date": list(s.index), "close": s.to_numpy().round(3)})

    def gold_spot(self, start, end):
        s = self._slice(self._walk("gold", 620.0, 0.008), start, end)
        return pd.DataFrame({"trade_date": list(s.index), "close": s.to_numpy().round(2)})

    def crude_fut(self, start, end):
        s = self._slice(self._walk("crude", 560.0, 0.015), start, end)
        return pd.DataFrame({"trade_date": list(s.index), "close": s.to_numpy().round(1)})

    def fx(self, code, start, end):
        if code not in _FX_BASE:
            return _empty(["trade_date", "close"])
        base, sd = _FX_BASE[code]
        s = self._slice(self._walk(("fx", code), base, sd), start, end)
        return pd.DataFrame({"trade_date": list(s.index), "close": s.to_numpy().round(4)})

    def global_index(self, start, end):
        frames = []
        for code, base in _GLOBAL_BASE.items():
            close = self._walk(("gidx", code), base, 0.009)
            pct = close.pct_change().fillna(0) * 100
            close, pct = self._slice(close, start, end), self._slice(pct, start, end)
            frames.append(pd.DataFrame({"ts_code": code, "trade_date": list(close.index),
                                        "close": close.to_numpy().round(2), "pct_chg": pct.to_numpy().round(4)}))
        out = pd.concat(frames, ignore_index=True)
        return out.sort_values(["trade_date", "ts_code"], kind="stable").reset_index(drop=True)

    def us_yields(self, start, end):
        y2 = self._slice(self._walk("y2", 3.9, 0.004), start, end)
        y10 = self._slice(self._walk("y10", 4.3, 0.004), start, end)
        return pd.DataFrame({"date": list(y2.index), "y2": y2.to_numpy().round(3), "y10": y10.to_numpy().round(3)})

    # ------------------------------------------------------------ 快讯

    def major_news(self, start_dt, end_dt, max_calls=None):
        s, e = datetime.strptime(start_dt, _FMT), datetime.strptime(end_dt, _FMT)
        rows = []
        for hours, src, title in _NEWS_HITS:
            t = max(e - timedelta(hours=hours), s)
            rows.append((t.strftime(_FMT), src, title))
        total_h = max((e - s).total_seconds() / 3600, 1)
        n_fill = max(6, int(total_h / 12))
        rng = self._rng("news", start_dt, end_dt)
        for i in range(n_fill):
            t = s + timedelta(seconds=float(rng.random()) * (e - s).total_seconds())
            rows.append((t.strftime(_FMT), "演示快讯", f"{_NEWS_FILLER[i % len(_NEWS_FILLER)]}（{i + 1}）"))
        df = pd.DataFrame(rows, columns=["pub_time", "src", "title"])
        df["url"] = [f"https://example.com/demo-news/{i}" for i in range(len(df))]
        df = df.drop_duplicates("title").sort_values("pub_time", kind="stable").reset_index(drop=True)
        return df[["pub_time", "src", "title", "url"]]

    def last_news_coverage(self):
        return {"status": "ok", "since": None}


class DemoFred:
    """与 FredSource.series 同签名；日期从 2024-01-01 起生成，随机游走，量级同真实序列。"""

    # id -> (频率, 基准, 单步标准差, 下限, 上限)
    _SPEC = {
        "WALCL": ("W-WED", 6.7e6, 0.001, 5e6, 8e6),        # 百万美元
        "WTREGEN": ("W-WED", 9e5, 0.015, 4e5, 1.4e6),      # 百万美元
        "RRPONTSYD": ("B", 1.5, 0.15, 0.1, 5.0),           # 十亿美元
        "WRESBAL": ("W-WED", 2.9e6, 0.005, 2.2e6, 3.8e6),  # 百万美元
        "DFF": ("D", 3.88, 0.01, 3.0, 5.0),                # %
        "SOFR": ("B", 4.3, 0.002, 3.0, 5.5),               # %
        "IORB": ("B", 4.4, 0.002, 3.0, 5.5),               # %
        "BAMLH0A0HYM2": ("B", 3.0, 0.01, 1.5, 7.0),        # %
        "NFCI": ("W-FRI", -0.5, 0.02, -1.5, 1.0),          # 点（加性）
        "DFII10": ("B", 1.9, 0.03, 0.2, 3.8),              # %（加性）
    }
    # 加性游走序列 -> 步长倍率
    _ADDITIVE = {"RRPONTSYD": 1.0, "DFF": 0.3, "NFCI": 1.0, "DFII10": 0.3}

    def __init__(self, seed=7):
        self.seed = seed

    def series(self, series_id, start):
        if series_id not in self._SPEC:
            return None
        freq, base, sd, lo, hi = self._SPEC[series_id]
        idx = pd.date_range("2024-01-01", pd.Timestamp.today().normalize(), freq=freq)
        rng = np.random.default_rng([self.seed, _hash("fred", series_id)])
        steps = rng.normal(0, sd, len(idx))
        if series_id in self._ADDITIVE:  # 加性游走
            vals = base + np.cumsum(steps) * self._ADDITIVE[series_id]
        else:
            vals = base * np.exp(np.cumsum(steps))
        vals = np.clip(vals, lo, hi)
        vals = vals.round(0) if base > 1e4 else vals.round(2)
        s = pd.Series(vals, index=idx, name=series_id)
        s = s[s.index >= pd.Timestamp(start)]
        return None if s.empty else s
