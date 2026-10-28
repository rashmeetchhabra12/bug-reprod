"""Scripted mechanics tests with a mocked provider; no live model decisions."""

import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bugrepro.cli import main
from bugrepro.expectations import parse_expectation
from bugrepro.model_driver import ModelClient, ModelError, investigate
from bugrepro.recipe import load_recipe
from bugrepro.runner import execute_recipe
from conftest import TASK_EXPECTATION
from test_task_app import TASK_SEQUENCE

KEY = "mock-secret-never-record"


@contextmanager
def endpoint(replies, *, status=200, delay=0, envelope=True, location=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers["Authorization"], body))
            time.sleep(delay)
            reply = replies[min(len(requests) - 1, len(replies) - 1)]
            payload = {"choices": [{"finish_reason": "stop", "message": {
                "content": reply if isinstance(reply, str) else json.dumps(reply)}}]} if envelope else reply
            raw = json.dumps(payload).encode()
            self.send_response(status)
            if location is not None:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.fixture(autouse=True)
def mock_key(monkeypatch):
    monkeypatch.setenv("BUGREPRO_TEST_KEY", KEY)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("key", [None, ""])
def test_keyless_literal_loopback_configuration(monkeypatch, host, scheme, key):
    if key is None:
        monkeypatch.delenv("BUGREPRO_TEST_KEY")
    else:
        monkeypatch.setenv("BUGREPRO_TEST_KEY", key)
    client = ModelClient(f"{scheme}://{host}:11434/v1/chat/completions",
                         "mock", "BUGREPRO_TEST_KEY", 5)
    client.reject_secret({"action": {"type": "goto", "path": "/"}})


@pytest.mark.parametrize("authenticated", [False, True])
def test_loopback_authorization_header(monkeypatch, authenticated):
    if not authenticated:
        monkeypatch.delenv("BUGREPRO_TEST_KEY")
    with endpoint([{"finish": True}]) as (url, requests):
        client = ModelClient(url, "mock", "BUGREPRO_TEST_KEY", 5)
        assert client.choose({}) == {"finish": True}
    assert requests[0][1] == ("Bearer " + KEY if authenticated else None)


@pytest.mark.parametrize("host", ["example.com", "192.168.1.2", "0.0.0.0",
    "127.0.0.1.example.com", "localhost.example.com", "127.1", "2130706433"])
def test_non_loopback_still_requires_key(monkeypatch, host):
    monkeypatch.delenv("BUGREPRO_TEST_KEY")
    with pytest.raises(ModelError, match="No API key configured"):
        ModelClient(f"https://{host}/v1/chat/completions", "mock", "BUGREPRO_TEST_KEY", 5)


def test_authenticated_remote_configuration():
    client = ModelClient("https://example.com/v1/chat/completions", "mock", "BUGREPRO_TEST_KEY", 5)
    with pytest.raises(ModelError, match="Credential appeared"):
        client.reject_secret({"nested": [KEY]})


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("url", [
    "http://example.com/v1/chat/completions",
    "http://user:password@127.0.0.1/v1/chat/completions",
    "http://127.0.0.1/v1/chat/completions?token=x",
    "http://127.0.0.1/v1/chat/completions#fragment",
    "ftp://127.0.0.1/v1/chat/completions",
    "http://127.0.0.1:99999/v1/chat/completions",
    "http://127.0.0.1:invalid/v1/chat/completions",
])
def test_endpoint_validation_still_applies(monkeypatch, authenticated, url):
    if not authenticated:
        monkeypatch.delenv("BUGREPRO_TEST_KEY")
    with pytest.raises(ModelError):
        ModelClient(url, "mock", "BUGREPRO_TEST_KEY", 5)


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_never_forwards_credentials(monkeypatch, authenticated, status):
    if not authenticated:
        monkeypatch.delenv("BUGREPRO_TEST_KEY")
    with endpoint([{"finish": True}]) as (destination, forwarded):
        with endpoint([KEY], status=status, location=destination) as (url, requests):
            with pytest.raises(ModelError, match="unsuccessful HTTP status") as error:
                ModelClient(url, "mock", "BUGREPRO_TEST_KEY", 5).choose({})
    assert len(requests) == 1
    assert forwarded == []
    assert KEY not in str(error.value)


def run_mock(url, target, root, **kwargs):
    return investigate(target=target, bug_report="Add a task and reload it.",
        expectation=TASK_EXPECTATION, model_client=ModelClient(url, "mock-model", "BUGREPRO_TEST_KEY", 5),
        run_root=root, driver="scripted-test", timeout_ms=500, **kwargs)


def assert_secret_absent(root):
    for path in root.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes()


