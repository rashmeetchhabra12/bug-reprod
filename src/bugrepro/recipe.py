"""Recipe schema and validation.

A recipe is data, never code. Only the action types below are accepted, and
each action is normalized into a small dataclass before the browser sees it.
There is intentionally no ``eval``, Python snippet or JavaScript field.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .expectations import Expectation, ExpectationError, parse_expectation
from .safety import TargetError, validate_base_url, validate_path

SCHEMA_VERSION = 1
MAX_ACTIONS = 50

# ARIA roles the locator layer is allowed to use. Kept small on purpose.
ROLE_NAMES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "switch",
        "heading",
        "tab",
        "menuitem",
        "option",
    }
)
FILLABLE_ROLES = frozenset({"textbox", "searchbox", "combobox"})
ACTION_TYPES = frozenset(
    {"goto", "click", "fill", "reload", "observe", "screenshot"}
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


class RecipeError(ValueError):
    """Raised when a recipe fails validation."""


@dataclass(frozen=True)
class Locator:
    role: str
    name: str
    exact: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "name": self.name, "exact": self.exact}


@dataclass(frozen=True)
class Action:
    type: str
    path: str | None = None
    locator: Locator | None = None
    text: str | None = None
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.type == "goto":
            return {"type": "goto", "path": self.path}
        if self.type == "reload":
            return {"type": "reload"}
        if self.type == "click":
            assert self.locator is not None
            return {"type": "click", "locator": self.locator.to_dict()}
        if self.type == "fill":
            assert self.locator is not None
            return {
                "type": "fill",
                "locator": self.locator.to_dict(),
                "text": self.text,
            }
        return {"type": self.type, "label": self.label}


@dataclass(frozen=True)
class Recipe:
    name: str
    recorded_base_url: str
    expectation: Expectation
    actions: tuple[Action, ...]
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "recorded_base_url": self.recorded_base_url,
            "expectation": self.expectation.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
        }


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise RecipeError(f"{where} has unknown field(s): {sorted(unknown)}")


def _parse_locator(raw: Any, where: str) -> Locator:
    if not isinstance(raw, dict):
        raise RecipeError(f"{where}.locator must be a JSON object.")
    _reject_unknown(raw, {"role", "name", "exact"}, f"{where}.locator")

    role = raw.get("role")
    if role not in ROLE_NAMES:
        raise RecipeError(f"{where}.locator.role must be one of {sorted(ROLE_NAMES)}.")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RecipeError(f"{where}.locator.name must be a non-empty string.")
    if len(name) > 200 or re.search(r"[\x00\r\n\t]", name):
        raise RecipeError(f"{where}.locator.name is invalid or too long.")

    exact = raw.get("exact", False)
    if not isinstance(exact, bool):
        raise RecipeError(f"{where}.locator.exact must be a boolean.")

    return Locator(role=role, name=name, exact=exact)


def _parse_label(raw: Any, where: str) -> str:
    label = raw.get("label")
    if not isinstance(label, str) or not _LABEL_RE.match(label):
        raise RecipeError(
            f"{where}.label must be 1-40 characters from A-Z, a-z, 0-9, '.', '_' or '-'."
        )
    return label


def _parse_action(raw: Any, index: int) -> Action:
    where = f"actions[{index}]"
    if not isinstance(raw, dict):
        raise RecipeError(f"{where} must be a JSON object.")
    action_type = raw.get("type")
    if action_type not in ACTION_TYPES:
        raise RecipeError(f"{where}.type must be one of {sorted(ACTION_TYPES)}.")

    if action_type == "goto":
        _reject_unknown(raw, {"type", "path"}, where)
        path = raw.get("path")
        try:
            validate_path(path)
        except TargetError as exc:
            raise RecipeError(f"{where}.path: {exc}") from exc
        return Action(type="goto", path=path)

    if action_type == "reload":
        # Reload the current same-origin page; it takes no arguments.
        _reject_unknown(raw, {"type"}, where)
        return Action(type="reload")

    if action_type == "click":
        _reject_unknown(raw, {"type", "locator"}, where)
        return Action(type="click", locator=_parse_locator(raw.get("locator"), where))

    if action_type == "fill":
        _reject_unknown(raw, {"type", "locator", "text"}, where)
        locator = _parse_locator(raw.get("locator"), where)
        if locator.role not in FILLABLE_ROLES:
            raise RecipeError(
                f"{where}.locator.role must be fillable: {sorted(FILLABLE_ROLES)}."
            )
        text = raw.get("text")
        if not isinstance(text, str):
            raise RecipeError(f"{where}.text must be a string.")
        if len(text) > 500 or "\x00" in text:
            raise RecipeError(f"{where}.text is too long or contains a NUL character.")
        return Action(type="fill", locator=locator, text=text)

    # observe / screenshot
    _reject_unknown(raw, {"type", "label"}, where)
    return Action(type=action_type, label=_parse_label(raw, where))


def parse_action(raw: Any) -> Action:
    """Validate one standalone action (used by the agent-facing ``act`` command)."""
    return _parse_action(raw, 0)


def parse_recipe(raw: Any) -> Recipe:
    if not isinstance(raw, dict):
        raise RecipeError("recipe must be a JSON object.")
    _reject_unknown(
        raw,
        {"schema_version", "name", "recorded_base_url", "expectation", "actions"},
        "recipe",
    )

    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise RecipeError(f"recipe.schema_version must be {SCHEMA_VERSION}.")

    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise RecipeError(
            "recipe.name must be 1-80 characters from A-Z, a-z, 0-9, space, '.', '_' or '-'."
        )

    try:
        recorded_base_url = validate_base_url(raw.get("recorded_base_url"))
    except TargetError as exc:
        raise RecipeError(f"recipe.recorded_base_url: {exc}") from exc

    try:
        expectation = parse_expectation(raw.get("expectation"))
    except ExpectationError as exc:
        raise RecipeError(f"recipe.expectation: {exc}") from exc

    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise RecipeError("recipe.actions must be a non-empty JSON array.")
    if len(raw_actions) > MAX_ACTIONS:
        raise RecipeError(f"recipe.actions must contain at most {MAX_ACTIONS} actions.")
    actions = tuple(_parse_action(item, index) for index, item in enumerate(raw_actions))

    return Recipe(
        name=name,
        recorded_base_url=recorded_base_url,
        expectation=expectation,
        actions=actions,
        schema_version=SCHEMA_VERSION,
    )


def load_recipe(path: str | Path) -> Recipe:
    """Load and validate a recipe JSON file."""
    recipe_path = Path(path)
    if not recipe_path.is_file():
        raise RecipeError(f"recipe file not found: {recipe_path}")
    try:
        raw = json.loads(recipe_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RecipeError(f"recipe is not valid JSON: {exc}") from exc
    return parse_recipe(raw)


def recipe_fingerprint(recipe: Recipe) -> str:
    """Digest of the whole recipe, used to prove buggy/fixed runs used the same input."""
    import hashlib

    canonical = json.dumps(
        recipe.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_recipe(path: str | Path, recipe: Recipe) -> None:
    """Write the normalized recipe into a run directory."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(recipe.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )