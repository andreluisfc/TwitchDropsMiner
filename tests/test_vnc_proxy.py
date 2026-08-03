from email.message import Message
from urllib.error import HTTPError

from src.web import app as web_app


class ClosingBody:
    closed = False

    def read(self) -> bytes:
        return b"missing"

    def close(self) -> None:
        self.closed = True


def test_fetch_vnc_http_closes_http_error_body(monkeypatch):
    body = ClosingBody()
    headers = Message()
    headers["content-type"] = "text/plain"
    error = HTTPError("http://example.test/package.json", 404, "Not Found", headers, body)

    def raise_http_error(*args, **kwargs):
        raise error

    monkeypatch.setattr(web_app, "urlopen", raise_http_error)

    status, content, response_headers, media_type = web_app._fetch_vnc_http(
        "GET", "http://example.test/package.json", None
    )

    assert status == 404
    assert content == b"missing"
    assert response_headers["content-type"] == "text/plain"
    assert media_type == "text/plain"
    assert body.closed