@pytest.mark.parametrize("authenticated", [False, True])
def test_mocked_end_to_end_export_replay(task_apps, tmp_path, monkeypatch, authenticated):
    if not authenticated:
        monkeypatch.delenv("BUGREPRO_TEST_KEY")
    replies = [{"action": action} for action in TASK_SEQUENCE] + [{"finish": True}]
    with endpoint(replies) as (url, requests):
        result = run_mock(url, task_apps["buggy"], tmp_path)
    assert result["check"]["status"] == "failed"
    assert result["driver"] == "scripted-test"
    assert result["export"]["recipe_path"] == "recipe.json"
    assert result["model_calls"]["used"] == 5
    assert Path(result["html_report"]).exists()
    record = json.loads((Path(result["run_dir"]) / "run.json").read_text())
    assert record["expectation"]["fingerprint"] == parse_expectation(TASK_EXPECTATION).fingerprint()
    assert record["model_calls"] == {"used": 5, "limit": 25}
    recipe = load_recipe(Path(result["run_dir"]) / "recipe.json")
    replay = execute_recipe(recipe=recipe, base_url=task_apps["fixed"],
                            run_root=tmp_path, invocation="replay", timeout_ms=500)
    assert replay.check.status == "passed"
    assert len(requests) == 5  # replay made no provider calls
    contexts = [json.loads(req[2]["messages"][1]["content"]) for req in requests]
    assert contexts[0]["latest"]["observation"]["url"] == "about:blank"
    assert contexts[1]["latest"]["observation"]["url"].startswith(task_apps["buggy"])
    assert len(contexts[-1]["executed_actions"]) == 4
    expected_auth = "Bearer " + KEY if authenticated else None
    assert all(req[0] == "/v1/chat/completions" and req[1] == expected_auth for req in requests)
    assert_secret_absent(tmp_path)


@pytest.mark.parametrize("reply", ["not json", [], {"action": {"type": "eval", "code": "1"}},
    {"action": {"type": "reload"}, "expectation": TASK_EXPECTATION},
    {"action": {"type": "goto", "path": "https://example.com"}},
    '{"action":{"type":"fill","locator":{"role":"textbox","name":"x"},"text":"\\u006dock-secret-never-record"}}',
    {"action": {"type": "fill", "locator": {"role": "textbox", "name": "x"}, "text": KEY}}])
def test_malformed_output_is_recorded_error(task_apps, tmp_path, reply):
    with endpoint([{"action": TASK_SEQUENCE[0]}, reply]) as (url, requests):
        result = run_mock(url, task_apps["buggy"], tmp_path)
    assert result["check"]["status"] == "error"
    assert result["execution_error_count"] == 1
    assert not result["export"]["available"]
    assert result["action_count"] == 1
    assert Path(result["report"]).exists()
    assert_secret_absent(tmp_path)


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_provider_error_is_not_a_bug(task_apps, tmp_path, status):
    with endpoint([KEY], status=status) as (url, requests):
        result = run_mock(url, task_apps["buggy"], tmp_path)
    assert result["check"]["status"] == "error"
    assert not result["export"]["available"]
    assert len(requests) == 1
    assert_secret_absent(tmp_path)


@pytest.mark.parametrize("limits", [{"model_call_limit": 1}, {"action_budget": 1}])
def test_limits_are_errors(task_apps, tmp_path, limits):
    with endpoint([{"action": TASK_SEQUENCE[0]}]) as (url, requests):
        result = run_mock(url, task_apps["buggy"], tmp_path, **limits)
    assert result["check"]["status"] == "error"
    assert not result["export"]["available"]
    assert result["action_count"] == 1
    assert len(requests) <= 2
    if "action_budget" in limits:
        assert result["budget"]["exhausted"] is True


def test_failed_browser_action_is_error(task_apps, tmp_path):
    with endpoint([{"action": TASK_SEQUENCE[0]}, {"action": {
        "type": "click", "locator": {"role": "button", "name": "Missing"}}}]) as (url, _):
        result = run_mock(url, task_apps["buggy"], tmp_path)
    assert result["check"]["status"] == "error"
    assert result["action_failure_count"] == 1
    assert not result["export"]["available"]


def test_timeout_and_connection_failure():
    with endpoint([{"finish": True}], delay=0.1) as (url, _):
        with pytest.raises(ModelError, match="timed out"):
            ModelClient(url, "mock", "BUGREPRO_TEST_KEY", 0.02).choose({})
    with pytest.raises(ModelError, match="connection failed"):
        ModelClient(url, "mock", "BUGREPRO_TEST_KEY", 0.1).choose({})


