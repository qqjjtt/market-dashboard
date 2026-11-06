"""FredSource：FRED 公开 CSV（无需 API key），仅用标准库 urllib 取数。"""
import io
import logging
import urllib.request

import pandas as pd

log = logging.getLogger(__name__)

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}&cosd={start}"
TIMEOUT = 20  # 秒


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 market-dashboard"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8")


class FredSource:
    def __init__(self, fetch_text=None):
        self._fetch = fetch_text or _http_get

    def series(self, series_id, start):
        """某 FRED 序列自 start（YYYY-MM-DD）起的观测值。
        返回 pd.Series（索引 pd.Timestamp 升序，值 float，FRED 的 "." 缺失值 → NaN）；
        网络异常、非 CSV、无数据行等任何失败 → 记 WARNING 并返回 None。"""
        url = FRED_URL.format(id=series_id, start=start)
        try:
            text = self._fetch(url)
            df = pd.read_csv(io.StringIO(text), dtype=str)
            if df.shape[1] < 2 or df.empty:
                log.warning("FRED %s 返回内容不是有效 CSV 或无数据行", series_id)
                return None
            dates = pd.to_datetime(df.iloc[:, 0], errors="coerce")  # 首列叫 DATE 或 observation_date 均可
            values = pd.to_numeric(df.iloc[:, 1], errors="coerce")  # "." -> NaN
            out = pd.Series(values.to_numpy(dtype=float), index=pd.DatetimeIndex(dates).rename(None), name=series_id)
            out = out[out.index.notna()].sort_index()
            if out.empty:
                log.warning("FRED %s 没有可解析的日期行", series_id)
                return None
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("FRED %s 获取失败（%s）", series_id, type(e).__name__)
            return None
