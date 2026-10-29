"""Scripted mechanics: CDP blocks all external traffic under both policies."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error as PlaywrightError

from bugrepro.browser import BlockedTargetError, BrowserSession
from bugrepro.model_driver import investigate
from bugrepro.recipe import parse_action, parse_recipe, load_recipe
from bugrepro.runner import execute_recipe
from bugrepro.html_report import load_report_run, render_report_html, compare_fingerprints


@pytest.fixture(scope="module")
def resource_app():
    hits = []

    class External(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"external")
        def log_message(self, *args): pass

    external = ThreadingHTTPServer(("0.0.0.0", 0), External)
    destination = f"http://127.0.0.2:{external.server_port}"

    class App(BaseHTTPRequestHandler):
        def do_GET(self):
            body = "<h1>Ready</h1>"
            if self.path == "/image":
                body += f'<img src="{destination}/image.gif?secret=not-recorded" alt="External image">'
            elif self.path == "/script":
                body += f'<script src="{destination}/script.gif"></script>'
            elif self.path == "/frame":
                body += f'<iframe src="{destination}/frame"></iframe>'
            elif self.path == "/navigate":
                body += f'<a href="{destination}/navigation.gif">Leave</a>'
            elif self.path == "/image-redirect":
                body += '<img src="/redirect-image" alt="Redirect image">'
            elif self.path == "/redirect-image":
                self.send_response(302)
                self.send_header("Location", destination + "/redirected.gif")
                self.end_headers()
                return
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        def log_message(self, *args): pass

    app = ThreadingHTTPServer(("127.0.0.1", 0), App)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (external, app)]
    for thread in threads: thread.start()
    try:
        yield {"url":f"http://127.0.0.1:{app.server_port}","hits":hits}
    finally:
        for server in (app, external): server.shutdown(); server.server_close()
        for thread in threads: thread.join(timeout=5)


def perform(session, action):
    return session.perform(parse_action(action))


@pytest.mark.parametrize("policy", ["strict", "block-external-images"])
def test_external_image_is_aborted_before_network(resource_app, tmp_path, policy):
    resource_app["hits"].clear()
    with BrowserSession(base_url=resource_app["url"], resource_policy=policy, timeout_ms=1000) as session:
        error = None
        try:
            perform(session, {"type":"goto","path":"/image"})
            session.screenshot(tmp_path / "evidence.png")
            perform(session, {"type":"observe","label":"loaded"})
        except BlockedTargetError as exc:
            error = exc
        assert session.blocked_resources
        image = session.blocked_resources[0]
        assert image["resource_type"] == "Image"
        assert image["disposition"] == "blocked"
        assert image["fatal"] == (policy == "strict")
        assert "secret=" not in json.dumps(session.network_record())
        if policy == "strict":
            assert isinstance(error, BlockedTargetError)
        else:
            assert error is None
            assert perform(session, {"type":"reload"}).headings == ("Ready",)
    time.sleep(0.1)
    assert resource_app["hits"] == []


@pytest.mark.parametrize("path", ["/script", "/frame", "/navigate"])
def test_non_image_external_resources_remain_fatal(resource_app, path):
    resource_app["hits"].clear()
    with BrowserSession(base_url=resource_app["url"], resource_policy="block-external-images", timeout_ms=1000) as session:
        with pytest.raises((PlaywrightError, BlockedTargetError)):
            perform(session, {"type":"goto","path":path})
            if path == "/navigate":
                perform(session, {"type":"click","locator":{"role":"link","name":"Leave"}})
            else:
                perform(session, {"type":"observe","label":"loaded"})
        assert any(item["fatal"] for item in session.blocked_resources)
        if path == "/navigate":
            assert any(item["resource_type"] == "Document" and item["is_main_document"] for item in session.blocked_resources)
    time.sleep(0.1)
    assert resource_app["hits"] == []


def test_redirected_external_image_still_sends_no_request(resource_app, tmp_path):
    resource_app["hits"].clear()
    with BrowserSession(base_url=resource_app["url"], resource_policy="block-external-images", timeout_ms=1000) as session:
        perform(session, {"type":"goto","path":"/image-redirect"})
        session.screenshot(tmp_path / "evidence.png")
        perform(session, {"type":"observe","label":"loaded"})
        assert any(item["resource_type"] == "Image" and not item["fatal"] for item in session.blocked_resources)
    assert resource_app["hits"] == []


def image_recipe(url, path="/image"):
    return parse_recipe({"schema_version":1,"name":"resource-policy","recorded_base_url":url,
        "expectation":{"kind":"heading_text","text":"Ready","level":1},
        "actions":[{"type":"goto","path":path},{"type":"reload"}]})


@pytest.mark.parametrize("policy,status", [("strict","error"),("block-external-images","passed")])
def test_scripted_records_and_reports(resource_app, tmp_path, policy, status):
    resource_app["hits"].clear()
    result = execute_recipe(recipe=image_recipe(resource_app["url"]),base_url=resource_app["url"],
        run_root=tmp_path,resource_policy=policy,timeout_ms=1000)
    record = json.loads(result.run_json_path.read_text())
    assert record["check"]["status"] == status
    assert record["resource_policy"] == policy
    assert record["blocked_resources"][0]["resource_type"] == "Image"
    assert "Restricted environment" in result.report_path.read_text()
    html = render_report_html({"attempt":load_report_run(result.run_dir,"attempt")})
    assert policy in html and "image.gif" in html
    assert "under this restricted environment" in html
    assert resource_app["hits"] == []


def test_image_policy_does_not_hide_unrelated_action_failure(resource_app, tmp_path):
    recipe = image_recipe(resource_app["url"])
    raw = recipe.to_dict()
    raw["actions"].append({"type":"click","locator":{"role":"button","name":"Missing"}})
    result = execute_recipe(recipe=parse_recipe(raw),base_url=resource_app["url"],run_root=tmp_path,
        resource_policy="block-external-images",timeout_ms=500)
    assert result.check.status == "error"
    assert len(result.action_failures) == 1
    assert "Missing" in result.action_failures[0]["error"]


def test_guard_abort_failure_is_always_fatal():
    session = BrowserSession(base_url="http://127.0.0.1:8101",resource_policy="block-external-images")
    session._cdp = Mock()
    session._cdp.send.side_effect = RuntimeError("interception failed")
    session._guard_request({"requestId":"id","request":{"url":"https://example.test/image.gif"},"resourceType":"Image"})
    assert session.blocked_resources[0]["fatal"]
    with pytest.raises(BlockedTargetError): session.check_network_policy()


def test_image_policy_does_not_hide_browser_failure(resource_app, tmp_path):
    with BrowserSession(base_url=resource_app["url"],resource_policy="block-external-images",timeout_ms=1000) as session:
        perform(session,{"type":"goto","path":"/image"})
        session.screenshot(tmp_path/"evidence.png")
        session.page.close()  # deliberate browser failure, not a supported agent action
        with pytest.raises(PlaywrightError): perform(session,{"type":"reload"})


def test_mocked_investigation_export_and_replay(resource_app, tmp_path, monkeypatch):
    # Prewritten decisions through a fake provider; explicitly scripted mechanics.
    class ScriptedClient:
        model = "mock"
        model_source = "session-reported"
        def __init__(self): self.decisions = iter([{"action":{"type":"goto","path":"/image"}}, {"action":{"type":"reload"}}, {"finish":True}])
        def reject_secret(self,value): pass
        def choose(self,context): return next(self.decisions)
    result = investigate(target=resource_app["url"],bug_report="Load and reload",expectation={"kind":"heading_text","text":"Ready"},
        model_client=ScriptedClient(),resource_policy="block-external-images",run_root=tmp_path,timeout_ms=1000,driver="scripted-test")
    assert result["check"]["status"] == "passed"
    assert result["blocked_resources"] and result["resource_policy"] == "block-external-images"
    recipe = load_recipe(Path(result["run_dir"])/"recipe.json")
    replay = execute_recipe(recipe=recipe,base_url=resource_app["url"],run_root=tmp_path,
        invocation="replay",resource_policy="block-external-images",timeout_ms=1000)
    assert replay.check.status == "passed"


def test_invalid_resource_policy_is_rejected():
    with pytest.raises(ValueError,match="resource_policy"):
        BrowserSession(base_url="http://127.0.0.1:8101",resource_policy="ignore-all")


def test_report_escapes_resources_and_compares_policy(resource_app, tmp_path):
    result = execute_recipe(recipe=image_recipe(resource_app["url"]),base_url=resource_app["url"],run_root=tmp_path,
        resource_policy="block-external-images",timeout_ms=1000)
    attempt = load_report_run(result.run_dir,"attempt")
    replay = load_report_run(result.run_dir,"fixed")
    replay.data = dict(replay.data,driver="recorded-replay",resource_policy="strict")
    attempt.data['driver'] = 'model-agent'
    attempt.data['blocked_resources'] = [{'url':'https://example.test/<script>alert(1)</script>', 'resource_type':'<img onerror=x>', 'fatal':False}]
    unchanged, reasons = compare_fingerprints([attempt,replay])
    assert not unchanged and any('Resource policies differ' in reason for reason in reasons)
    html = render_report_html({'attempt':attempt,'fixed':replay})
    assert '<script>alert' not in html and '&lt;script&gt;' in html
    assert 'Resource policy: strict' in html and 'Resource policy: block-external-images' in html
