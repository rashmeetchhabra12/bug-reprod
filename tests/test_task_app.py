"""Scripted mechanics checks for the Phase 3 task-list fixture.

These tests are explicitly scripted: the action sequence is written in advance
and no model chooses anything. They validate the fixture, the ``reload``
action and the ``region_text_exact`` expectation. The live model-driven attempt
is documented separately in docs/PROGRESS.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bugrepro.expectations import (
    ExpectationError,
    parse_expectation,
)
from bugrepro.recipe import RecipeError, parse_action, parse_recipe
from bugrepro.runner import execute_recipe

from conftest import TASK_EXPECTATION, TASK_TITLE

# Hand-written sequence used only by these scripted tests.
TASK_SEQUENCE = [
    {"type": "goto", "path": "/"},
    {
        "type": "fill",
        "locator": {"role": "textbox", "name": "Task title"},
        "text": TASK_TITLE,
    },
    {"type": "click", "locator": {"role": "button", "name": "Add task"}},
    {"type": "reload"},
]


def _task_recipe() -> dict:
    return {
        "schema_version": 1,
        "name": "task-add-reload",
        "recorded_base_url": "http://127.0.0.1:8103",
        "expectation": TASK_EXPECTATION,
        "actions": TASK_SEQUENCE,
    }


def test_reload_action_is_validated() -> None:
    action = parse_action({"type": "reload"})
    assert action.type == "reload"
    assert action.to_dict() == {"type": "reload"}

    with pytest.raises(RecipeError):
        parse_action({"type": "reload", "path": "/"})
    with pytest.raises(RecipeError):
        parse_action({"type": "reload", "url": "http://127.0.0.1:8103/"})
    with pytest.raises(RecipeError):
        parse_action({"type": "reload", "locator": {"role": "button", "name": "x"}})


def test_region_expectation_validation_and_fingerprint() -> None:
    expectation = parse_expectation(TASK_EXPECTATION)
    assert expectation.kind == "region_text_exact"
    assert expectation.region == "Tasks"
    assert expectation.fingerprint().startswith("sha256:")
    # Round trip is stable.
    assert parse_expectation(expectation.to_dict()) == expectation

    with pytest.raises(ExpectationError):
        parse_expectation({"kind": "region_text_exact", "text": TASK_TITLE})
    with pytest.raises(ExpectationError):
        parse_expectation(
            {"kind": "heading_text", "text": "x", "region": "Tasks"}
        )
    with pytest.raises(ExpectationError):
        parse_expectation(
            {"kind": "region_text_exact", "text": "", "region": "Tasks"}
        )
    with pytest.raises(ExpectationError):
        parse_expectation(
            {"kind": "region_text_exact", "text": "x", "region": "Tasks", "css": "li"}
        )


def test_heading_fingerprint_is_unchanged_by_phase_3() -> None:
    """Phase 1/2 expectation fingerprints must remain stable."""
    expectation = parse_expectation(
        {"kind": "heading_text", "text": "Profile for Bob", "level": 1}
    )
    assert expectation.to_dict() == {
        "kind": "heading_text",
        "text": "Profile for Bob",
        "level": 1,
    }
    assert expectation.fingerprint() == (
        "sha256:0629a38ab49d09b2fb646d3d782aae9a7057451a268233b4f7f260bf2c949dab"
    )


def test_identical_recipe_fails_on_buggy_and_passes_on_fixed(
    task_apps: dict[str, str], tmp_path: Path
) -> None:
    recipe = parse_recipe(_task_recipe())
    run_root = tmp_path / "runs"

    buggy = execute_recipe(
        recipe=recipe,
        base_url=task_apps["buggy"],
        run_root=run_root,
        invocation="pytest",
        label="task-buggy",
    )
    fixed = execute_recipe(
        recipe=recipe,
        base_url=task_apps["fixed"],
        run_root=run_root,
        invocation="pytest",
        label="task-fixed",
    )

    assert buggy.action_failures == []
    assert buggy.execution_errors == []
    assert fixed.action_failures == []
    assert fixed.execution_errors == []

    # Buggy mode: the task was visible after adding it, but the reload lost it.
    assert buggy.check.status == "failed"
    assert buggy.check.observed == []
    assert "does not contain the exact task title" in buggy.check.reason

    # Fixed mode: the same recipe and expectation now pass.
    assert fixed.check.status == "passed"
    assert fixed.check.observed == [TASK_TITLE]

    # Unchanged recipe and expectation across both runs.
    assert buggy.recipe_hash == fixed.recipe_hash
    assert buggy.expectation_fingerprint == fixed.expectation_fingerprint
    assert buggy.expectation_fingerprint == recipe.expectation.fingerprint()
    assert (
        (buggy.run_dir / "recipe.json").read_bytes()
        == (fixed.run_dir / "recipe.json").read_bytes()
    )

    # The task is visible before the reload, which is what makes the report
    # "adding appears to work" rather than "adding never works".
    buggy_doc = json.loads(buggy.run_json_path.read_text(encoding="utf-8"))
    before_reload = buggy_doc["actions"][2]["observation"]
    assert before_reload["url"].endswith("/")
    assert TASK_TITLE in before_reload["aria_excerpt"]

    # After the reload the task is gone.
    after_reload = buggy_doc["actions"][3]["observation"]
    assert TASK_TITLE not in after_reload["aria_excerpt"]
    assert "No tasks yet" in after_reload["aria_excerpt"]

    fixed_doc = json.loads(fixed.run_json_path.read_text(encoding="utf-8"))
    assert TASK_TITLE in fixed_doc["actions"][3]["observation"]["aria_excerpt"]


def test_reload_keeps_the_task_only_in_fixed_mode(
    task_apps: dict[str, str], tmp_path: Path
) -> None:
    """A fresh context per run also means task storage cannot leak."""
    recipe = parse_recipe(_task_recipe())
    run_root = tmp_path / "isolation"

    first = execute_recipe(
        recipe=recipe,
        base_url=task_apps["fixed"],
        run_root=run_root,
        invocation="pytest",
        label="fixed-first",
    )
    second = execute_recipe(
        recipe=recipe,
        base_url=task_apps["fixed"],
        run_root=run_root,
        invocation="pytest",
        label="fixed-second",
    )

    # Exactly one task each time: the second run did not inherit the first
    # run's cookie, which is what a leaked context would show as two tasks.
    assert first.check.observed == [TASK_TITLE]
    assert second.check.observed == [TASK_TITLE]


def test_region_expectation_reports_error_when_region_is_missing(
    task_apps: dict[str, str], tmp_path: Path
) -> None:
    raw = _task_recipe()
    raw["expectation"] = {
        "kind": "region_text_exact",
        "text": TASK_TITLE,
        "region": "Not a real region",
    }
    raw["actions"] = [{"type": "goto", "path": "/"}]
    recipe = parse_recipe(raw)

    result = execute_recipe(
        recipe=recipe,
        base_url=task_apps["buggy"],
        run_root=tmp_path / "runs",
        invocation="pytest",
        label="missing-region",
        timeout_ms=1500,
    )

    assert result.check.status == "error"
    assert result.check.status != "failed"
    assert "No region named" in result.check.reason