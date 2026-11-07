"""页头四句结论（SPEC §4.0）。纯函数，不做 IO。缺失值 None，绝不补 0。"""
from __future__ import annotations

import math

import config

MINUS = "−"
_DIR_WORD = {1: "松", 0: "平", -1: "紧"}


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and not math.isfinite(x))


def signed(x: float, nd: int = 1) -> str:
    """带符号数：+45.0 / −5.0（负号用 −）。"""
    s = f"{abs(x):.{nd}f}"
    if float(s) == 0:
        return s
    return f"+{s}" if x > 0 else f"{MINUS}{s}"


def _md(d: str | None) -> str:
    return f"{int(d[4:6])}/{int(d[6:8])}" if d and len(d) >= 8 else "—"


def _card(key: str, title: str, text: str, sub: str, tone: str) -> dict:
    return {"key": key, "title": title, "text": text, "sub": sub, "tone": tone}


def national_team_verdict(last3: list[float]) -> tuple[str, str]:
    """最近 3 个已披露日（新→旧）净申赎 → (label, tone)。

    3 日合计 > 0 且至少 2 日为正 → 仍在；合计 < 0 且至少 2 日为负 → 疑似撤出；其余 → 放缓。
    """
    vals = [float(v) for v in last3 if _ok(v)]
    if not vals:
        return "数据不足", "gray"
    total = sum(vals)
    pos = sum(v > 0 for v in vals)
    neg = sum(v < 0 for v in vals)
    if total > 0 and pos >= 2:
        return "仍在", "red"
    if total < 0 and neg >= 2:
        return "疑似撤出", "green"
    return "放缓", "orange"


def _liquidity_card(vote: dict, rate_expect: dict, fed_card: dict) -> dict:
    sigs = vote.get("signals", [])
    parts = []
    for s in sigs:
        d = s.get("dir")
        parts.append(f"{s['name']} {_DIR_WORD[d]}" if d in _DIR_WORD else f"{s['name']} 缺失（不计票）")
    line1 = " · ".join(parts)

    chg = (fed_card or {}).get("change")
    if _ok(chg):
        arrow = "↑" if chg > 0 else "↓" if chg < 0 else "→"
        fed = f"美元净流动性 4 周 {arrow} {abs(chg):.0f} 十亿"
    else:
        fed = "美元净流动性数据缺失"
    label = (rate_expect or {}).get("label")
    rate = label if label and label != "无数据" else "利率预期无数据"
    line2 = f"外围：{fed} · {rate}"

    if all(s.get("dir") is None for s in sigs):
        text, tone = "国内流动性数据不足", "gray"
    else:
        text = f"国内流动性{vote['label']}（合计 {vote['score']:+d}）"
        tone = {"偏松": "red", "偏紧": "green"}.get(vote["label"], "blue")
    return _card("liquidity", "流动性", text, f"{line1}\n{line2}", tone)


_ZONE_TONE = {"极度紧张": "green", "偏紧": "green", "中性": "blue", "偏松": "red", "极度宽松": "red"}


def _liquidity_card_from_index(li: dict) -> dict:
    """流动性指数驱动的结论卡（设计 §5.3）：紧 → green、松 → red（A 股红涨绿跌约定），空 → gray。"""
    if not _ok(li.get("value")):
        return _card("liquidity", "流动性", "流动性数据不足", li.get("reason") or "数据不足", "gray")
    zone = li.get("zone") or ""
    return _card("liquidity", "流动性", f"流动性{zone}（{li['value']}）", li.get("reason") or "",
                 _ZONE_TONE.get(zone, "blue"))


def _stale(date: str | None, trade_date: str | None) -> str | None:
    """面板日期与交易日 D 不同 → 返回「M/D」；同日或任一缺失 → None。"""
    return _md(date) if date and trade_date and date != trade_date else None


def _sector_card(sector_day: dict, sector_date: str | None = None, trade_date: str | None = None) -> dict:
    inflow = [r["name"] for r in (sector_day or {}).get("inflow", [])[:3]]
    outflow = [r["name"] for r in (sector_day or {}).get("outflow", [])[:3]]
    if not inflow and not outflow:
        return _card("sector", "板块流向", "暂无板块资金流向数据", "同花顺行业口径", "gray")
    text = f"流入：{'、'.join(inflow) or '—'} ｜ 流出：{'、'.join(outflow) or '—'}"
    sub = "同花顺行业 · 当日净额前三 / 后三"
    stale = _stale(sector_date, trade_date)
    if stale:
        sub += f"（截至 {stale}）"
    return _card("sector", "板块流向", text, sub, "blue")


