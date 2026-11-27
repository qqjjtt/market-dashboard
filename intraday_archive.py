"""Archive complete A-share intraday charts and stage them for static hosting."""
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from render import archive_json

SH = ZoneInfo("Asia/Shanghai")
CLOSE = "15:00"


def _iso_date(value: str) -> str | None:
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return day.isoformat() if value in (day.isoformat(), day.strftime("%Y%m%d")) else None


def _has_points(series, size: int) -> bool:
    return isinstance(series, list) and len(series) == size


def _complete(intraday: dict, trade_date: str) -> bool:
    meta = intraday.get("meta") or {}
    grid = intraday.get("grid") or []
    size = len(grid)
    if (not grid or grid[-1] != CLOSE or _iso_date(meta.get("trade_date")) != trade_date
            or meta.get("demo") or meta.get("sampled_at") != CLOSE
            or meta.get("points_count") != size or meta.get("total_grid") != size):
        return False

    volume = intraday.get("volume") or {}
    if volume.get("date") or volume.get("today_cum_yi"):
        if (_iso_date(volume.get("date")) != trade_date
                or not _has_points(volume.get("today_cum_yi"), size)):
            return False

    def closed(item, curves):
        return (item.get("has_close_bar") is True and _iso_date(item.get("trade_date")) == trade_date
                and all(_has_points(item.get(key), size) for key in curves))

    sectors = intraday.get("sectors") or []
    if not sectors or not all(closed(s, ("pct_curve", "flow_curve", "amt_curve")) for s in sectors):
        return False
    return all(
        not (leader.get("pct_curve") or leader.get("flow_curve"))
        or closed(leader, ("pct_curve", "flow_curve"))
        for sector in sectors for leader in (sector.get("leaders") or [])
    )


def _atomic_write(path: Path, data: bytes) -> None:
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def archive_intraday(payload: dict, directory: str | Path, *, requested_date: str,
                     now: datetime) -> Path | None:
    """Write a chart only for today's actual trading session, after its close."""
    now = now.astimezone(SH) if now.tzinfo else now.replace(tzinfo=SH)
    trade_date = now.date().isoformat()
    meta = payload.get("meta") or {}
    intraday = payload.get("intraday")
    if (now.time() < time(15) or meta.get("demo") or _iso_date(requested_date) != trade_date
            or _iso_date(meta.get("trade_date")) != trade_date
            or not isinstance(intraday, dict) or not _complete(intraday, trade_date)):
        return None
    path = Path(directory) / "intraday" / f"{trade_date}.json"
    if not path.exists():
        _atomic_write(path, archive_json(intraday).encode("utf-8"))
    return path


def publish_intraday(directory: str | Path, out_dir: str | Path) -> None:
    """Stage date files and an index that lists the existing archive newest first."""
    source = Path(directory) / "intraday"
    target = Path(out_dir) / "history" / "intraday"
    files = sorted((p for p in source.glob("*.json")
                    if p.is_file() and _iso_date(p.stem) == p.stem), reverse=True)
    if source.resolve() != target.resolve():
        for path in files:
            _atomic_write(target / path.name, path.read_bytes())
    manifest = archive_json({"dates": [p.stem for p in files]}).encode("utf-8")
    _atomic_write(source / "manifest.json", manifest)
    _atomic_write(target / "manifest.json", manifest)
