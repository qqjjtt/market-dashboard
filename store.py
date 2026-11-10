"""历史数据库（第二数据库）：行业资金流全量表 + 每日指标长表，存成仓库里的 CSV。

纯函数 + 少量文件读写，不依赖 build。写文件一律先写临时文件再替换；读写失败只记 WARNING，不抛异常给调用方
（调用方 build.py 再包一层 try/except 兜底）。
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("store")

SECTOR_COLS = ["trade_date", "industry", "net_amount", "pct_change"]
DAILY_COLS = ["trade_date", "key", "value"]
SECTOR_FILE = "sector_flow.csv"
DAILY_FILE = "daily.csv"
MATRIX_FILE = "sector_flow.json"

_STR_COLS = {"trade_date", "industry", "key"}
_SKIP_TOP = {"meta", "conclusions", "calendar", "eco_calendar", "news", "policy_ops", "sector_flow"}
_ID_FIELDS = ("key", "code", "name", "label")


def _empty(cols) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=object if c in _STR_COLS else float) for c in cols})


def _typed(df: pd.DataFrame, cols) -> pd.DataFrame:
    out = df[list(cols)].copy()
    for c in cols:
        if c in _STR_COLS:
            out[c] = out[c].astype(str)
        else:
            out[c] = pd.to_numeric(out[c], errors="coerce").astype(float)
    return out.reset_index(drop=True)


def load_csv(path, cols) -> pd.DataFrame:
    """读 CSV；文件不存在 → 带列的空表；读失败或缺列 → WARNING 后返回带列的空表。trade_date 一律转 str。"""
    if path is None or not Path(path).exists():
        return _empty(cols)
    p = Path(path)
    try:
        df = pd.read_csv(p, dtype={c: str for c in cols if c in _STR_COLS}, encoding="utf-8")
        if not set(cols) <= set(df.columns):
            raise ValueError(f"缺列 {sorted(set(cols) - set(df.columns))}")
        return _typed(df, cols)
    except Exception as e:  # noqa: BLE001
        log.warning("历史库文件 %s 读取失败（%s: %s），按空表处理", p.name, type(e).__name__, str(e)[:80])
        return _empty(cols)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def save_csv(df: pd.DataFrame, path, keys) -> None:
    """按 keys 升序、去掉索引写出；先写 path.tmp 再 os.replace；自动建目录；utf-8，换行 \\n。"""
    out = df.sort_values(list(keys), kind="stable").reset_index(drop=True)
    _atomic_write(Path(path), out.to_csv(index=False, lineterminator="\n"))


def merge_sector(old: pd.DataFrame, new: pd.DataFrame | None) -> pd.DataFrame:
    """按 (trade_date, industry) 合并，new 覆盖 old；new 中净额为空的行丢掉（不覆盖旧值）；new 为空 → 原样返回 old。

    精度保护：数据源只对最近约两周给两位小数，更早的日期是四舍五入到整数亿元的值。
    对两边都有的行，若新净额是整数、旧净额不是整数、且两者相差不超过 0.5，则净额保留旧值
    （pct_change 仍取新值），避免回补时把 12.34 覆盖成 12.0。其余情况新覆盖旧。"""
    if new is None or len(new) == 0:
        return old
    n = _typed(new, SECTOR_COLS)
    n = n[n["net_amount"].notna()]
    if n.empty:
        return old
    o = _typed(old, SECTOR_COLS) if len(old) else _empty(SECTOR_COLS)
    n = n.drop_duplicates(["trade_date", "industry"], keep="last")
    if len(o):
        prev = o.drop_duplicates(["trade_date", "industry"], keep="last").set_index(["trade_date", "industry"])["net_amount"].astype(float)
        old_net = pd.Series(list(zip(n["trade_date"], n["industry"])), index=n.index).map(prev)
        nn = n["net_amount"].astype(float)
        keep = (
            old_net.notna()
            & ((nn - nn.round()).abs() < 1e-9)
            & ((old_net - old_net.round()).abs() >= 1e-9)
            & ((nn - old_net).abs() <= 0.5 + 1e-9)
        )
        n = n.copy()
        n.loc[keep, "net_amount"] = old_net[keep]
    both = pd.concat([o, n], ignore_index=True)
    both = both.drop_duplicates(["trade_date", "industry"], keep="last")
    both = both.sort_values(["trade_date", "industry"], kind="stable").reset_index(drop=True)
    for c in ("net_amount", "pct_change"):
        both[c] = both[c].astype(float).round(2)
    return both


def missing_days(have: set, want: list, recheck: int) -> list:
    """want 为升序交易日。have 为空 → 全部（首次回补）；否则只报 want 最后 recheck 天里不在 have 中的日子。"""
    if not have:
        return list(want)
    tail = list(want)[-max(int(recheck), 0):] if recheck > 0 else []
    return [d for d in tail if d not in have]


def _cell(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)


def sector_matrix(df: pd.DataFrame, days: int) -> dict:
    """取最近 days 个交易日的紧凑矩阵：行 = 行业（只含最新一日出现的，sorted），列 = 交易日，缺失为 None。"""
    empty = {"dates": [], "names": [], "net": [], "pct": []}
    if df is None or len(df) == 0:
        return empty
    d = df.copy()
    d["trade_date"] = d["trade_date"].astype(str)
    dates = sorted(d["trade_date"].unique())[-max(int(days), 1):]
    d = d[d["trade_date"].isin(dates)]
    names = sorted(d.loc[d["trade_date"] == dates[-1], "industry"].astype(str).unique())
    d = d.drop_duplicates(["trade_date", "industry"], keep="last")
    out = {"dates": dates, "names": names}
    for key, col in (("net", "net_amount"), ("pct", "pct_change")):
        wide = d.pivot(index="industry", columns="trade_date", values=col).reindex(index=names, columns=dates)
        out[key] = [[_cell(v) for v in row] for row in wide.astype(float).to_numpy()]
    return out


def _is_num(v) -> bool:
    if isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, float, np.integer, np.floating)):
        return False
    return math.isfinite(float(v))


def _seg(el: dict):
    for k in _ID_FIELDS:
        v = el.get(k)
        if isinstance(v, str) and v.strip():
            return v.replace("\n", "").replace("\r", "")
    return None


def _walk(node, path: str, out: dict) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            p = p.replace("\n", "").replace("\r", "")
            if _is_num(v):
                out.setdefault(p, v.item() if isinstance(v, np.generic) else v)
            elif isinstance(v, (dict, list)):
                _walk(v, p, out)
        dates = node.get("dates")
        if isinstance(dates, list) and dates:  # 时间序列：与 dates 等长的兄弟序列记最新值
            for k, v in node.items():
                if k == "dates" or not isinstance(v, list) or len(v) != len(dates) or not _is_num(v[-1]):
                    continue
                p = (f"{path}.{k}" if path else str(k)).replace("\n", "").replace("\r", "")
                last = v[-1]
                out.setdefault(f"{p}.last", last.item() if isinstance(last, np.generic) else last)
    elif isinstance(node, list):
        seen = set()
        for el in node:
            if not isinstance(el, dict):
                continue
            s = _seg(el)
            if s is None or s in seen:
                continue
            seen.add(s)
            _walk(el, f"{path}.{s}" if path else s, out)


def flatten(payload: dict) -> list:
    """把一天的 payload 摊平成 [(key, value)]：只收有限数值；list 元素靠 key/code/name/label 取路径段，其余跳过。"""
    out: dict = {}
    if isinstance(payload, dict):
        _walk({k: v for k, v in payload.items() if k not in _SKIP_TOP}, "", out)
    return sorted(out.items())


def update_daily(db_dir, payload: dict, history_dir=None) -> int:
    """把 payload 摊平写进 db_dir/daily.csv（该交易日已有的行整体替换）；history_dir 给出时先补库里没有的历史存档。"""
    td = ((payload or {}).get("meta") or {}).get("trade_date")
    if not td:
        log.warning("每日指标库：payload 缺交易日，未写入")
        return 0
    td = str(td)
    path = Path(db_dir) / DAILY_FILE
    df = load_csv(path, DAILY_COLS)
    parts = [df]
    if history_dir and Path(history_dir).is_dir():
        have = set(df["trade_date"])
        for f in sorted(Path(history_dir).glob("*.json")):
            d = f.stem.replace("-", "")
            if len(d) != 8 or not d.isdigit() or d in have or d == td:
                continue
            try:
                rows = flatten(json.loads(f.read_text(encoding="utf-8")))
            except Exception as e:  # noqa: BLE001
                log.warning("历史存档 %s 读取失败（%s: %s），已跳过", f.name, type(e).__name__, str(e)[:80])
                continue
            parts.append(pd.DataFrame([(d, k, float(v)) for k, v in rows], columns=DAILY_COLS))
    rows = flatten(payload)
    parts = [p[p["trade_date"] != td] for p in parts if len(p)]
    parts.append(pd.DataFrame([(td, k, float(v)) for k, v in rows], columns=DAILY_COLS))
    merged = pd.concat(parts, ignore_index=True)
    merged = merged.drop_duplicates(["trade_date", "key"], keep="last")
    save_csv(merged, path, ["trade_date", "key"])
    return len(rows)


def write_matrix(matrix: dict, out_dir) -> Path:
    """写 out_dir/db/sector_flow.json（紧凑 JSON、原子写）。"""
    path = Path(out_dir) / "db" / MATRIX_FILE
    _atomic_write(path, json.dumps(matrix, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return path


def publish_csv(db_dir, out_dir) -> None:
    """把 db_dir 下的 *.csv 复制到 out_dir/db/；db_dir 不存在则什么都不做。"""
    src = Path(db_dir)
    if not src.is_dir():
        return
    dst = Path(out_dir) / "db"
    for f in sorted(src.glob("*.csv")):
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dst / f.name)
