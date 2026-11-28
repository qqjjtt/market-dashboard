from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

import intraday_server as server


class Boom(server.IntradayApp):
    def get_payload(self, refresh=False):
        raise RuntimeError("secret-token=abc upstream detail")


@pytest.fixture
def port():
    with ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(Boom())) as http:
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            yield http.server_port
        finally:
            http.shutdown()
            thread.join()


def get(port, path, headers=None):
    c = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read().decode()
    finally:
        c.close()


def test_errors_do_not_echo_upstream_details(port):
    status, body = get(port, "/api/intraday")
    assert status == 500 and "secret" not in body and "RuntimeError" not in body
    status, body = get(port, "/../../etc/passwd")
    assert status == 404 and "passwd" not in body


def test_loopback_server_rejects_foreign_host(port):
    assert get(port, "/api/intraday", {"Host": f"evil.test:{port}"})[0] == 403
    assert get(port, "/favicon.ico", {"Host": f"localhost:{port}"})[0] == 204
