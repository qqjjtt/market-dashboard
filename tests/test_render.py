import json
import math

import numpy as np
import pytest

from render import render, to_json


def test_render_escapes_script_close():
    s = to_json({"t": "</script><b>x", "n": float("nan")})
    assert "</script>" not in s and json.loads(s) == {"t": "</script><b>x", "n": None}


def test_to_json_nan_inf_and_numpy():
    d = {"a": [float("inf"), -math.inf, np.float64("nan"), np.float64(1.5)], "b": np.int64(3),
         "c": np.bool_(True), "d": (1, 2), "e": {3: "x"}, "z": "中文"}
    s = to_json(d)
    assert json.loads(s) == {"a": [None, None, None, 1.5], "b": 3, "c": True, "d": [1, 2], "e": {"3": "x"}, "z": "中文"}
    assert "NaN" not in s and "Infinity" not in s


def test_render_replaces_placeholder_once(tmp_path):
    t = tmp_path / "t.html"
    t.write_text("<script>const DATA = /*__DATA__*/null;</script>", encoding="utf-8")
    out = tmp_path / "sub" / "o.html"
    render(str(t), {"x": 1}, str(out))
    assert out.read_text(encoding="utf-8") == '<script>const DATA = {"x":1};</script>'


def test_render_requires_placeholder(tmp_path):
    t = tmp_path / "t.html"
    t.write_text("<script>const DATA = null;</script>", encoding="utf-8")
    with pytest.raises(ValueError):
        render(str(t), {}, str(tmp_path / "o.html"))
    t.write_text("/*__DATA__*/null /*__DATA__*/null", encoding="utf-8")
    with pytest.raises(ValueError):
        render(str(t), {}, str(tmp_path / "o.html"))


def test_no_method_text_in_template():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "template.html").read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    for bad in ("σ", "标准差", "÷", "Σ"):
        assert bad not in src, bad


def test_calendar_page_wired():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "template.html").read_text(encoding="utf-8")
    assert 'id="p-calendar"' in src
    assert 'key: "calendar"' in src
    assert "function renderCalendar" in src
    assert "数据来源 tushare eco_cal，时间为北京时间；预告时间以官方发布为准。" in src
    m = re.search(r"function renderCalendar\(.*?\n\}\n", src, flags=re.S)
    assert m, "renderCalendar body not found"
    assert "innerHTML" not in m.group(0)
