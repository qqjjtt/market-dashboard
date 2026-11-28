from types import SimpleNamespace

import build
import limit_server


def test_limit_map_skips_demo_and_sources_without_tushare(tmp_path):
    assert build._limit_map(object(), "20261127", str(tmp_path), demo=True) == {"available": False, "reason": "demo"}
    assert build._limit_map(object(), "20261127", str(tmp_path), demo=False)["reason"] == "no_source"
    assert not (tmp_path / "limit.html").exists()


def test_limit_map_writes_snapshot_and_summary(tmp_path, monkeypatch):
    data = {"overview": {"available": True, "up_count": 417, "max_board": 6, "seal_rate": 90.8, "zha_count": 42,
                         "down_count": 0, "top_leaders": [{"name": "克来机电"}]},
            "promotion": {"overall_promo_rate": 44.6},
            "meta": {"trade_date": "20261127", "coverage": {"kpl_up": False}}}
    monkeypatch.setattr(limit_server.ServerApp, "get_dashboard", lambda self, date, days, refresh=False: data)
    monkeypatch.setattr(limit_server.ServerApp, "render_html", lambda self, snapshot=False: "<html>snap</html>")
    got = build._limit_map(SimpleNamespace(pro=object()), "20261127", str(tmp_path), demo=False)
    assert got["available"] and got["href"] == "limit.html" and got["up_count"] == 417
    assert got["leaders"] == ["克来机电"] and got["concepts"] is False and got["promo_rate"] == 44.6
    assert (tmp_path / "limit.html").read_text(encoding="utf-8") == "<html>snap</html>"


def test_limit_map_missing_day_and_failure_never_write(tmp_path, monkeypatch):
    src = SimpleNamespace(pro=object())
    monkeypatch.setattr(limit_server.ServerApp, "get_dashboard",
                        lambda self, date, days, refresh=False: {"overview": {"available": False}, "meta": {}})
    assert build._limit_map(src, "20261127", str(tmp_path), demo=False)["reason"] == "missing"

    def boom(self, date, days, refresh=False):
        raise RuntimeError("token=secret")
    monkeypatch.setattr(limit_server.ServerApp, "get_dashboard", boom)
    assert build._limit_map(src, "20261127", str(tmp_path), demo=False) == {"available": False, "reason": "failed"}
    assert not list(tmp_path.iterdir())
