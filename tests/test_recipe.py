"""Recipe and expectation validation tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from bugrepro.expectations import ExpectationError, parse_expectation
from bugrepro.recipe import (
    RecipeError,
    load_recipe,
    parse_recipe,
    recipe_fingerprint,
)

RECIPE_PATH = Path(__file__).resolve().parents[1] / "recipes" / "sample-alice-bob.json"


def _valid_recipe() -> dict:
    return json.loads(RECIPE_PATH.read_text(encoding="utf-8"))


def test_shipped_recipe_is_valid_and_stores_expectation_separately() -> None:
    recipe = load_recipe(RECIPE_PATH)
    assert recipe.name == "alice-logout-bob-profile"
    assert recipe.expectation.text == "Profile for Bob"
    assert recipe.expectation.kind == "heading_text"
    assert len(recipe.actions) == 8
    assert recipe.actions[0].type == "goto"
    assert recipe.actions[1].type == "fill"
    assert recipe.actions[2].type == "click"

    document = recipe.to_dict()
    assert document["expectation"]["text"] == "Profile for Bob"
    assert all("expectation" not in action for action in document["actions"])


def test_recipe_round_trip_is_stable() -> None:
    recipe = load_recipe(RECIPE_PATH)
    reparsed = parse_recipe(recipe.to_dict())
    assert reparsed.to_dict() == recipe.to_dict()
    assert recipe_fingerprint(reparsed) == recipe_fingerprint(recipe)


def test_expectation_fingerprint_changes_only_with_expectation() -> None:
    recipe = load_recipe(RECIPE_PATH)
    changed_actions = parse_recipe(
        {**recipe.to_dict(), "actions": [{"type": "observe", "label": "only-view"}]}
    )
    assert changed_actions.expectation.fingerprint() == recipe.expectation.fingerprint()

    changed_expectation = parse_recipe(
        {
            **recipe.to_dict(),
            "expectation": {"kind": "heading_text", "text": "Profile for Alice", "level": 1},
        }
    )
    assert (
        changed_expectation.expectation.fingerprint()
        != recipe.expectation.fingerprint()
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(actions=[{"type": "run_python", "code": "print(1)"}]),
        lambda r: r.update(actions=[{"type": "goto", "path": "//evil.test"}]),
        lambda r: r.update(
            actions=[
                {
                    "type": "click",
                    "locator": {"role": "script", "name": "x"},
                }
            ]
        ),
        lambda r: r.update(
            actions=[
                {
                    "type": "fill",
                    "locator": {"role": "button", "name": "Log in"},
                    "text": "x",
                }
            ]
        ),
        lambda r: r.update(
            actions=[
                {
                    "type": "click",
                    "locator": {"role": "button", "name": "Log in", "css": "#x"},
                }
            ]
        ),
        lambda r: r.update(expectation={"kind": "page_contains", "text": "Bob"}),
        lambda r: r.update(expectation={"kind": "heading_text", "text": ""}),
        lambda r: r.update(actions=[]),
        lambda r: r.update(schema_version=99),
    ],
)
def test_invalid_recipes_are_rejected(mutate) -> None:
    raw = _valid_recipe()
    mutate(raw)
    with pytest.raises(RecipeError):
        parse_recipe(raw)


def test_expectation_rejects_extra_fields() -> None:
    with pytest.raises(ExpectationError):
        parse_expectation(
            {"kind": "heading_text", "text": "Profile for Bob", "ignore_failure": True}
        )


def test_load_recipe_accepts_recipe_inside_run_directory(tmp_path: Path) -> None:
    recipe = load_recipe(RECIPE_PATH)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "recipe.json").write_text(
        json.dumps(recipe.to_dict()), encoding="utf-8"
    )
    assert load_recipe(run_dir / "recipe.json").name == recipe.name


def test_unknown_recipe_fields_are_rejected() -> None:
    raw = copy.deepcopy(_valid_recipe())
    raw["model"] = "some-agent"
    with pytest.raises(RecipeError):
        parse_recipe(raw)