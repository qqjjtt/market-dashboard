"""FredSource：FRED 取数，仅用标准库 urllib。
有 API key（环境变量 FRED_API_KEY）时走官方 API；没有则退回公开 CSV（无需 key，但可能被拒连）。
key 不打印、不记录：失败时日志只写异常类型。"""
import io
import json
import logging
import os
import urllib.request

import pandas as pd

log = logging.getLogger(__name__)

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}&cosd={start}"
FRED_API_URL = ("https://api.stlouisfed.org/fred/series/observations"
                "?series_id={id}&observation_start={start}&api_key={key}&file_type=json")
TIMEOUT = 20  # 秒


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 market-dashboard"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def _parse_csv(text):
    """CSV → (日期列, 值列)；首列叫 DATE 或 observation_date 均可。无效 → None。"""
    df = pd.read_csv(io.StringIO(text), dtype=str)
    if df.shape[1] < 2 or df.empty:
        return None
    return df.iloc[:, 0], df.iloc[:, 1]


def _parse_api_json(text):
    """官方 API JSON → (日期列, 值列)；错误响应或无 observations → None。"""
    obs = json.loads(text).get("observations")
    if not obs:
        return None
    return pd.Series([o.get("date") for o in obs]), pd.Series([o.get("value") for o in obs])


class FredSource:
    def __init__(self, fetch_text=None, api_key=None):
        self._fetch = fetch_text or _http_get
        self.api_key = api_key

    @classmethod
    def from_env(cls):
        return cls(api_key=os.environ.get("FRED_API_KEY") or None)

    def series(self, series_id, start):
        """某 FRED 序列自 start（YYYY-MM-DD）起的观测值。
        返回 pd.Series（索引 pd.Timestamp 升序，值 float，FRED 的 "." 缺失值 → NaN）；
        网络异常、内容无效、无数据行等任何失败 → 记 WARNING 并返回 None。"""
        if self.api_key:
            url, parse, via = FRED_API_URL.format(id=series_id, start=start, key=self.api_key), _parse_api_json, "官方 API"
        else:
            url, parse, via = FRED_URL.format(id=series_id, start=start), _parse_csv, "公开 CSV"
        try:
            cols = parse(self._fetch(url))
            if cols is None:
                log.warning("FRED %s 返回内容无效或无数据行（%s）", series_id, via)
                return None
            dates = pd.to_datetime(cols[0], errors="coerce")
            values = pd.to_numeric(cols[1], errors="coerce")  # "." -> NaN
            out = pd.Series(values.to_numpy(dtype=float), index=pd.DatetimeIndex(dates).rename(None), name=series_id)
            out = out[out.index.notna()].sort_index()
            if out.empty:
                log.warning("FRED %s 没有可解析的日期行（%s）", series_id, via)
                return None
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("FRED %s 获取失败（%s，%s）", series_id, via, type(e).__name__)
            return None
