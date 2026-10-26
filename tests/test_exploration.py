"""Phase 4 exploration tests.

Most checks are scripted mechanics tests. The only model-driven exploration is
documented in docs/PROGRESS.md; nothing here claims to be AI-discovered.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from bugrepro.agent_client import AgentClient, AgentClientError
from bugrepro.exploration import (
    ExplorationError,
    parse_exploration_spec,
    parse_scenario,
)
from bugrepro.html_report import (
    load_exploration_report,
    render_exploration_html,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_TITLE = "Prepare hackathon demo"
SECOND_TITLE = "Review submission"

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)


def _spec(
    target: str,
    *,
    requirements: list[dict[str, str]] | None = None,
    allowed_actions: list[str] | None = None,
    allowed_test_data: list[str] | None = None,
    max_scenarios: int = 3,
    max_actions: int = 60,
    excluded: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "target": target,
        "description": "A small task list where a user adds tasks by title.",
        "requirements": requirements
        or [
            {"id": "R1", "text": "An added task remains visible after reloading."},
            {
                "id": "R2",
                "text": "Adding a second distinct task preserves the first task.",
            },
        ],
        "allowed_actions": allowed_actions
        or ["goto", "click", "fill", "reload", "observe", "screenshot"],
        "allowed_test_data": allowed_test_data
        if allowed_test_data is not None
        else [TASK_TITLE, SECOND_TITLE],
        "excluded_interactions": excluded or ["Deleting tasks"],
        "max_scenarios": max_scenarios,
        "max_actions": max_actions,
    }


def _scenario(
    requirement_id: str = "R1",
    *,
    expectation_text: str = TASK_TITLE,
    test_data: list[str] | None = None,
    title: str = "Added task survives reload",
) -> dict[str, Any]:
    return {
        "requirement_id": requirement_id,
        "title": title,
        "description": "Add the allowed title, reload, and check the Tasks region.",
        "expectation": {
            "kind": "region_text_exact",
            "text": expectation_text,
            "region": "Tasks",
        },
        "test_data": test_data if test_data is not None else [TASK_TITLE],
    }


R1_ACTIONS: list[dict[str, Any]] = [
    {"type": "goto", "path": "/"},
    {
        "type": "fill",
        "locator": {"role": "textbox", "name": "Task title"},
        "text": TASK_TITLE,
    },
    {"type": "click", "locator": {"role": "button", "name": "Add task"}},
    {"type": "reload"},
]

R2_ACTIONS: list[dict[str, Any]] = [
    {"type": "goto", "path": "/"},
    {
        "type": "fill",
        "locator": {"role": "textbox", "name": "Task title"},
        "text": TASK_TITLE,
    },
    {"type": "click", "locator": {"role": "button", "name": "Add task"}},
    {
        "type": "fill",
        "locator": {"role": "textbox", "name": "Task title"},
        "text": SECOND_TITLE,
    },
    {"type": "click", "locator": {"role": "button", "name": "Add task"}},
]


def _spawn_service(base: Path) -> tuple[subprocess.Popen[str], Path]:
    base.mkdir(parents=True, exist_ok=True)
    state_file = base / "state.json"
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
            str(base / "runs"),
            "--idle-timeout",
            "600",
            "--session-idle-timeout",
            "300",
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
            raise RuntimeError(f"agent service exited during startup: {stderr}")
        if state_file.exists():
            try:
                AgentClient(state_file).health()
                return process, state_file
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


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):
    base = tmp_path_factory.mktemp("exploration-service")
    process, state_file = _spawn_service(base)
    try:
        yield {"process": process, "state_file": state_file, "client": AgentClient(state_file)}
    finally:
        _stop_service(process)


def _start(
    client: AgentClient,
    target: str,
    *,
    requirements: list[dict[str, str]] | None = None,
    max_scenarios: int = 3,
    max_actions: int = 60,
    allowed_actions: list[str] | None = None,
    label: str = "test-exploration",
) -> dict[str, Any]:
    return client.start_exploration(
        {
            "spec": _spec(
                target,
                requirements=requirements,
                max_scenarios=max_scenarios,
                max_actions=max_actions,
                allowed_actions=allowed_actions,
            ),
            "driver": "scripted-test",
            "model": {"id": "test-harness", "source": "session-reported"},
            "label": label,
        }
    )


def _run(client: AgentClient, session_id: str, actions: list[dict[str, Any]]) -> None:
    for action in actions:
        client.act(session_id, action)


# --------------------------------------------------------------------- unit
def test_spec_validation_rejects_out_of_bounds_input() -> None:
    valid = _spec("http://127.0.0.1:8103")
    assert parse_exploration_spec(valid).max_actions == 60

    with pytest.raises(ExplorationError):
        parse_exploration_spec({**valid, "target": "https://example.com"})
    with pytest.raises(ExplorationError):
        parse_exploration_spec({**valid, "max_scenarios": 4})
    with pytest.raises(ExplorationError):
        parse_exploration_spec({**valid, "max_actions": 61})
    with pytest.raises(ExplorationError):
        parse_exploration_spec({**valid, "allowed_actions": ["goto", "eval"]})
    with pytest.raises(ExplorationError):
        parse_exploration_spec({**valid, "requirements": []})
    with pytest.raises(ExplorationError):
        parse_exploration_spec(
            {
                **valid,
                "requirements": [
                    {"id": "R1", "text": "First"},
                    {"id": "R1", "text": "Duplicate"},
                ],
            }
        )
    with pytest.raises(ExplorationError):
        parse_exploration_spec({**valid, "unknown": True})


def test_scenario_validation_links_requirements_and_test_data() -> None:
    spec = parse_exploration_spec(_spec("http://127.0.0.1:8103"))

    proposal = parse_scenario(_scenario(), spec)
    assert proposal.requirement.id == "R1"
    assert proposal.expectation.text == TASK_TITLE
    assert proposal.fill_texts == frozenset({TASK_TITLE})

    with pytest.raises(ExplorationError):
        parse_scenario({**_scenario(), "requirement_id": "R3"}, spec)
    with pytest.raises(ExplorationError):
        parse_scenario(_scenario(test_data=["Not allowed text"]), spec)
    with pytest.raises(ExplorationError):
        parse_scenario(
            {
                **_scenario(),
                "expectation": {
                    "kind": "region_text_exact",
                    "text": "Not allowed text",
                    "region": "Tasks",
                },
            },
            spec,
        )
    with pytest.raises(ExplorationError):
        parse_scenario({**_scenario(), "expectation": {"kind": "css", "text": "x"}}, spec)


# ---------------------------------------------------------------- integration
def test_requirements_are_frozen_and_spec_constraints_enforced(
    service, task_apps: dict[str, str]
) -> None:
    client: AgentClient = service["client"]
    started = _start(
        client,
        task_apps["buggy"],
        allowed_actions=["goto", "fill", "observe", "screenshot"],
        label="frozen-requirements",
    )
    exploration_id = started["exploration_id"]
    manifest_path = Path(started["run_dir"]) / "exploration.json"

    manifest_before = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_before["requirements"] == [
        {"id": "R1", "text": "An added task remains visible after reloading."},
        {
            "id": "R2",
            "text": "Adding a second distinct task preserves the first task.",
        },
    ]
    assert manifest_before["spec_fingerprint"].startswith("sha256:")

    with pytest.raises(AgentClientError) as excinfo:
        client.start_scenario(exploration_id, _scenario(requirement_id="R3"))
    assert excinfo.value.status == 400

    scenario = client.start_scenario(exploration_id, _scenario())
    session_id = scenario["session_id"]
    assert scenario["expectation"]["fingerprint"] == (
        "sha256:4d8436d2449495fb23541b5972f7e04634efe09a74928c439b1edbd59a3a94d3"
    )

    client.act(session_id, {"type": "goto", "path": "/"})

    with pytest.raises(AgentClientError) as excinfo:
        client.act(session_id, {"type": "reload"})
    assert excinfo.value.status == 400
    assert excinfo.value.payload.get("code") == "action_not_allowed"

    with pytest.raises(AgentClientError) as excinfo:
        client.act(
            session_id,
            {
                "type": "fill",
                "locator": {"role": "textbox", "name": "Task title"},
                "text": "Test data that was not allowed",
            },
        )
    assert excinfo.value.status == 400
    assert excinfo.value.payload.get("code") == "test_data_not_allowed"

    client.close_scenario(exploration_id, scenario["scenario_id"])
    client.finish_exploration(exploration_id)

    manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_after["requirements"] == manifest_before["requirements"]
    assert manifest_after["spec_fingerprint"] == manifest_before["spec_fingerprint"]
    assert (
        manifest_after["scenarios"][0]["expectation"]["fingerprint"]
        == scenario["expectation"]["fingerprint"]
    )
    assert manifest_after["exploration_status"] == "finished"


def test_violation_needs_replay_confirmation(
    service, task_apps: dict[str, str], tmp_path: Path
) -> None:
    client: AgentClient = service["client"]
    started = _start(client, task_apps["buggy"], label="buggy-r1")
    exploration_id = started["exploration_id"]
    scenario = client.start_scenario(exploration_id, _scenario())
    session_id = scenario["session_id"]
    scenario_id = scenario["scenario_id"]

    _run(client, session_id, R1_ACTIONS)

    finished = client.finish_scenario(exploration_id, scenario_id)
    assert finished["check"]["status"] == "failed"
    assert finished["classification"] == "possible_issue"
    assert finished["replay_confirmation"]["status"] == "not_run"

    exported = client.export_scenario(exploration_id, scenario_id)
    assert exported["export"]["sha256"].startswith("sha256:")

    confirmed = client.confirm_scenario(exploration_id, scenario_id)
    assert confirmed["replay_confirmation"]["status"] == "confirmed"
    assert confirmed["classification"] == "violation"
    assert confirmed["replay_confirmation"]["check"]["status"] == "failed"
    assert confirmed["replay_confirmation"]["observed"] == []

    done = client.finish_exploration(exploration_id)
    assert done["classification_counts"]["violation"] == 1
    assert done["requirement_summary"][0]["classification"] == "violation"
    assert done["budget"]["exploration_actions"] == 4
    assert done["budget"]["confirmation_actions"] == 4
    assert done["budget"]["used"] == 8
    assert done["scenarios"][0]["expectation"]["fingerprint"] == (
        confirmed["expectation"]["fingerprint"]
    )

    manifest = json.loads(
        (Path(done["run_dir"]) / "exploration.json").read_text(encoding="utf-8")
    )
    scenario_doc = manifest["scenarios"][0]
    assert scenario_doc["requirement_id"] == "R1"
    assert scenario_doc["requirement_text"] == (
        "An added task remains visible after reloading."
    )
    assert scenario_doc["replay_confirmation"]["status"] == "confirmed"
    assert manifest["requirements"] == [
        {"id": "R1", "text": "An added task remains visible after reloading."},
        {
            "id": "R2",
            "text": "Adding a second distinct task preserves the first task.",
        },
    ]

    report = load_exploration_report(done["run_dir"])
    html = render_exploration_html(report)
    assert "VIOLATION" in html
    assert "REPLAY CONFIRMED" in html
    assert "An added task remains visible after reloading." in html
    assert "data:image/png;base64," in html
    assert "<script" not in html.lower()
    assert report.integrity_warnings == []
    output = tmp_path / "exploration.html"
    output.write_text(html, encoding="utf-8")
    assert output.stat().st_size > 1000


def test_execution_error_is_not_a_product_finding(
    service, task_apps: dict[str, str]
) -> None:
    client: AgentClient = service["client"]
    started = _start(client, task_apps["buggy"], label="action-error")
    exploration_id = started["exploration_id"]
    scenario = client.start_scenario(exploration_id, _scenario())
    session_id = scenario["session_id"]
    scenario_id = scenario["scenario_id"]

    client.act(session_id, {"type": "goto", "path": "/"})
    failed = client.act(
        session_id,
        {
            "type": "click",
            "locator": {"role": "button", "name": "No Such Button"},
        },
    )
    assert failed["status"] == "failed"

    finished = client.finish_scenario(exploration_id, scenario_id)
    assert finished["check"]["status"] == "error"
    assert finished["classification"] == "execution_error"
    assert finished["classification"] != "violation"
    assert finished["action_failure_count"] == 1

    with pytest.raises(AgentClientError) as excinfo:
        client.export_scenario(exploration_id, scenario_id)
    assert excinfo.value.status == 409

    done = client.finish_exploration(exploration_id)
    assert done["classification_counts"]["execution_error"] == 1
    assert done["classification_counts"]["violation"] == 0
    assert done["requirement_summary"][0]["classification"] == "execution_error"


def test_budget_exhaustion_is_not_evaluated(
    service, task_apps: dict[str, str]
) -> None:
    client: AgentClient = service["client"]
    started = _start(
        client,
        task_apps["buggy"],
        max_actions=2,
        max_scenarios=2,
        label="budget",
    )
    exploration_id = started["exploration_id"]
    scenario = client.start_scenario(exploration_id, _scenario())
    session_id = scenario["session_id"]
    scenario_id = scenario["scenario_id"]
    assert scenario["budget"]["limit"] == 2

    client.act(session_id, {"type": "goto", "path": "/"})
    client.act(
        session_id,
        {
            "type": "fill",
            "locator": {"role": "textbox", "name": "Task title"},
            "text": TASK_TITLE,
        },
    )
    blocked = client.act(
        session_id,
        {"type": "click", "locator": {"role": "button", "name": "Add task"}},
    )
    assert blocked["status"] == "error"
    assert "budget" in blocked["error"].lower()

    finished = client.finish_scenario(exploration_id, scenario_id)
    assert finished["check"]["status"] == "error"
    assert finished["classification"] == "not_evaluated"
    assert "budget" in finished["classification_reason"].lower()

    with pytest.raises(AgentClientError) as excinfo:
        client.export_scenario(exploration_id, scenario_id)
    assert excinfo.value.status == 409

    with pytest.raises(AgentClientError) as excinfo:
        client.start_scenario(exploration_id, _scenario())
    assert excinfo.value.status == 409

    done = client.finish_exploration(exploration_id)
    assert done["budget"]["used"] == 2
    assert done["budget"]["remaining"] == 0
    assert done["classification_counts"]["not_evaluated"] == 1


def test_passing_scenario_is_no_violation(
    service, task_apps: dict[str, str]
) -> None:
    client: AgentClient = service["client"]
    started = _start(client, task_apps["fixed"], label="fixed-pass")
    exploration_id = started["exploration_id"]
    scenario = client.start_scenario(exploration_id, _scenario())
    session_id = scenario["session_id"]
    scenario_id = scenario["scenario_id"]

    _run(client, session_id, R1_ACTIONS)
    finished = client.finish_scenario(exploration_id, scenario_id)
    assert finished["check"]["status"] == "passed"
    assert finished["classification"] == "no_violation"

    exported = client.export_scenario(exploration_id, scenario_id)
    assert exported["export"]["sha256"].startswith("sha256:")

    with pytest.raises(AgentClientError) as excinfo:
        client.confirm_scenario(exploration_id, scenario_id)
    assert excinfo.value.status == 409

    done = client.finish_exploration(exploration_id)
    assert done["classification_counts"]["no_violation"] == 1
    assert done["classification_counts"]["violation"] == 0
    assert done["requirement_summary"][0]["classification"] == "no_violation"


# -------------------------------------------------------------------- report
def _write_evidence_run(
    root: Path,
    name: str,
    *,
    driver: str,
    check_status: str,
    expectation_fingerprint: str,
    recipe_fingerprint: str,
    payload: str,
) -> Path:
    run_dir = root / name
    (run_dir / "evidence").mkdir(parents=True)
    (run_dir / "evidence" / "final.png").write_bytes(PNG_BYTES)
    run = {
        "schema_version": 2,
        "run_id": name,
        "invocation": "exploration",
        "driver": driver,
        "model": {"id": "deepseek/deepseek-v4.1-flash", "source": "session-reported"},
        "attempt_status": "finished",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:01:00Z",
        "base_url": "http://127.0.0.1:8103",
        "requirement": {"id": "R1", "text": payload},
        "scenario": {"title": payload, "description": payload, "test_data": [payload]},
        "expectation": {
            "kind": "region_text_exact",
            "text": payload,
            "level": 1,
            "region": "Tasks",
            "fingerprint": expectation_fingerprint,
            "frozen_at": "2026-01-01T00:00:00Z",
        },
        "actions": [
            {
                "index": 1,
                "type": "click",
                "input": {"type": "click", "locator": {"role": "button", "name": "Add task"}},
                "status": "ok",
                "duration_ms": 5,
                "observation": {
                    "url": "http://127.0.0.1:8103/",
                    "title": "Task list",
                    "headings": ["Task list"],
                    "aria_excerpt": payload,
                    "captured_at": "2026-01-01T00:00:30Z",
                },
                "error": None,
                "screenshot": "evidence/final.png",
                "rationale": payload,
            }
        ],
        "action_failures": [],
        "execution_errors": [],
        "budget": {"limit": 20, "used": 1, "remaining": 19, "exhausted": False},
        "check": {
            "status": check_status,
            "expected": payload,
            "observed": [] if check_status == "failed" else [payload],
            "reason": payload,
            "duration_ms": 5,
        },
        "recipe": {
            "name": name,
            "action_count": 1,
            "sha256": recipe_fingerprint,
            "path": "recipe.json",
        },
        "export": {
            "available": True,
            "reason": "",
            "recipe_path": "recipe.json",
            "sha256": recipe_fingerprint,
        },
        "evidence": ["evidence/final.png"],
    }
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    return run_dir


def test_exploration_report_escapes_and_flags_unverified(
    tmp_path: Path,
) -> None:
    payload = '<script>alert("payload")</script>'
    root = tmp_path / "exploration"
    scenario_run = _write_evidence_run(
        root,
        "scenarios/s1",
        driver="alan-agent",
        check_status="failed",
        expectation_fingerprint="sha256:" + "a" * 64,
        recipe_fingerprint="sha256:" + "b" * 64,
        payload=payload,
    )
    confirmation_run = _write_evidence_run(
        root,
        "confirmations/c1",
        driver="recorded-replay",
        check_status="failed",
        expectation_fingerprint="sha256:" + "a" * 64,
        recipe_fingerprint="sha256:" + "b" * 64,
        payload=payload,
    )

    manifest = {
        "schema_version": 1,
        "exploration_id": "explore-test",
        "exploration_status": "finished",
        "target": "http://127.0.0.1:8103",
        "target_override": {"used": False, "spec_target": None, "actual_target": None},
        "description": payload,
        "driver": "alan-agent",
        "model": {"id": "deepseek/deepseek-v4.1-flash", "source": "session-reported"},
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:02:00Z",
        "requirements": [
            {"id": "R1", "text": payload},
            {"id": "R2", "text": "A requirement with no scenario."},
        ],
        "allowed_actions": ["goto", "click"],
        "allowed_test_data": [payload],
        "excluded_interactions": [],
        "max_scenarios": 3,
        "max_actions": 60,
        "spec_fingerprint": "sha256:" + "d" * 64,
        "budget": {
            "limit": 60,
            "used": 2,
            "exploration_actions": 1,
            "confirmation_actions": 1,
            "remaining": 58,
            "scenarios_used": 1,
            "max_scenarios": 3,
        },
        "scenarios": [
            {
                "scenario_id": "S1",
                "requirement_id": "R1",
                "requirement_text": payload,
                "title": payload,
                "description": payload,
                "expectation": {
                    "kind": "region_text_exact",
                    "text": payload,
                    "level": 1,
                    "region": "Tasks",
                    "fingerprint": "sha256:" + "a" * 64,
                    "frozen_at": "2026-01-01T00:00:00Z",
                },
                "test_data": [payload],
                "session_id": "session-test",
                "run_dir": "scenarios/s1",
                "status": "finished",
                "classification": "violation",
                "classification_reason": payload,
                "check": {
                    "status": "failed",
                    "expected": payload,
                    "observed": [],
                    "reason": payload,
                    "duration_ms": 5,
                },
                "action_count": 1,
                "action_failure_count": 0,
                "execution_error_count": 0,
                "budget": {"limit": 20, "used": 1, "remaining": 19},
                "evidence": ["evidence/final.png"],
                "export": {
                    "recipe_path": "scenarios/s1/recipe.json",
                    "sha256": "sha256:" + "b" * 64,
                    "expectation_fingerprint": "sha256:" + "a" * 64,
                    "exported_at": "2026-01-01T00:01:00Z",
                },
                "exportable": True,
                "replay_confirmation": {
                    "status": "confirmed",
                    "reason": payload,
                    "run_dir": "confirmations/c1",
                    "run_id": "c1",
                    "check": {
                        "status": "failed",
                        "expected": payload,
                        "observed": [],
                        "reason": payload,
                        "duration_ms": 5,
                    },
                    "observed": [],
                    "action_count": 1,
                    "recipe_sha256": "sha256:" + "b" * 64,
                    "expectation_fingerprint": "sha256:" + "a" * 64,
                    "completed_at": "2026-01-01T00:02:00Z",
                },
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:01:00Z",
            }
        ],
        "requirement_summary": [
            {
                "id": "R1",
                "text": payload,
                "scenario_ids": ["S1"],
                "classification": "violation",
                "evaluated": True,
            },
            {
                "id": "R2",
                "text": "A requirement with no scenario.",
                "scenario_ids": [],
                "classification": "not_evaluated",
                "evaluated": False,
            },
        ],
        "untested_requirements": [
            {"id": "R2", "text": "A requirement with no scenario."}
        ],
    }
    (root / "exploration.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    report = load_exploration_report(root)
    assert report.integrity_warnings == []
    html = render_exploration_html(report)
    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in html
    assert "VIOLATION" in html
    assert "REPLAY CONFIRMED" in html
    assert "A requirement with no scenario." in html
    assert "Requirements with no scenario" in html
    assert "data:image/png;base64," in html
    assert str(scenario_run) not in html
    assert str(confirmation_run) not in html

    # Tamper with the run fingerprint: the report must flag the mismatch.
    run_path = scenario_run / "run.json"
    tampered = json.loads(run_path.read_text(encoding="utf-8"))
    tampered["expectation"]["fingerprint"] = "sha256:" + "e" * 64
    run_path.write_text(json.dumps(tampered), encoding="utf-8")

    second = load_exploration_report(root)
    assert second.integrity_warnings
    html_second = render_exploration_html(second)
    assert "Evidence integrity warnings" in html_second