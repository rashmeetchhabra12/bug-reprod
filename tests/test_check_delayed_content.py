"""Delayed-content regression tests for the expectation checker.

A container can appear before its content. The checker must wait for the
expected content within the timeout, while still distinguishing a wrong value
(failed) from a missing container (error).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bugrepro.browser import BrowserSession
from bugrepro.check import evaluate_expectation
from bugrepro.expectations import Expectation
from bugrepro.recipe import parse_action


class _App(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/delayed-heading":
            body = """<!doctype html><html><body>
              <h1 id="title"></h1>
              <script>setTimeout(() => {
                document.getElementById('title').textContent = 'Profile for Bob';
              }, 600);</script>
            </body></html>"""
        elif self.path == "/delayed-region":
            body = """<!doctype html><html><body>
              <section role="region" aria-label="Tasks">
                <h2>Tasks</h2><ul id="tasks"></ul>
              </section>
              <script>setTimeout(() => {
                const item = document.createElement('li');
                item.textContent = 'Prepare hackathon demo';
                document.getElementById('tasks').appendChild(item);
              }, 600);</script>
            </body></html>"""
        elif self.path == "/wrong-heading":
            body = "<!doctype html><html><body><h1>Profile for Alice</h1></body></html>"
        elif self.path == "/no-heading":
            body = "<!doctype html><html><body><p>No heading here.</p></body></html>"
        elif self.path == "/no-region":
            body = "<!doctype html><html><body><h1>Task list</h1></body></html>"
        else:
            self.send_response(404)
            self.end_headers()
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:
        pass


@pytest.fixture(scope="module")
def delayed_app() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _App)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _goto(session: BrowserSession, path: str) -> None:
    session.perform(parse_action({"type": "goto", "path": path}))


def test_heading_content_arriving_after_600ms_passes_with_2000ms_timeout(
    delayed_app: str,
) -> None:
    expectation = Expectation(kind="heading_text", text="Profile for Bob", level=1)
    with BrowserSession(base_url=delayed_app, timeout_ms=3000) as session:
        _goto(session, "/delayed-heading")
        result = evaluate_expectation(session.page, expectation, 2000)

    assert result.status == "passed"
    assert result.observed == ["Profile for Bob"]


def test_region_content_arriving_after_600ms_passes_with_2000ms_timeout(
    delayed_app: str,
) -> None:
    expectation = Expectation(
        kind="region_text_exact",
        text="Prepare hackathon demo",
        region="Tasks",
    )
    with BrowserSession(base_url=delayed_app, timeout_ms=3000) as session:
        _goto(session, "/delayed-region")
        result = evaluate_expectation(session.page, expectation, 2000)

    assert result.status == "passed"
    assert result.observed == ["Prepare hackathon demo"]


def test_wrong_existing_content_is_failed_not_error(delayed_app: str) -> None:
    expectation = Expectation(kind="heading_text", text="Profile for Bob", level=1)
    with BrowserSession(base_url=delayed_app, timeout_ms=3000) as session:
        _goto(session, "/wrong-heading")
        result = evaluate_expectation(session.page, expectation, 500)

    assert result.status == "failed"
    assert result.observed == ["Profile for Alice"]


def test_missing_container_remains_an_error(delayed_app: str) -> None:
    heading = Expectation(kind="heading_text", text="Profile for Bob", level=1)
    region = Expectation(
        kind="region_text_exact", text="Prepare hackathon demo", region="Tasks"
    )
    with BrowserSession(base_url=delayed_app, timeout_ms=3000) as session:
        _goto(session, "/no-heading")
        heading_result = evaluate_expectation(session.page, heading, 300)
        _goto(session, "/no-region")
        region_result = evaluate_expectation(session.page, region, 300)

    assert heading_result.status == "error"
    assert "No level-1 heading" in heading_result.reason
    assert region_result.status == "error"
    assert 'No region named "Tasks"' in region_result.reason