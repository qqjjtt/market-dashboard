"""JSON 序列化与模板填充。"""
from __future__ import annotations

import json
import math
import os

import numpy as np
import pandas as pd

PLACEHOLDER = "/*__DATA__*/null"


def _clean(o):
    """递归转成 JSON 友好类型：NaN/inf/NaT → None，numpy 标量 → Python，其余未知类型 → str。"""
    if o is None or isinstance(o, (str, bool)):
        return o
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        v = float(o)
        return v if math.isfinite(v) else None
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return [_clean(v) for v in o.tolist()]
    if o is pd.NaT or o is pd.NA:
        return None
    if isinstance(o, pd.Timestamp):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    return str(o)


def to_json(payload: dict) -> str:
    """紧凑 JSON，可安全嵌入 <script>：`</` → `<\\/`，`<!--` 与 U+2028/2029 转义。"""
    s = json.dumps(_clean(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return (s.replace("</", "<\\/").replace("<!--", "\\u003c!--")
             .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render(template_path: str, payload: dict, out_path: str) -> None:
    """把 payload 嵌入模板中唯一的 `/*__DATA__*/null`；找不到或出现多处则抛 ValueError。"""
    with open(template_path, encoding="utf-8") as fh:
        tpl = fh.read()
    n = tpl.count(PLACEHOLDER)
    if n != 1:
        raise ValueError(f"模板中应恰有 1 处 {PLACEHOLDER}，实际 {n} 处")
    html = tpl.replace(PLACEHOLDER, to_json(payload), 1)
    d = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(d, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
