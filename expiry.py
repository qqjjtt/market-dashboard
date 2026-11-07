"""衍生品到期日推算（纯计算，只提示，不影响流动性指数）。"""
import datetime as dt

# 美股休市日，手工维护（遇第三个周五休市时提前）
US_HOLIDAYS = ["20261126", "20261225", "20270101", "20270118", "20270215", "20270326",
               "20270531", "20270618", "20270705", "20270906", "20271125", "20271224"]


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _fmt(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def _shift_forward(day: str, trade_days: list[str]) -> str:
    if day in set(trade_days):
        return day
    later = [t for t in trade_days if t >= day]
    return min(later) if later else day


def cn_events(months: list[tuple[int, int]], trade_days: list[str]) -> list[dict]:
    out = []
    for y, m in months:
        q = m in (3, 6, 9, 12)
        out.append({"date": _shift_forward(_fmt(_nth_weekday(y, m, 4, 3)), trade_days),
                    "market": "A股", "kind": "index", "name": "股指期货、期权交割日", "quarterly": q})
        out.append({"date": _shift_forward(_fmt(_nth_weekday(y, m, 2, 4)), trade_days),
                    "market": "A股", "kind": "etf_option", "name": "ETF 期权到期日", "quarterly": False})
    return out


def us_events(months: list[tuple[int, int]], holidays: set[str]) -> list[dict]:
    out = []
    for y, m in months:
        d = _nth_weekday(y, m, 4, 3)
        while d.weekday() >= 5 or _fmt(d) in holidays:
            d -= dt.timedelta(days=1)
        q = m in (3, 6, 9, 12)
        out.append({"date": _fmt(d), "market": "美股", "kind": "us",
                    "name": "四巫日（股指期货、期权同日到期）" if q else "月度期权到期日",
                    "quarterly": q})
    return out


def reminders(events: list[dict], day: str, next_days: list[str]) -> dict:
    end = next_days[-1] if next_days else day
    upcoming = sorted((e for e in events if day <= e["date"] <= end), key=lambda e: e["date"])
    return {"today": [e for e in events if e["date"] == day], "upcoming": upcoming}