@pytest.mark.parametrize("payload", [{}, {"choices": []},
    {"choices": [{"finish_reason": "length", "message": {"content": '{"finish":true}'}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": '{"finish":true}', "tool_calls": [{}]}}]},
    {"choices": [{"finish_reason": "stop", "message": {"refusal": KEY}}]}])
def test_invalid_provider_envelopes(payload):
    with endpoint([payload], envelope=False) as (url, _):
        with pytest.raises(ModelError) as error:
            ModelClient(url, "mock", "BUGREPRO_TEST_KEY", 5).choose({})
    assert KEY not in str(error.value)


def test_finish_without_actions_is_error(task_apps, tmp_path):
    with endpoint([{"finish": True}]) as (url, _):
        result = run_mock(url, task_apps["fixed"], tmp_path)
    assert result["check"]["status"] == "error"
    assert not result["export"]["available"]


def test_finish_at_exact_limits(task_apps, tmp_path):
    with endpoint([{"action": TASK_SEQUENCE[0]}, {"finish": True}]) as (url, _):
        result = run_mock(url, task_apps["fixed"], tmp_path, action_budget=1, model_call_limit=2)
    assert result["check"]["status"] == "failed"
    assert result["export"]["available"]


@pytest.mark.parametrize("app", ["accounts", "tasks"])
def test_scripted_demo_cli_exit_codes(sample_apps, task_apps, tmp_path, app):
    apps = sample_apps if app == "accounts" else task_apps
    recipe = Path(__file__).resolve().parents[1] / "recipes" / "sample-alice-bob.json"
    if app == "tasks":
        recipe = tmp_path / "tasks.json"
        recipe.write_text(json.dumps({"schema_version": 1, "name": "tasks-demo",
            "recorded_base_url": apps["buggy"], "expectation": TASK_EXPECTATION,
            "actions": TASK_SEQUENCE}), encoding="utf-8")
    cli = str(Path(sys.executable).with_name("bugrepro.exe"))
    buggy = subprocess.run([cli, "run", str(recipe), "--base-url", apps["buggy"],
        "--run-root", str(tmp_path / "runs"), "--timeout-ms", "500"],
        capture_output=True, text=True, timeout=40)
    assert buggy.returncode == 1, buggy.stdout + buggy.stderr
    run_dir = next((tmp_path / "runs").iterdir())
    fixed = subprocess.run([cli, "replay", str(run_dir), "--base-url", apps["fixed"],
        "--run-root", str(tmp_path / "runs"), "--timeout-ms", "500"],
        capture_output=True, text=True, timeout=40)
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    for run in (tmp_path / "runs").iterdir():
        record = json.loads((run / "run.json").read_text())
        assert record["driver"] in {"scripted-test", "recorded-replay"}


def test_missing_key_and_nonfinite_timeout(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BUGREPRO_TEST_KEY")
    args = ["investigate", "--target", "http://127.0.0.1:8101", "--report-file", "missing",
            "--expectation-file", "missing", "--model-endpoint", "https://example.com/v1/chat/completions",
            "--model", "mock", "--api-key-env", "BUGREPRO_TEST_KEY", "--run-root", str(tmp_path)]
    assert main(args) == 2
    assert "No API key configured" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())
    with pytest.raises(ModelError, match="finite"):
        ModelClient("https://example.com", "mock", "BUGREPRO_TEST_KEY", float("nan"))


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("mode,code", [("buggy", 1), ("fixed", 0)])
def test_standalone_cli(task_apps, tmp_path, monkeypatch, mode, code, authenticated):
    if not authenticated:
        monkeypatch.delenv("BUGREPRO_TEST_KEY")
    report = tmp_path / "bug.txt"
    report.write_text("Add task then reload", encoding="utf-8")
    expectation = tmp_path / "expectation.json"
    expectation.write_text(json.dumps(TASK_EXPECTATION), encoding="utf-8")
    with endpoint([{"action": a} for a in TASK_SEQUENCE] + [{"finish": True}]) as (url, _):
        process = subprocess.run([sys.executable, "-m", "bugrepro", "investigate",
            "--target", task_apps[mode], "--report-file", str(report),
            "--expectation-file", str(expectation), "--model-endpoint", url,
            "--model", "mock", "--api-key-env", "BUGREPRO_TEST_KEY",
            "--run-root", str(tmp_path / "runs"), "--timeout-ms", "500"],
            capture_output=True, text=True, timeout=40)
    assert process.returncode == code, process.stdout + process.stderr
    assert KEY not in process.stdout + process.stderr
    # CLI driver describes the model protocol. The responses here are scripted mocks.
    assert json.loads(process.stdout)["driver"] == "model-agent"
