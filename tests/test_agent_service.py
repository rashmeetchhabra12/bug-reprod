"""Scripted tests for the persistent agent service.

These are mechanics tests written in advance. They drive the same service the
Alan agent uses, but no model chooses anything here, and they are deliberately
kept separate from the live agent demonstration in the completion report.

Covered:
- browser state surviving separate CLI invocations,
- isolation between investigations,
- expectation immutability,
- budget and error handling,
- cleanup of the service and its browser,
- replay of a recorded sequence.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from bugrepro.agent_client import AgentClient, AgentClientError
from bugrepro.expectations import parse_expectation
from bugrepro.recipe import load_recipe, parse_action
from bugrepro.runner import execute_recipe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_FILE = PROJECT_ROOT / "examples" / "live-bug-report.txt"
EXPECTATION_FILE = PROJECT_ROOT / "examples" / "live-expectation.json"
EXPECTATION = json.loads(EXPECTATION_FILE.read_text(encoding="utf-8"))
EXPECTED_FINGERPRINT = parse_expectation(EXPECTATION).fingerprint()

# The hand-written reproduction sequence used by the scripted tests only.
LOGIN_SEQUENCE: list[dict[str, Any]] = [
    {"type": "goto", "path": "/"},
    {
        "type": "fill",
        "locator": {"role": "textbox", "name": "Username"},
        "text": "alice",
    },
    {"type": "click", "locator": {"role": "button", "name": "Log in"}},
    {"type": "click", "locator": {"role": "link", "name": "View profile"}},
    {"type": "click", "locator": {"role": "button", "name": "Log out"}},
    {
        "type": "fill",
        "locator": {"role": "textbox", "name": "Username"},
        "text": "bob",
    },
    {"type": "click", "locator": {"role": "button", "name": "Log in"}},
    {"type": "click", "locator": {"role": "link", "name": "View profile"}},
]


def _spawn_service(
    base: Path,
    *,
    idle_timeout: float = 300.0,
    session_idle_timeout: float = 120.0,
) -> tuple[subprocess.Popen[str], Path, Path]:
    """Start the agent service in its own process and wait until it is ready."""
    base.mkdir(parents=True, exist_ok=True)
    state_file = base / "state.json"
    run_root = base / "runs"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bugrepro",
            "agent",
            "serve",
            "--port",
            "0",
            "--state-file",
            str(state_file),
            "--run-root",
            str(run_root),
            "--idle-timeout",
            str(idle_timeout),
            "--session-idle-timeout",
            str(session_idle_timeout),
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 40
    while time.time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(
                f"agent service exited during startup: {stderr}"
            )
        if state_file.exists():
            try:
                AgentClient(state_file).health()
                return process, state_file, run_root
            except AgentClientError:
                pass
        time.sleep(0.15)
    process.terminate()
    raise RuntimeError("agent service did not become ready")


def _stop_service(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _cli(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bugrepro", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _start_payload(
    base_url: str,
    *,
    driver: str = "scripted-test",
    budget: int = 20,
    label: str = "test",
) -> dict[str, Any]:
    return {
        "target": base_url,
        "bug_report": REPORT_FILE.read_text(encoding="utf-8"),
        "expectation": EXPECTATION,
        "driver": driver,
        "model": {"id": "test-harness", "source": "session-reported"},
        "label": label,
        "action_budget": budget,
        "timeout_ms": 5_000,
    }


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):
    base = tmp_path_factory.mktemp("agent-service")
    process, state_file, run_root = _spawn_service(base)
    try:
        yield {
            "process": process,
            "state_file": state_file,
            "run_root": run_root,
            "client": AgentClient(state_file),
        }
    finally:
        _stop_service(process)


def test_browser_state_survives_separate_cli_invocations(
    service, sample_apps: dict[str, str], tmp_path: Path
) -> None:
    """Each CLI call is a separate process; the browser context is not."""
    state_file = service["state_file"]
    token = json.loads(state_file.read_text(encoding="utf-8"))["token"]

    start = _cli(
        "agent",
        "start",
        "--target",
        sample_apps["buggy"],
        "--report-file",
        str(REPORT_FILE),
        "--expectation-file",
        str(EXPECTATION_FILE),
        "--driver",
        "scripted-test",
        "--label",
        "cli-persistence",
        "--state-file",
        str(state_file),
    )
    assert start.returncode == 0, start.stderr
    # The service token must never reach stdout.
    assert token not in start.stdout
    session = json.loads(start.stdout)
    session_id = session["session_id"]
    assert session["expectation"]["fingerprint"] == EXPECTED_FINGERPRINT

    def act(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        action_file = tmp_path / f"{name}.json"
        action_file.write_text(json.dumps(payload), encoding="utf-8")
        result = _cli(
            "agent",
            "act",
            "--session",
            session_id,
            "--action-file",
            str(action_file),
            "--state-file",
            str(state_file),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert token not in result.stdout
        return json.loads(result.stdout)

    act("01-goto", LOGIN_SEQUENCE[0])
    act("02-fill", LOGIN_SEQUENCE[1])
    act("03-login", LOGIN_SEQUENCE[2])

    # A brand new CLI process must see the logged-in state.
    observed = _cli(
        "agent", "observe", "--session", session_id, "--state-file", str(state_file)
    )
    assert observed.returncode == 0, observed.stdout + observed.stderr
    payload = json.loads(observed.stdout)
    assert "Alice" in payload["observation"]["aria_excerpt"]
    assert payload["budget"]["used"] == 3
    assert payload["budget"]["remaining"] == 17

    _cli("agent", "close", "--session", session_id, "--state-file", str(state_file))


def test_investigations_are_isolated(service, sample_apps: dict[str, str]) -> None:
    client: AgentClient = service["client"]
    first = client.start(_start_payload(sample_apps["buggy"], label="iso-a"))
    second = client.start(_start_payload(sample_apps["buggy"], label="iso-b"))

    client.act(first["session_id"], LOGIN_SEQUENCE[0])
    client.act(first["session_id"], LOGIN_SEQUENCE[1])
    client.act(first["session_id"], LOGIN_SEQUENCE[2])
    client.act(second["session_id"], LOGIN_SEQUENCE[0])

    first_view = client.observe(first["session_id"])
    second_view = client.observe(second["session_id"])

    assert "Alice" in first_view["observation"]["aria_excerpt"]
    assert "Alice" not in second_view["observation"]["aria_excerpt"]
    assert "Not signed in" in second_view["observation"]["aria_excerpt"]

    for session in (first, second):
        client.close(session["session_id"])


def test_expectation_is_frozen_and_cannot_be_replaced(
    service, sample_apps: dict[str, str]
) -> None:
    client: AgentClient = service["client"]
    started = client.start(_start_payload(sample_apps["buggy"], label="frozen"))
    session_id = started["session_id"]

    # The action interface must not be able to carry a different expectation.
    with pytest.raises(AgentClientError) as excinfo:
        client.act(
            session_id,
            {
                "type": "goto",
                "path": "/",
                "expectation": {"kind": "heading_text", "text": "Anything"},
            },
        )
    assert excinfo.value.status == 400

    # The budget was not consumed by the rejected action.
    assert client.status(session_id)["budget"]["used"] == 0

    for action in LOGIN_SEQUENCE:
        client.act(session_id, action)

    finished = client.finish(session_id)
    assert finished["expectation"]["fingerprint"] == EXPECTED_FINGERPRINT
    assert finished["expectation"]["text"] == "Profile for Bob"
    assert finished["check"]["status"] == "failed"

    exported = client.export(session_id)
    recipe = load_recipe(exported["recipe_path"])
    assert recipe.expectation.text == "Profile for Bob"
    assert recipe.expectation.fingerprint() == EXPECTED_FINGERPRINT

    run_document = json.loads(
        (Path(finished["run_dir"]) / "run.json").read_text(encoding="utf-8")
    )
    assert run_document["expectation"]["fingerprint"] == EXPECTED_FINGERPRINT
    assert run_document["driver"] == "scripted-test"
    assert run_document["model"]["id"] == "test-harness"
    client.close(session_id)


def test_budget_exhaustion_is_not_a_reproduction(
    service, sample_apps: dict[str, str]
) -> None:
    client: AgentClient = service["client"]
    started = client.start(
        _start_payload(sample_apps["buggy"], budget=2, label="budget")
    )
    session_id = started["session_id"]

    client.act(session_id, LOGIN_SEQUENCE[0])
    client.act(session_id, LOGIN_SEQUENCE[1])

    blocked = client.act(session_id, LOGIN_SEQUENCE[2])
    assert blocked["status"] == "error"
    assert "budget" in blocked["error"].lower()
    assert blocked["budget"]["exhausted"] is True

    finished = client.finish(session_id)
    assert finished["check"]["status"] == "error"
    assert finished["check"]["status"] != "failed"
    assert finished["budget"]["exhausted"] is True

    with pytest.raises(AgentClientError) as excinfo:
        client.export(session_id)
    assert excinfo.value.status == 409

    report = (Path(finished["run_dir"]) / "report.md").read_text(encoding="utf-8")
    assert "EXECUTION ERROR - inconclusive" in report
    client.close(session_id)


def test_action_failure_is_not_a_reproduction(
    service, sample_apps: dict[str, str]
) -> None:
    client: AgentClient = service["client"]
    started = client.start(_start_payload(sample_apps["buggy"], label="action-fail"))
    session_id = started["session_id"]

    client.act(session_id, LOGIN_SEQUENCE[0])
    failed = client.act(
        session_id,
        {"type": "click", "locator": {"role": "button", "name": "No Such Button"}},
    )
    assert failed["status"] == "failed"
    assert failed["error"]

    finished = client.finish(session_id)
    assert finished["check"]["status"] == "error"
    assert finished["action_failure_count"] == 1

    with pytest.raises(AgentClientError) as excinfo:
        client.export(session_id)
    assert excinfo.value.status == 409
    client.close(session_id)


def test_exported_recipe_replays_unchanged(
    service, sample_apps: dict[str, str], tmp_path: Path
) -> None:
    client: AgentClient = service["client"]
    started = client.start(_start_payload(sample_apps["buggy"], label="replay"))
    session_id = started["session_id"]

    for action in LOGIN_SEQUENCE:
        client.act(session_id, action)
    finished = client.finish(session_id)
    assert finished["check"]["status"] == "failed"
    assert finished["check"]["observed"] == ["Profile for Alice"]

    exported = client.export(session_id)
    recipe = load_recipe(exported["recipe_path"])
    # The exported recipe is the normalized form of the executed sequence.
    assert [action.to_dict() for action in recipe.actions] == [
        parse_action(action).to_dict() for action in LOGIN_SEQUENCE
    ]
    assert recipe.expectation.fingerprint() == EXPECTED_FINGERPRINT

    # Replay against the buggy build: same failure, no model calls.
    buggy = execute_recipe(
        recipe=recipe,
        base_url=sample_apps["buggy"],
        run_root=tmp_path / "replay-buggy",
        invocation="replay",
        label="exported-buggy",
    )
    assert buggy.driver == "recorded-replay"
    assert buggy.check.status == "failed"
    assert buggy.check.observed == ["Profile for Alice"]
    assert buggy.expectation_fingerprint == EXPECTED_FINGERPRINT
    assert buggy.recipe_hash == exported["recipe_sha256"]

    # Replay against the fixed build: the identical recipe now passes.
    fixed = execute_recipe(
        recipe=recipe,
        base_url=sample_apps["fixed"],
        run_root=tmp_path / "replay-fixed",
        invocation="replay",
        label="exported-fixed",
    )
    assert fixed.driver == "recorded-replay"
    assert fixed.check.status == "passed"
    assert fixed.check.observed == ["Profile for Bob"]
    assert fixed.expectation_fingerprint == EXPECTED_FINGERPRINT
    assert fixed.recipe_hash == buggy.recipe_hash
    client.close(session_id)


def test_service_requires_a_token_and_exposes_no_code_endpoint(service) -> None:
    state = json.loads(service["state_file"].read_text(encoding="utf-8"))
    url = state["url"].rstrip("/")

    request = urllib.request.Request(
        url + "/sessions",
        data=json.dumps({"target": "http://127.0.0.1:8101"}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 401

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(url + "/exec", timeout=10)
    assert excinfo.value.code == 401


def test_service_stop_removes_state_file_and_browser(tmp_path: Path) -> None:
    process, state_file, _run_root = _spawn_service(tmp_path / "stop")
    client = AgentClient(state_file)
    started = client.start(_start_payload("http://127.0.0.1:8101", label="stop"))
    client.act(started["session_id"], LOGIN_SEQUENCE[0])

    client.shutdown()
    deadline = time.time() + 20
    while state_file.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert not state_file.exists()
    process.wait(timeout=20)
    assert process.returncode is not None


def test_service_stops_itself_when_idle(tmp_path: Path) -> None:
    process, state_file, _run_root = _spawn_service(
        tmp_path / "idle", idle_timeout=2.0, session_idle_timeout=2.0
    )
    deadline = time.time() + 25
    while state_file.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert not state_file.exists()
    process.wait(timeout=20)
    _stop_service(process)