def _spike_note(spikes) -> str:
    """观察池天量提示：最多点名 2 只，多于 2 只加「等 N 只」；无 → 空串。"""
    names = [str(s.get("name") or s.get("code") or "") for s in (spikes or [])]
    names = [n for n in names if n]
    if not names:
        return ""
    more = f" 等 {len(names)} 只" if len(names) > 2 else ""
    return " · 观察池天量：" + "、".join(names[:2]) + more


def _team_card(team_last3: list[float], team_asof: str | None, round_total: float | None,
               watch_spikes=None) -> dict:
    vals = [float(v) for v in team_last3 if _ok(v)]
    label, tone = national_team_verdict(vals)
    note = _spike_note(watch_spikes)
    if not vals:
        return _card("team", "国家队", "暂无已披露数据", f"{config.TEAM_NAME} ETF 净申赎" + note, "gray")
    total = sum(vals)
    n = len(vals)
    if label == "仍在":
        text = f"仍在（近 {n} 日 {signed(total)} 亿）"
    elif label == "疑似撤出":
        text = f"疑似撤出（{signed(total)} 亿）"
    else:
        text = f"放缓（近 {n} 日 {signed(total)} 亿）"
    sub = f"截至 {_md(team_asof)}"
    if _ok(round_total):
        sub += f" · 本轮累计 {signed(round_total)} 亿"
    return _card("team", "国家队", text, sub + note, tone)


def _band(t: float) -> str:
    if t >= 80:
        return "过热"
    if t >= 60:
        return "温暖"
    if t >= 40:
        return "中性"
    if t >= 20:
        return "偏冷"
    return "冰冷"


def _retail_card(temp_kpis: dict, market_conc: float | None, market_conc_yday: float | None,
                 conc_date: str | None = None, trade_date: str | None = None) -> dict:
    today = (temp_kpis or {}).get("today")
    if not _ok(today):
        return _card("retail", "散户情绪", "体温暂无数据", "五维口径", "gray")
    if _ok(market_conc):
        conc = f"浓度 {market_conc:.1f}%"
        if _ok(market_conc_yday):
            conc += f"（{signed(market_conc - market_conc_yday)}pp）"
        stale = _stale(conc_date, trade_date)  # 体温是 D 当日，浓度晚于它时必须说明
        if stale:
            conc += f"（浓度截至 {stale}）"
    else:
        conc = "浓度 —"
    tone = "red" if today >= 60 else "orange" if today >= 40 else "blue"
    dt = (temp_kpis or {}).get("delta_text")
    return _card("retail", "散户情绪", f"体温 {today:.1f}（{_band(today)}）· {conc}",
                 f"五维口径 · 较昨日{dt}" if dt else "五维口径", tone)


def build_conclusions(vote: dict, rate_expect: dict, fed_card: dict, sector_day: dict,
                      team_last3: list[float], team_asof: str | None, round_total: float | None,
                      temp_kpis: dict, market_conc: float | None, market_conc_yday: float | None,
                      trade_date: str | None = None, sector_date: str | None = None,
                      conc_date: str | None = None, watch_spikes: list[dict] | None = None,
                      liquidity_index: dict | None = None) -> list[dict]:
    """四张结论卡（流动性 / 板块流向 / 国家队 / 散户情绪）。

    trade_date = 交易日 D；sector_date / conc_date 为板块流向 / 韭菜浓度各自的数据日期，
    与 D 不同时在对应卡片上注明「（截至 M/D）」，避免把旧数据当成当天的。

    liquidity_index 不为 None 时流动性卡由指数驱动（text/sub/tone 见 _liquidity_card_from_index），
    否则沿用原投票逻辑。投票逻辑下流动性卡的 sub 含两行（\\n 分隔）：信号方向行 + 外围一句。
    """
    return [
        _liquidity_card_from_index(liquidity_index) if liquidity_index is not None
        else _liquidity_card(vote, rate_expect, fed_card),
        _sector_card(sector_day, sector_date, trade_date),
        _team_card(team_last3, team_asof, round_total, watch_spikes),
        _retail_card(temp_kpis, market_conc, market_conc_yday, conc_date, trade_date),
    ]
