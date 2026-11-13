"""备用数据源探测：逐个调用腾讯、新浪、东方财富、同花顺的接口，记录能否拿到数据、耗时多久，
并与 tushare 同一天的数据比对，判断能否作为替代或相互印证。

只读、不写任何数据文件；结果打印成表格，在 GitHub Actions 上同时写进运行摘要。
用法：python probe_sources.py [--date YYYYMMDD]（默认取 tushare 最近一个已收盘交易日）
"""
import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
TIMEOUT = 15


def http_get(url, referer=None, encoding="utf-8"):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode(encoding, errors="replace")


def timed(fn):
    """运行 fn，返回 (结果, 耗时秒, 错误文字)。"""
    t = time.monotonic()
    try:
        return fn(), round(time.monotonic() - t, 2), None
    except Exception as e:  # 探测脚本：任何失败都记下来继续
        return None, round(time.monotonic() - t, 2), f"{type(e).__name__}: {str(e)[:120]}"


def dash(d):  # 20261113 -> 2026-11-13
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


# ------------------------------------------------------------------ 各来源
def tx_kline(sym, n=5):
    """腾讯日 K：{日期: 收盘}。"""
    txt = http_get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},")
    data = json.loads(txt)["data"][sym]
    rows = data.get("day") or data.get("qfqday") or []
    return {r[0]: float(r[2]) for r in rows}


def tx_quote(syms):
    """腾讯实时：{代码: (名称, 现价, 涨跌幅%, 时间)}。"""
    txt = http_get("https://qt.gtimg.cn/q=" + ",".join(syms), encoding="gbk")
    out = {}
    for line in txt.strip().split(";"):
        if "=" not in line:
            continue
        key, val = line.strip().split("=", 1)
        f = val.strip('"').split("~")
        if len(f) > 32:
            out[key.replace("v_", "")] = (f[1], float(f[3]), float(f[32]), f[30])
    if not out:
        raise ValueError("无数据")
    return out


def sina_kline(sym, n=5):
    txt = http_get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
    return {r["day"]: float(r["close"]) for r in json.loads(txt)}


def sina_quote(syms):
    txt = http_get("https://hq.sinajs.cn/list=" + ",".join(syms),
                   referer="https://finance.sina.com.cn", encoding="gbk")
    out = {}
    for line in txt.strip().split(";"):
        if "=" not in line:
            continue
        key, val = line.strip().split("=", 1)
        f = val.strip('"').split(",")
        if len(f) > 31 and f[3]:
            out[key.replace("var hq_str_", "")] = (f[0], float(f[3]), f[30] + " " + f[31])
    if not out:
        raise ValueError("无数据（可能被拦截或缺少 Referer）")
    return out


