"""Explicitly scripted mechanics check for Phase 1.

This is NOT an AI investigation and the bug was not AI-discovered. The same
hand-written recipe is replayed against both demo builds:

    login Alice -> view profile -> logout -> login Bob -> view profile

The immutable expectation is "Profile for Bob".
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from bugrepro.recipe import load_recipe, parse_recipe
from bugrepro.runner import execute_recipe

RECIPE_PATH = Path(__file__).resolve().parents[1] / "recipes" / "sample-alice-bob.json"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_same_scripted_recipe_fails_on_buggy_and_passes_on_fixed(
    sample_apps: dict[str, str], tmp_path: Path
) -> None:
    recipe = load_recipe(RECIPE_PATH)
    run_root = tmp_path / "runs"

    buggy = execute_recipe(
        recipe=recipe,
        base_url=sample_apps["buggy"],
        run_root=run_root,
        invocation="pytest",
        label="buggy-app",
    )
    fixed = execute_recipe(
        recipe=recipe,
        base_url=sample_apps["fixed"],
        run_root=run_root,
        invocation="pytest",
        label="fixed-app",
    )

    # No browser problem on either side: the difference is the app bug.
    assert buggy.action_failures == []
    assert buggy.execution_errors == []
    assert fixed.action_failures == []
    assert fixed.execution_errors == []

    # Buggy build: expectation evaluated and failed with Alice's profile.
    assert buggy.check.status == "failed"
    assert buggy.check.expected == "Profile for Bob"
    assert buggy.check.observed == ["Profile for Alice"]

    # Fixed build: identical expectation passes.
    assert fixed.check.status == "passed"
    assert fixed.check.observed == ["Profile for Bob"]

    # Same recipe content and same expectation fingerprint in both runs.
    assert buggy.recipe_hash == fixed.recipe_hash
    assert buggy.expectation_fingerprint == fixed.expectation_fingerprint
    assert buggy.expectation_fingerprint == recipe.expectation.fingerprint()
    assert (
        (buggy.run_dir / "recipe.json").read_bytes()
        == (fixed.run_dir / "recipe.json").read_bytes()
    )

    # Separate, non-overwriting run directories with readable reports.
    assert buggy.run_dir != fixed.run_dir
    assert buggy.report_path.is_file()
    assert fixed.report_path.is_file()
    buggy_report = buggy.report_path.read_text(encoding="utf-8")
    assert "EXPECTATION FAILED - bug reproduced" in buggy_report
    assert "Profile for Bob" in buggy_report
    assert "Profile for Alice" in buggy_report

    buggy_record = json.loads(buggy.run_json_path.read_text(encoding="utf-8"))
    assert buggy_record["check"]["status"] == "failed"
    assert len(buggy_record["actions"]) == len(recipe.actions)
    assert all(record["status"] == "ok" for record in buggy_record["actions"])
    assert len(list((buggy.run_dir / "evidence").glob("*.png"))) == len(recipe.actions)


def test_unreachable_target_is_execution_error_not_a_reproduction(
    tmp_path: Path,
) -> None:
    recipe = load_recipe(RECIPE_PATH)
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))

    result = execute_recipe(
        recipe=recipe,
        base_url=f"http://127.0.0.1:{port}",
        run_root=tmp_path / "runs",
        invocation="pytest",
        label="unreachable",
    )

    assert result.check.status == "error"
    assert result.check.status != "failed"
    assert result.action_failures or result.execution_errors
    report = result.report_path.read_text(encoding="utf-8")
    assert "EXECUTION ERROR - inconclusive" in report
    assert "not proof" in report


def test_external_redirect_is_blocked_not_followed(
    sample_apps: dict[str, str], tmp_path: Path
) -> None:
    raw = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
    raw["actions"] = [{"type": "goto", "path": "/redirect-external"}]
    recipe = parse_recipe(raw)

    result = execute_recipe(
        recipe=recipe,
        base_url=sample_apps["buggy"],
        run_root=tmp_path / "runs",
        invocation="pytest",
        label="external-redirect",
    )

    assert result.check.status == "error"
    assert result.check.status != "failed"
    assert result.execution_errors
    assert "Blocked non-loopback" in result.execution_errors[0]["error"]