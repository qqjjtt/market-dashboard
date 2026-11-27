import json
import pytest

import config
import intraday_server as isrv
import intraday_tracker as it


def test_minute_grid_241_points():
    grid = it.build_minute_grid()
    assert len(grid) == 241
    assert grid[0] == "09:30" and grid[120] == "11:30"
    assert grid[121] == "13:01" and grid[-1] == "15:00"


def test_to_secid_and_tx_sym():
    assert it.to_secid("512480.SH") == "1.512480"
    assert it.to_secid("159755.SZ") == "0.159755"
    assert it.to_secid("BK1036") == "90.BK1036"
    assert it.to_tx_sym("512480.SH") == "sh512480"
    assert it.to_tx_sym("159755.SZ") == "sz159755"


def test_align_intraday_curves_matches_target_flow():
    bars = [
        ("09:30", 1.000, 10_000_000.0),
        ("09:31", 1.010, 20_000_000.0),
        ("09:32", 1.005, 15_000_000.0),
        ("09:33", 1.020, 35_000_000.0),
    ]
    res = it.align_intraday_curves(bars, pre_close=1.000, target_net_flow_yi=12.5)
    assert res["last_idx"] == 3
    assert res["pct"] == [0.0, 1.0, 0.5, 2.0]
    assert res["amt"] == [0.1, 0.3, 0.45, 0.8]
    assert len(res["flow"]) == 4
    assert res["flow"][-1] == pytest.approx(12.5, abs=0.05)


def test_parse_em_ths_tx_intraday():
    em_json = json.dumps({
        "data": {
            "preClose": 1.0,
            "trends": [
                "2026-11-27 09:30,1.01,1000,10100000.0",
                "2026-11-27 09:31,1.02,2000,20400000.0",
            ],
        }
    })
    em_res = it._parse_em_intraday(em_json)
    assert em_res["date"] == "20261127" and em_res["pre_close"] == 1.0
    assert len(em_res["bars"]) == 2 and em_res["bars"][1] == ("09:31", 1.02, 20400000.0)

    ths_txt = 'quotebridge_v4_time_hs_512480_today({"hs_512480":{"date":"20261127","pre":"1.00","data":"0930,1.01,10100000,1.01,1000;0931,1.02,20400000,1.015,2000"}})'
    ths_res = it._parse_ths_intraday(ths_txt, "512480")
    assert ths_res["date"] == "20261127" and ths_res["pre_close"] == 1.0
    assert len(ths_res["bars"]) == 2 and ths_res["bars"][0] == ("09:30", 1.01, 10100000.0)

    tx_txt = json.dumps({
        "data": {
            "sh512480": {
                "data": [
                    {
                        "date": "20261127",
                        "prec": "1.00",
                        "data": [
                            "0930 1.01 1000 10100000.00",
                            "0931 1.02 3000 30500000.00",
                        ],
                    }
                ]
            }
        }
    })
    tx_res = it._parse_tx_intraday(tx_txt, "sh512480")
    assert tx_res["date"] == "20261127" and tx_res["pre_close"] == 1.0
    # 腾讯累计成交额转单分钟增量：30500000 - 10100000 = 20400000
    assert tx_res["bars"][0] == ("09:30", 1.01, 10100000.0)
    assert tx_res["bars"][1] == ("09:31", 1.02, 20400000.0)


def test_build_demo_intraday_payload_and_export(tmp_path):
    p = it.build_demo_intraday_payload("20261106")
    assert p["meta"]["trade_date"] == "20261106"
    assert len(p["grid"]) == 241
    assert len(p["sectors"]) == len(config.INTRADAY_ETFS)
    s0 = p["sectors"][0]
    assert len(s0["pct_curve"]) == 241 and len(s0["flow_curve"]) == 241
    assert len(s0["leaders"]) == 4 and len(s0["leaders"][0]["pct_curve"]) == 241

    out_html = tmp_path / "intraday.html"
    isrv.export_static(str(out_html), demo=True)
    text = out_html.read_text(encoding="utf-8")
    assert "/*__INTRADAY_DATA__*/null" not in text
    assert "板块资金流向与 ETF 强弱 · 分时曲线" in text