def em_kline(secid, n=5):
    txt = http_get("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
                   f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53&klt=101&fqt=0&end=20500101&lmt={n}")
    rows = json.loads(txt)["data"]["klines"]
    return {r.split(",")[0]: float(r.split(",")[2]) for r in rows}


def em_margin(n=5):
    """东财两融历史：{日期: 融资余额（亿）}。"""
    txt = http_get("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ"
                   f"&columns=DIM_DATE,RZYE&pageSize={n}&pageNumber=1&sortColumns=DIM_DATE&sortTypes=-1")
    rows = json.loads(txt)["result"]["data"]
    return {r["DIM_DATE"][:10]: r["RZYE"] / 1e8 for r in rows}


def em_industry_flow():
    """东财行业板块主力净流入（盘中实时，口径与同花顺不同）：{行业: 净额（亿）}。"""
    txt = http_get("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=500&po=1&np=1&fltt=2&invt=2"
                   "&fid=f62&fs=m:90+t:2&fields=f12,f14,f62")
    rows = json.loads(txt)["data"]["diff"]
    return {r["f14"]: r["f62"] / 1e8 for r in rows if isinstance(r.get("f62"), (int, float))}


def ths_industry_flow():
    """同花顺行业资金流（经 AKShare，需执行 JS 生成 hexin-v）：{行业: 净额（亿）}。"""
    import akshare as ak
    df = ak.stock_fund_flow_industry(symbol="即时")
    return {r["行业"]: float(r["净额"]) for _, r in df.iterrows()}


# ------------------------------------------------------------------ tushare 基准
def tushare_baseline(date):
    import tushare as ts
    pro = ts.pro_api(os.environ["TUSHARE_TOKEN"])
    if date is None:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
        idx = pro.index_daily(ts_code="000001.SH", start_date=start, end_date=end)
        date = str(idx["trade_date"].max())
    base = {"date": date}
    idx = pro.index_daily(ts_code="000001.SH", trade_date=date)
    base["sh_close"] = float(idx["close"].iloc[0]) if len(idx) else None
    etf = pro.fund_daily(ts_code="510300.SH", trade_date=date)
    base["etf_close"] = float(etf["close"].iloc[0]) if len(etf) else None
    # 两融数据次日才公布，取基准日及之前最近一天
    start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
    m = pro.margin(start_date=start, end_date=date)
    if len(m):
        md = str(m["trade_date"].max())
        m = m[m["trade_date"] == md]
        base.update(margin_date=md, rzye=float(m["rzye"].sum()) / 1e8,
                    margin_ex=sorted(m["exchange_id"].tolist()))
    else:
        base.update(margin_date=None, rzye=None, margin_ex=[])
    f = pro.moneyflow_ind_ths(trade_date=date)
    base["ind_flow"] = {r.industry: float(r.net_amount) for r in f.itertuples()} if len(f) else {}
    return base


def compare_close(name, got, date, ref, tol=0.011):
    d = dash(date)
    if got is None:
        return None
    if d not in got:
        return f"无 {d} 数据（最新 {max(got)}）"
    if ref is None:
        return f"{got[d]:.3f}（tushare 无数）"
    diff = got[d] - ref
    return f"{'✅ 一致' if abs(diff) <= tol else '⚠️ 不一致'} {got[d]:.3f} vs {ref:.3f}"


def compare_flow(got, ref):
    if got is None:
        return None
    if not ref:
        return f"{len(got)} 个行业（tushare 当天无数）"
    common = [k for k in got if k in ref]
    if not common:
        return f"{len(got)} 个行业，与 tushare 行业名无交集"
    diffs = [abs(got[k] - ref[k]) for k in common]
    close = sum(1 for x in diffs if x <= 0.05)
    same_sign = sum(1 for k in common if (got[k] > 0) == (ref[k] > 0))
    return (f"同名 {len(common)}/{len(ref)}，差 ≤0.05 亿的 {close} 个，方向一致 {same_sign} 个，"
            f"最大差 {max(diffs):.2f} 亿")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    args = ap.parse_args()

    base, t_base, err = timed(lambda: tushare_baseline(args.date))
    rows = [("tushare（基准）", "指数/ETF/两融/行业流向", t_base,
             err or f"{base['date']}：上证 {base['sh_close']}，510300 {base['etf_close']}，"
                     f"融资余额 {base['rzye'] or 0:.1f} 亿（{base['margin_date']}，{'/'.join(base['margin_ex'])}），"
                     f"行业流向 {len(base['ind_flow'])} 个")]
    if base is None:
        base = {"date": args.date or datetime.now().strftime("%Y%m%d"), "sh_close": None,
                "etf_close": None, "rzye": None, "margin_date": None, "ind_flow": {}}
    date = base["date"]

    def add(src, item, fn, judge):
        res, t, e = timed(fn)
        rows.append((src, item, t, e or judge(res)))

    add("腾讯", "上证日 K", lambda: tx_kline("sh000001"),
        lambda r: compare_close("腾讯", r, date, base["sh_close"]))
    add("腾讯", "510300 日 K", lambda: tx_kline("sh510300"),
        lambda r: compare_close("腾讯", r, date, base["etf_close"]))
    add("腾讯", "实时行情", lambda: tx_quote(["sh000001", "sz399001", "sh510300"]),
        lambda r: "；".join(f"{v[0]} {v[1]} ({v[2]:+.2f}%) @{v[3]}" for v in r.values()))
    add("新浪", "上证日 K", lambda: sina_kline("sh000001"),
        lambda r: compare_close("新浪", r, date, base["sh_close"]))
    add("新浪", "实时行情", lambda: sina_quote(["sh000001", "sh510300"]),
        lambda r: "；".join(f"{v[0]} {v[1]} @{v[2]}" for v in r.values()))
    add("东方财富", "上证日 K", lambda: em_kline("1.000001"),
        lambda r: compare_close("东财", r, date, base["sh_close"]))
    add("东方财富", "融资余额", lambda: em_margin(),
        lambda r: (lambda d: f"{r[d]:.1f} 亿 vs tushare {base['rzye']:.1f} 亿，差 "
                             f"{(r[d] / base['rzye'] - 1) * 100:+.2f}%"
                   if d in r and base["rzye"] else f"无 {d}（最新 {max(r)}）")(dash(base["margin_date"] or date)))
    add("东方财富", "行业资金流（实时）", em_industry_flow,
        lambda r: f"{len(r)} 个板块（东财口径，与同花顺不可直接比）")
    add("同花顺", "行业资金流", ths_industry_flow, lambda r: compare_flow(r, base["ind_flow"]))

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    where = "GitHub Actions（" + os.environ.get("RUNNER_NAME", "runner") + "）" if os.environ.get("GITHUB_ACTIONS") else "本机"
    lines = [f"## 备用数据源探测 · {where} · {now}", "",
             f"比对基准日：{dash(date)}（tushare）", "",
             "| 来源 | 项目 | 耗时 s | 结果 |", "|---|---|---|---|"]
    for src, item, t, res in rows:
        lines.append(f"| {src} | {item} | {t} | {str(res).replace('|', '/')} |")
    lines += ["", "说明：同花顺「即时」行业资金流在收盘后即当天全天数，可与 tushare 同日数据比对；"
                  "盘中运行时两者日期不同，只看能否取到。"]
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
