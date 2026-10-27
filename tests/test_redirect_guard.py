"""Redirect and service-worker enforcement tests.

CDP Fetch must validate every request destination before it reaches the
network. A controlled server on 127.0.0.2 stands in for a rejected destination;
it must receive zero requests for direct redirects and redirect chains.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import Error as PlaywrightError

from bugrepro.browser import BlockedTargetError, BrowserSession
from bugrepro.recipe import parse_action


class _ExternalHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.server.hits.append(self.path)  # type: ignore[attr-defined]
        body = b"external-ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


class _ExternalServer(ThreadingHTTPServer):
    daemon_threads = True
    hits: list[str]


class _AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        external_port = self.server.external_port  # type: ignore[attr-defined]
        if self.path == "/same-origin-login":
            self._redirect("/home")
            return
        if self.path == "/home":
            self._html("<h1>Home</h1>")
            return
        if self.path == "/external":
            self._redirect(f"http://127.0.0.2:{external_port}/capture")
            return
        if self.path == "/chain":
            self._redirect("/hop")
            return
        if self.path == "/hop":
            self._redirect(f"http://127.0.0.2:{external_port}/chain-capture")
            return
        if self.path == "/sw.js":
            body = (
                "self.addEventListener('install', e => self.skipWaiting());"
                "self.addEventListener('activate', e => e.waitUntil(clients.claim()));"
                "self.addEventListener('fetch', e => {"
                "  if (e.request.url.endsWith('/go')) {"
                f"    e.respondWith(fetch('http://127.0.0.2:{external_port}/sw-capture')"
                "      .then(r => r.text()).then(t => new Response(t)));"
                "  }"
                "});"
            ).encode("utf-8")
            self._body(body, "application/javascript")
            return
        if self.path == "/sw-page":
            self._html(
                """<script>
                navigator.serviceWorker.register('/sw.js')
                  .then(() => navigator.serviceWorker.ready)
                  .catch(() => {});
                </script><p>service worker page</p>"""
            )
            return
        if self.path == "/go":
            self._body(b"app-go", "text/plain")
            return
        self.send_response(404)
        self.end_headers()

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _html(self, body: str) -> None:
        self._body(body.encode("utf-8"), "text/html; charset=utf-8")

    def _body(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


class _AppServer(ThreadingHTTPServer):
    daemon_threads = True
    external_port: int


@pytest.fixture(scope="module")
def redirect_app() -> dict:
    external = _ExternalServer(("0.0.0.0", 0), _ExternalHandler)
    external.hits = []
    app = _AppServer(("127.0.0.1", 0), _AppHandler)
    app.external_port = external.server_address[1]

    external_thread = threading.Thread(target=external.serve_forever, daemon=True)
    app_thread = threading.Thread(target=app.serve_forever, daemon=True)
    external_thread.start()
    app_thread.start()
    try:
        yield {
            "base_url": f"http://127.0.0.1:{app.server_address[1]}",
            "external_hits": external.hits,
            "external_port": app.external_port,
        }
    finally:
        app.shutdown()
        external.shutdown()
        app.server_close()
        external.server_close()
        app_thread.join(timeout=5)
        external_thread.join(timeout=5)


def _goto(session: BrowserSession, path: str):
    return session.perform(parse_action({"type": "goto", "path": path}))


def test_same_origin_redirect_is_allowed(redirect_app: dict) -> None:
    redirect_app["external_hits"].clear()
    with BrowserSession(base_url=redirect_app["base_url"], timeout_ms=3000) as session:
        observation = _goto(session, "/same-origin-login")
        assert observation.url.endswith("/home")
        assert observation.headings == ("Home",)
        assert session.blocked_requests == []

    time.sleep(0.2)
    assert redirect_app["external_hits"] == []


def test_external_redirect_is_blocked_before_network(redirect_app: dict) -> None:
    redirect_app["external_hits"].clear()
    with BrowserSession(base_url=redirect_app["base_url"], timeout_ms=3000) as session:
        with pytest.raises((PlaywrightError, BlockedTargetError)):
            _goto(session, "/external")
        assert session.blocked_requests
        assert "127.0.0.2" in session.blocked_requests[0]

    time.sleep(0.25)
    assert redirect_app["external_hits"] == []


def test_redirect_chain_is_blocked_before_external_destination(
    redirect_app: dict,
) -> None:
    redirect_app["external_hits"].clear()
    with BrowserSession(base_url=redirect_app["base_url"], timeout_ms=3000) as session:
        with pytest.raises((PlaywrightError, BlockedTargetError)):
            _goto(session, "/chain")
        assert any("chain-capture" in item for item in session.blocked_requests)

    time.sleep(0.25)
    assert redirect_app["external_hits"] == []


def test_service_workers_are_blocked_and_cannot_bypass_the_guard(
    redirect_app: dict,
) -> None:
    redirect_app["external_hits"].clear()
    with BrowserSession(base_url=redirect_app["base_url"], timeout_ms=3000) as session:
        _goto(session, "/sw-page")
        session.page.wait_for_timeout(500)
        session.page.reload(wait_until="load")
        session.page.wait_for_timeout(300)

        controller = session.page.evaluate(
            "() => navigator.serviceWorker.controller ? 'yes' : 'no'"
        )
        fetched = session.page.evaluate("() => fetch('/go').then(r => r.text())")

        assert controller == "no"
        assert fetched == "app-go"
        assert session.blocked_requests == []

    time.sleep(0.25)
    assert redirect_app["external_hits"] == []