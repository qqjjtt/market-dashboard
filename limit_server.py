"""涨停地图：本机 Web 服务与单文件快照导出。"""
from __future__ import annotations

import argparse
import logging
import re
import threading
import time
from datetime import date as calendar_date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import limit_tracker as lt
from render import to_json

TEMPLATE_PATH = Path(__file__).with_name('limit_template.html')
PLACEHOLDER = '/*__LIMIT_DATA__*/null'
log = logging.getLogger('limit_server')


def parse_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r'[0-9]{8}|[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('日期应为 YYYYMMDD 或 YYYY-MM-DD')
    try:
        return calendar_date.fromisoformat(value).strftime('%Y%m%d')
    except ValueError:
        raise ValueError('日期无效') from None


def parse_days(value: str | int, low: int, high: int) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'回看窗口应为 {low}–{high} 个交易日') from None
    if not low <= days <= high:
        raise ValueError(f'回看窗口应为 {low}–{high} 个交易日')
    return days


class ServerApp:
    def __init__(self, store: lt.LimitDataStore | None = None, default_date=None, default_days=15):
        self.store = store if store is not None else lt.LimitDataStore()
        self._lock = threading.Lock()
        self._cache: dict[tuple[str | None, int], tuple[float, dict]] = {}
        self.default_date = default_date
        self.default_days = default_days

    def get_dashboard(self, date=None, days=15, refresh=False):
        key = (date, days)
        with self._lock:
            now = time.monotonic()
            hit = self._cache.get(key)
            if not refresh and hit and now - hit[0] < 180:
                return hit[1]
            if refresh:
                self._cache.clear()
            payload = lt.build_dashboard_payload(self.store, target_date=date, lookback_days=days, refresh=refresh)
            payload['meta']['offline'] = getattr(self.store, 'offline', False)
            if len(self._cache) >= 24:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic(), payload)
            return payload

    def get_stock_history(self, code, date=None, days=30):
        with self._lock:
            return lt.build_stock_history(self.store, ts_code=code, end_date=date, days=days)

    def render_html(self, snapshot=False):
        template = TEMPLATE_PATH.read_text(encoding='utf-8')
        if template.count(PLACEHOLDER) != 1:
            raise RuntimeError('看板模板缺少唯一数据占位符')
        template = template.replace('<body>', f'<body data-date="{self.default_date or ""}" data-days="{self.default_days}">', 1)
        if snapshot:
            template = template.replace('/*__LIMIT_SNAPSHOT__*/false', 'true', 1)
            template = template.replace(PLACEHOLDER, to_json(self.get_dashboard(self.default_date, self.default_days)), 1)
        return template


def make_handler(app: ServerApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug('%s', fmt % args)

        def send_content(self, text, kind, status):
            body = text.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', f'{kind}; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def send_json(self, data, status=HTTPStatus.OK):
            self.send_content(to_json(data), 'application/json', status)

        def do_GET(self):
            port = self.server.server_port
            if self.headers.get('Host') not in {f'localhost:{port}', f'127.0.0.1:{port}'}:
                self.send_json({'error': '本服务仅接受本机地址'}, HTTPStatus.FORBIDDEN)
                return
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.send_json({'error': '请直接打开本机看板'}, HTTPStatus.FORBIDDEN)
                return
            try:
                url = urlsplit(self.path)
                if url.path in ('/', '/index.html', '/limit.html'):
                    self.send_content(app.render_html(), 'text/html', HTTPStatus.OK)
                    return
                if url.path == '/api/health':
                    self.send_json({'status': 'ok'})
                    return
                if url.path not in ('/api/dashboard', '/api/stock'):
                    self.send_json({'error': '路径不存在'}, HTTPStatus.NOT_FOUND)
                    return
                params = parse_qs(url.query, max_num_fields=8)
                param = lambda key, default=None: params.get(key, [default])[-1]
                target = parse_date(param('date'))
                if url.path == '/api/dashboard':
                    days = parse_days(param('days', app.default_days), 5, 40)
                    refresh = param('refresh') == '1'
                    self.send_json(app.get_dashboard(target, days, refresh))
                else:
                    code = param('code', '').upper()
                    if not re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', code):
                        raise ValueError('股票代码无效（示例：600000.SH）')
                    self.send_json(app.get_stock_history(code, target, parse_days(param('days', 30), 5, 60)))
            except ValueError as exc:
                self.send_json({'error': str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                # 上游异常文案可能含凭据；不把原文放进响应或日志。
                log.warning('数据请求失败：%s', type(exc).__name__)
                self.send_json({'error': '暂无可用数据。请检查 Tushare 凭据、接口权限或本地缓存'}, HTTPStatus.SERVICE_UNAVAILABLE)

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--date', help='初始交易日 YYYYMMDD')
    parser.add_argument('--days', type=int, default=15, help='默认轮动窗口（5–40 个交易日）')
    parser.add_argument('--offline', action='store_true', help='仅读已有本地行情缓存，不请求接口')
    parser.add_argument('--export', type=Path, help='导出指定日期的离线 HTML 快照')
    args = parser.parse_args(argv)
    try:
        target = parse_date(args.date)
        days = parse_days(args.days, 5, 40)
        if not 1 <= args.port <= 65535:
            raise ValueError('端口须在 1–65535 范围内')
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    app = ServerApp(lt.LimitDataStore(offline=args.offline), target, days)
    if args.export:
        try:
            html = app.render_html(snapshot=True)
        except Exception as exc:
            log.error('快照导出失败：%s。请检查 Tushare 凭据、接口权限或缓存。', type(exc).__name__)
            return 1
        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(html, encoding='utf-8')
        log.info('已导出 %s', args.export.resolve())
        return 0
    with ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app)) as server:
        log.info('涨停地图已启动：http://127.0.0.1:%d', args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
