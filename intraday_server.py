"""本地交互式「板块 ETF 与强势股 · 分时强弱看板」Web 服务与静态快照导出入口。

用法：
1. 启动本地实时 Web 服务（开盘后每分钟自动刷新东财/同花顺分时与日K）：
   .venv/bin/python intraday_server.py --port 8766

2. 导出单文件离线交互快照（内嵌当前分时数据，且打开后浏览器仍支持直连每分钟刷新）：
   .venv/bin/python intraday_server.py --export site/intraday.html [--demo]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import intraday_tracker as it
from intraday_archive import archive_intraday, publish_intraday
from render import to_json

ROOT = Path(__file__).resolve().parent
TEMPLATE_PATH = ROOT / "intraday_template.html"
HISTORY_DIR = ROOT / "history" / "intraday"
PLACEHOLDER = "/*__INTRADAY_DATA__*/null"
SH = ZoneInfo("Asia/Shanghai")
_HISTORY_FILE = re.compile(r"^(?:manifest|\d{4}-\d{2}-\d{2})\.json$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("intraday_server")


class IntradayApp:
    """线程安全的盘中分时数据服务（含 15 秒短缓存，避免多标签页并发刷爆上游）。"""

    def __init__(self, demo: bool = False, cache_ttl: float = 15.0):
        self.demo = demo
        self.cache_ttl = cache_ttl
        self._lock = threading.Lock()
        self._cached_payload: dict | None = None
        self._cached_at: float = 0.0

    def get_payload(self, refresh: bool = False) -> dict:
        with self._lock:
            now = time.monotonic()
            if not refresh and self._cached_payload is not None and (now - self._cached_at) < self.cache_ttl:
                return self._cached_payload
            if self.demo:
                payload = it.build_demo_intraday_payload()
            else:
                payload = it.collect_intraday_payload()
            self._cached_payload = payload
            self._cached_at = now
            if not self.demo:
                store_closed_chart(payload)
            return payload

    def render_html(self, refresh: bool = False) -> str:
        tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
        payload = self.get_payload(refresh=refresh)
        if PLACEHOLDER in tpl:
            return tpl.replace(PLACEHOLDER, to_json(payload), 1)
        return tpl


def read_history(url_path: str, directory: Path = HISTORY_DIR) -> bytes | None:
    """只读 history/intraday 下的清单和 YYYY-MM-DD.json，拒绝其它路径。"""
    name = urllib.parse.unquote(url_path.rstrip("/").rsplit("/", 1)[-1])
    if not _HISTORY_FILE.fullmatch(name):
        return None
    path = directory / name
    if not path.is_file():
        return None
    return path.read_bytes()


def store_closed_chart(payload: dict, directory: Path = ROOT / "history") -> None:
    """收盘后的完整分时写入 history/intraday，并刷新可搜寻的日期清单。"""
    meta = payload.get("meta") or {}
    day = meta.get("trade_date")
    if not day or meta.get("demo"):
        return
    try:
        archive_intraday(
            {"meta": {"trade_date": day, "demo": False}, "intraday": payload},
            directory,
            requested_date=str(day),
            now=datetime.now(SH),
        )
        publish_intraday(directory, directory.parent)
    except Exception:  # noqa: BLE001
        log.warning("收盘分时存档失败", exc_info=True)


def make_handler(app: IntradayApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, data: dict, status: int = HTTPStatus.OK):
            body = to_json(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = HTTPStatus.OK):
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            try:
                if path in ("/", "/index.html", "/intraday.html"):
                    refresh = qs.get("refresh", ["0"])[0] in ("1", "true")
                    self._send_html(app.render_html(refresh=refresh))
                elif path == "/api/intraday":
                    refresh = qs.get("refresh", ["0"])[0] in ("1", "true")
                    self._send_json(app.get_payload(refresh=refresh))
                elif path == "/favicon.ico":
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                elif path.startswith("/history/intraday/"):
                    body = read_history(path)
                    if body is None:
                        self._send_json({"error": f"Not found: {path}"}, status=HTTPStatus.NOT_FOUND)
                    else:
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(body)
                else:
                    self._send_json({"error": f"Not found: {path}"}, status=HTTPStatus.NOT_FOUND)
            except Exception as e:  # noqa: BLE001
                log.exception("请求处理失败: %s", self.path)
                self._send_json({"error": f"{type(e).__name__}: {e}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


def export_static(out_path: str, demo: bool = False) -> Path:
    app = IntradayApp(demo=demo)
    html = app.render_html(refresh=True)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, target)
    log.info("已导出盘中分时看板快照至 %s (%d 字节)", target, len(html.encode("utf-8")))
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="板块 ETF 与强势股 · 盘中分时强弱看板")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=8766, help="监听端口（默认 8766）")
    ap.add_argument("--demo", action="store_true", help="使用离线模拟数据")
    ap.add_argument("--export", metavar="PATH", help="导出单文件静态 HTML 快照并退出（如 site/intraday.html）")
    args = ap.parse_args(argv)

    if args.export:
        export_static(args.export, demo=args.demo)
        return 0

    app = IntradayApp(demo=args.demo)
    log.info("正在预热盘中板块 ETF 与强势股分时数据...")
    try:
        p = app.get_payload(refresh=True)
        m = p["meta"]
        log.info("预热完成：交易日 %s · 已采到 %s (%d/%d 分钟点，%d 个板块，源 %s)",
                 m["trade_date"], m["sampled_at"], m["points_count"], m["total_grid"],
                 m["sector_count"], m["source"])
    except Exception as e:  # noqa: BLE001
        log.warning("预热警告: %s", e)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    url = f"http://{args.host}:{args.port}"
    log.info("盘中分时看板已启动: %s (按 Ctrl+C 停止)", url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到退出信号，正在关闭服务...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
