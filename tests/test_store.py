import json
import logging
import math

import numpy as np
import pandas as pd

import store
from store import DAILY_COLS, SECTOR_COLS


def sec(rows):
    return pd.DataFrame(rows, columns=SECTOR_COLS)


# ---------- load_csv / save_csv ----------

def test_load_csv_missing_file_returns_empty_with_cols(tmp_path):
    df = store.load_csv(tmp_path / "nope.csv", SECTOR_COLS)
    assert df.empty and list(df.columns) == SECTOR_COLS


def test_load_csv_corrupt_or_missing_cols_warns(tmp_path, caplog):
    p = tmp_path / "bad.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        df = store.load_csv(p, SECTOR_COLS)
    assert df.empty and list(df.columns) == SECTOR_COLS
    assert any("bad.csv" in r.getMessage() for r in caplog.records)
    p.write_bytes(b"\xff\xfe\x00\x01 not csv \x00\x00")
    assert store.load_csv(p, DAILY_COLS).empty


def test_load_csv_trade_date_is_str(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("trade_date,industry,net_amount,pct_change\n20261106,银行,1.5,0.3\n", encoding="utf-8")
    df = store.load_csv(p, SECTOR_COLS)
    assert df["trade_date"].tolist() == ["20261106"] and df["net_amount"].iloc[0] == 1.5


def test_save_csv_sorted_atomic_and_makes_dir(tmp_path):
    p = tmp_path / "sub" / "dir" / "s.csv"
    df = sec([("20261106", "B", 1.0, 0.1), ("20261105", "Z", 2.0, 0.2), ("20261105", "A", 3.0, 0.3)])
    store.save_csv(df, p, ["trade_date", "industry"])
    assert p.exists() and not (p.parent / "s.csv.tmp").exists()
    text = p.read_bytes().decode("utf-8")
    assert "\r" not in text and text.splitlines()[0] == ",".join(SECTOR_COLS)
    back = store.load_csv(p, SECTOR_COLS)
    assert list(zip(back["trade_date"], back["industry"])) == [("20261105", "A"), ("20261105", "Z"), ("20261106", "B")]


# ---------- merge_sector ----------

def test_merge_new_overrides_old_and_dedups():
    old = sec([("20261105", "A", 1.0, 0.1), ("20261105", "B", 2.0, 0.2)])
    new = sec([("20261105", "A", 9.0, 0.9), ("20261106", "A", 5.0, 0.5), ("20261106", "A", 6.0, 0.6)])
    m = store.merge_sector(old, new)
    assert len(m) == 3 and not m.duplicated(["trade_date", "industry"]).any()
    got = {(r.trade_date, r.industry): r.net_amount for r in m.itertuples()}
    assert got == {("20261105", "A"): 9.0, ("20261105", "B"): 2.0, ("20261106", "A"): 6.0}


def test_merge_null_net_does_not_override():
    old = sec([("20261105", "A", 1.0, 0.1)])
    new = sec([("20261105", "A", np.nan, 0.9), ("20261105", "B", np.nan, 0.1)])
    m = store.merge_sector(old, new)
    assert len(m) == 1 and m["net_amount"].iloc[0] == 1.0 and m["pct_change"].iloc[0] == 0.1


def test_merge_empty_new_returns_old():
    old = sec([("20261105", "A", 1.0, 0.1)])
    assert store.merge_sector(old, sec([])).equals(old)
    assert store.merge_sector(old, None).equals(old)


def test_merge_rounds_to_two_decimals():
    m = store.merge_sector(sec([]), sec([("20261105", "A", 1.23456, 0.987654)]))
    assert m["net_amount"].iloc[0] == 1.23 and m["pct_change"].iloc[0] == 0.99


# ---------- missing_days ----------

def test_missing_days_empty_have_returns_all():
    want = ["20261101", "20261102", "20261103"]
    assert store.missing_days(set(), want, 2) == want


def test_missing_days_only_recent_window():
    want = [f"2026110{i}" for i in range(1, 9)]
    have = {"20261102", "20261107"}
    assert store.missing_days(have, want, 3) == ["20261106", "20261108"]
    assert store.missing_days(set(want), want, 3) == []


# ---------- sector_matrix ----------

def test_matrix_alignment_none_and_names_from_latest_day():
    df = sec([
        ("20261104", "旧名", 1.0, 0.1),
        ("20261104", "A", 2.0, 0.2),
        ("20261105", "A", np.nan, 0.3),
        ("20261105", "B", 4.0, np.nan),
        ("20261106", "B", 5.556, 0.5),
        ("20261106", "A", 6.0, 0.6),
    ])
    m = store.sector_matrix(df, 10)
    assert m["dates"] == ["20261104", "20261105", "20261106"]
    assert m["names"] == ["A", "B"]
    assert m["net"] == [[2.0, None, 6.0], [None, 4.0, 5.56]]
    assert m["pct"] == [[0.2, 0.3, 0.6], [None, None, 0.5]]


def test_matrix_keeps_only_recent_days():
    df = sec([(f"2026110{i}", "A", float(i), 0.0) for i in range(1, 7)])
    m = store.sector_matrix(df, 2)
    assert m["dates"] == ["20261105", "20261106"] and m["net"] == [[5.0, 6.0]]


def test_matrix_empty():
    assert store.sector_matrix(sec([]), 5) == {"dates": [], "names": [], "net": [], "pct": []}


# ---------- flatten ----------

def test_flatten_nested_dict_and_skips():
    payload = {
        "meta": {"x": 1}, "conclusions": [{"label": "a", "v": 1}], "calendar": {"n": 1}, "eco_calendar": {"n": 2},
        "news": {"n": 3}, "policy_ops": [{"name": "p", "v": 1}], "sector_flow": {"n": 4},
        "liquidity": {"vote": {"score": 2, "label": "偏松"}, "flag": True},
        "top": 3.5,
    }
    got = dict(store.flatten(payload))
    assert got == {"liquidity.vote.score": 2, "top": 3.5}


def test_flatten_lists_use_identifier_segments():
    payload = {
        "indices": [{"code": "000001.SH", "close": 3000.5, "chg": -0.5},
                    {"name": "沪深300", "close": 4000},
                    {"label": "标签", "v": 1},
                    {"key": "k1", "code": "ignored", "v": 2},
                    {"close": 1},                        # 无标识 → 跳过
                    {"name": "", "v": 9},                # 空串不算标识
                    {"code": "000001.SH", "close": 99}],  # 重复 → 只保留第一个
        "series": [1, 2, 3],                              # 标量 list → 跳过
        "hist": [{"date": "20261106", "v": 1}],
    }
    got = dict(store.flatten(payload))
    assert got == {"indices.000001.SH.close": 3000.5, "indices.000001.SH.chg": -0.5,
                   "indices.沪深300.close": 4000, "indices.标签.v": 1, "indices.k1.v": 2}


def test_flatten_only_finite_numbers_and_strips_newlines():
    payload = {"a": {"b": True, "c": float("nan"), "d": float("inf"), "e": "1", "f": None, "g": np.float64(2.5),
                     "h": np.int64(3), "i": np.bool_(True)},
               "rows": [{"name": "多\n行", "v": 1}]}
    got = store.flatten(payload)
    assert dict(got) == {"a.g": 2.5, "a.h": 3, "rows.多行.v": 1}
    keys = [k for k, _ in got]
    assert keys == sorted(keys) and len(keys) == len(set(keys))
    assert all(isinstance(v, (int, float)) and not math.isnan(v) for _, v in got)


# ---------- update_daily ----------

def payload(td, x):
    return {"meta": {"trade_date": td}, "temperature": {"score": x}, "rows": [{"name": "A", "v": x * 2}]}


def test_update_daily_replaces_same_day(tmp_path):
    assert store.update_daily(tmp_path, payload("20261105", 1)) == 2
    store.update_daily(tmp_path, payload("20261106", 5))
    store.update_daily(tmp_path, payload("20261106", 7))
    df = store.load_csv(tmp_path / "daily.csv", DAILY_COLS)
    d6 = {r.key: r.value for r in df[df.trade_date == "20261106"].itertuples()}
    assert d6 == {"rows.A.v": 14.0, "temperature.score": 7.0}
    assert len(df) == 4 and df["trade_date"].is_monotonic_increasing


def test_update_daily_backfills_from_history_and_skips_bad(tmp_path, caplog):
    hist = tmp_path / "history"
    hist.mkdir()
    (hist / "2026-11-03.json").write_text(json.dumps(payload("20261103", 3)), encoding="utf-8")
    (hist / "2026-11-04.json").write_text("{broken", encoding="utf-8")
    (hist / "2026-11-05.json").write_text(json.dumps(payload("20261105", 4)), encoding="utf-8")
    db = tmp_path / "db"
    store.update_daily(db, payload("20261105", 1))     # 11-05 已在库：存档不覆盖
    with caplog.at_level(logging.WARNING):
        n = store.update_daily(db, payload("20261106", 5), history_dir=hist)
    assert n == 2
    assert any("2026-11-04" in r.getMessage() for r in caplog.records)
    df = store.load_csv(db / "daily.csv", DAILY_COLS)
    assert sorted(df.trade_date.unique()) == ["20261103", "20261105", "20261106"]
    d5 = df[(df.trade_date == "20261105") & (df.key == "temperature.score")].value.iloc[0]
    assert d5 == 1.0


# ---------- write_matrix / publish_csv ----------

def test_write_matrix_compact_json(tmp_path):
    m = {"dates": ["20261106"], "names": ["银行"], "net": [[None]], "pct": [[1.5]]}
    p = store.write_matrix(m, tmp_path / "site")
    assert p == tmp_path / "site" / "db" / "sector_flow.json"
    text = p.read_text(encoding="utf-8")
    assert "银行" in text and " " not in text and json.loads(text) == m
    assert not list(p.parent.glob("*.tmp"))


def test_publish_csv_copies_csv_only(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    (db / "a.csv").write_text("x", encoding="utf-8")
    (db / "b.txt").write_text("y", encoding="utf-8")
    store.publish_csv(db, tmp_path / "site")
    assert (tmp_path / "site" / "db" / "a.csv").read_text() == "x"
    assert not (tmp_path / "site" / "db" / "b.txt").exists()
    store.publish_csv(tmp_path / "nodir", tmp_path / "site2")
    assert not (tmp_path / "site2").exists()
