from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

import limit_server as server


def test_snapshot_carries_escaped_embedded_data(monkeypatch):
    app = server.ServerApp(store=object(), default_date='20261126', default_days=12)
    monkeypatch.setattr(app, 'get_dashboard', lambda date, days: {'name': '</script>', 'days': days})
    web = app.render_html()
    assert server.PLACEHOLDER in web and 'data-date="20261126"' in web
    snapshot = app.render_html(snapshot=True)
    assert server.PLACEHOLDER not in snapshot and '<\\/script>' in snapshot
    assert '||true;' in snapshot and '"days":12' in snapshot


@pytest.fixture
def api(monkeypatch):
    app = server.ServerApp(store=object())
    monkeypatch.setattr(app, 'get_dashboard', lambda date, days, refresh: dict(date=date, days=days, refresh=refresh))
    with ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler(app)) as http:
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            yield http.server_port
        finally:
            http.shutdown()
            thread.join()


def request(port, path, headers=None):
    c = HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        c.request('GET', path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read().decode()
    finally:
        c.close()


def test_routes_and_input_limits(api):
    assert request(api, '/')[0] == 200
    assert request(api, '/api/dashboard?date=2026-11-26&days=20&refresh=1') == (200, '{"date":"20261126","days":20,"refresh":true}')
    assert request(api, '/api/dashboard?date=20261131')[0] == 400
    assert request(api, '/api/dashboard?days=5000')[0] == 400
    assert request(api, '/api/stock?code=../../file')[0] == 400
    assert request(api, '/api/dashboard', {'Host': f'other.test:{api}'})[0] == 403
    assert request(api, '/api/dashboard', {'Sec-Fetch-Site': 'cross-site'})[0] == 403
    assert request(api, '/unknown')[0] == 404
