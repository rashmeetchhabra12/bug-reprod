"""Requirement-guided exploration.

An exploration is a bounded set of scenarios derived from user-authored
requirements. Each scenario runs in its own investigation (fresh browser
context), freezes a concrete expectation before its first action, and is
recorded with the evidence and a conservative classification.

Design rules:

* Requirements come only from the validated specification. A scenario cannot
  add or rewrite one.
* The concrete expectation is frozen when the scenario starts and cannot be
  changed afterwards.
* The exploration has at most 3 scenarios and 60 browser actions in total;
  each scenario also gets a small per-session budget.
* A failed action, crash or timeout is an execution error, not a product bug.
* A requirement violation is only labelled ``violation`` after its exported
  recipe reproduces the same observation in a fresh context on the same target.
* This module never calls a model. The Alan agent chooses actions through the
  existing service; this code only validates, executes and records.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .browser import PlaywrightHost
from .check import CheckResult
from .expectations import (
    REGION_KIND,
    Expectation,
    ExpectationError,
    parse_expectation,
)
from .investigation import Investigation, InvestigationError
from .recipe import ACTION_TYPES, load_recipe, recipe_fingerprint
from .runner import execute_recipe_with_host
from .safety import TargetError, validate_base_url

SCHEMA_VERSION = 1
MAX_REQUIREMENTS = 10
MAX_SCENARIOS = 3
MAX_ACTIONS = 60
SCENARIO_ACTION_CAP = 20
MAX_REQUIREMENT_TEXT = 500
MAX_DESCRIPTION = 1_000
MAX_TEST_DATA_ITEMS = 20
MAX_EXCLUDED_ITEMS = 10

VIOLATION = "violation"
POSSIBLE_ISSUE = "possible_issue"
NO_VIOLATION = "no_violation"
EXECUTION_ERROR = "execution_error"
NOT_EVALUATED = "not_evaluated"
CLASSIFICATIONS = frozenset(
    {VIOLATION, POSSIBLE_ISSUE, NO_VIOLATION, EXECUTION_ERROR, NOT_EVALUATED}
)

CONFIRMED = "confirmed"
NOT_REPRODUCED = "not_reproduced"
CONFIRMATION_ERROR = "error"
NOT_RUN = "not_run"

_REQUIREMENT_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,15}$")
_CONTROL_RE = re.compile(r"[\x00\r\n\t]")


class ExplorationError(InvestigationError):
    """A validated exploration error; inherits HTTP status and code."""


def _iso(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    allow_newlines: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ExplorationError(f"{field_name} must be a string.")
    text = value.strip()
    if not text:
        raise ExplorationError(f"{field_name} must not be empty.")
    if len(text) > max_length:
        raise ExplorationError(
            f"{field_name} must be at most {max_length} characters."
        )
    if "\x00" in text:
        raise ExplorationError(f"{field_name} must not contain NUL.")
    if not allow_newlines and _CONTROL_RE.search(text):
        raise ExplorationError(f"{field_name} must be a single line.")
    return text


def _string_list(
    value: Any,
    *,
    field_name: str,
    max_items: int,
    max_length: int,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ExplorationError(f"{field_name} must be an array of strings.")
    if not value and not allow_empty:
        raise ExplorationError(f"{field_name} must not be empty.")
    if len(value) > max_items:
        raise ExplorationError(
            f"{field_name} must contain at most {max_items} items."
        )
    result: list[str] = []
    for index, item in enumerate(value):
        text = _clean_text(
            item, field_name=f"{field_name}[{index}]", max_length=max_length
        )
        if text in result:
            raise ExplorationError(f"{field_name} contains a duplicate: {text!r}.")
        result.append(text)
    return tuple(result)


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text}


@dataclass(frozen=True)
class ExplorationSpec:
    target: str
    description: str
    requirements: tuple[Requirement, ...]
    allowed_actions: tuple[str, ...]
    allowed_test_data: tuple[str, ...]
    excluded_interactions: tuple[str, ...]
    max_scenarios: int
    max_actions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "description": self.description,
            "requirements": [item.to_dict() for item in self.requirements],
            "allowed_actions": list(self.allowed_actions),
            "allowed_test_data": list(self.allowed_test_data),
            "excluded_interactions": list(self.excluded_interactions),
            "max_scenarios": self.max_scenarios,
            "max_actions": self.max_actions,
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def with_target(self, target: str) -> "ExplorationSpec":
        return replace(self, target=validate_base_url(target))


def parse_exploration_spec(raw: Any, *, target_override: str | None = None) -> ExplorationSpec:
    """Validate a user-authored exploration specification."""
    if not isinstance(raw, dict):
        raise ExplorationError("exploration specification must be a JSON object.")
    allowed = {
        "target",
        "description",
        "requirements",
        "allowed_actions",
        "allowed_test_data",
        "excluded_interactions",
        "max_scenarios",
        "max_actions",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ExplorationError(
            f"exploration specification has unknown field(s): {sorted(unknown)}."
        )

    try:
        target = validate_base_url(raw.get("target"))
    except TargetError as exc:
        raise ExplorationError(f"target: {exc}") from exc

    description = _clean_text(
        raw.get("description"),
        field_name="description",
        max_length=MAX_DESCRIPTION,
        allow_newlines=True,
    )

    raw_requirements = raw.get("requirements")
    if not isinstance(raw_requirements, list) or not raw_requirements:
        raise ExplorationError("requirements must be a non-empty array.")
    if len(raw_requirements) > MAX_REQUIREMENTS:
        raise ExplorationError(
            f"requirements must contain at most {MAX_REQUIREMENTS} items."
        )
    requirements: list[Requirement] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_requirements):
        if not isinstance(item, dict):
            raise ExplorationError(f"requirements[{index}] must be an object.")
        if set(item) != {"id", "text"}:
            raise ExplorationError(
                f"requirements[{index}] must contain exactly id and text."
            )
        req_id = _clean_text(
            item.get("id"), field_name=f"requirements[{index}].id", max_length=16
        )
        if not _REQUIREMENT_ID_RE.match(req_id):
            raise ExplorationError(
                f"requirements[{index}].id must match {_REQUIREMENT_ID_RE.pattern}."
            )
        if req_id in seen_ids:
            raise ExplorationError(f"duplicate requirement id: {req_id}.")
        seen_ids.add(req_id)
        text = _clean_text(
            item.get("text"),
            field_name=f"requirements[{index}].text",
            max_length=MAX_REQUIREMENT_TEXT,
        )
        requirements.append(Requirement(id=req_id, text=text))

    allowed_actions = _string_list(
        raw.get("allowed_actions"),
        field_name="allowed_actions",
        max_items=len(ACTION_TYPES),
        max_length=20,
        allow_empty=False,
    )
    unknown_actions = set(allowed_actions) - ACTION_TYPES
    if unknown_actions:
        raise ExplorationError(
            f"allowed_actions contains unsupported action(s): {sorted(unknown_actions)}."
        )

    allowed_test_data = _string_list(
        raw.get("allowed_test_data", []),
        field_name="allowed_test_data",
        max_items=MAX_TEST_DATA_ITEMS,
        max_length=200,
    )
    excluded = _string_list(
        raw.get("excluded_interactions", []),
        field_name="excluded_interactions",
        max_items=MAX_EXCLUDED_ITEMS,
        max_length=200,
    )

    max_scenarios = raw.get("max_scenarios")
    if isinstance(max_scenarios, bool) or not isinstance(max_scenarios, int):
        raise ExplorationError("max_scenarios must be an integer.")
    if not (1 <= max_scenarios <= MAX_SCENARIOS):
        raise ExplorationError(f"max_scenarios must be between 1 and {MAX_SCENARIOS}.")

    max_actions = raw.get("max_actions")
    if isinstance(max_actions, bool) or not isinstance(max_actions, int):
        raise ExplorationError("max_actions must be an integer.")
    if not (1 <= max_actions <= MAX_ACTIONS):
        raise ExplorationError(f"max_actions must be between 1 and {MAX_ACTIONS}.")

    if target_override is not None:
        try:
            target = validate_base_url(target_override)
        except TargetError as exc:
            raise ExplorationError(f"target override: {exc}") from exc

    return ExplorationSpec(
        target=target,
        description=description,
        requirements=tuple(requirements),
        allowed_actions=allowed_actions,
        allowed_test_data=allowed_test_data,
        excluded_interactions=excluded,
        max_scenarios=max_scenarios,
        max_actions=max_actions,
    )


@dataclass(frozen=True)
class ScenarioProposal:
    requirement: Requirement
    title: str
    description: str
    expectation: Expectation
    test_data: tuple[str, ...]
    fill_texts: frozenset[str] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement.id,
            "title": self.title,
            "description": self.description,
            "expectation": self.expectation.to_dict(),
            "test_data": list(self.test_data),
        }


def parse_scenario(raw: Any, spec: ExplorationSpec) -> ScenarioProposal:
    """Validate one concrete scenario against the frozen exploration spec."""
    if not isinstance(raw, dict):
        raise ExplorationError("scenario must be a JSON object.")
    allowed = {"requirement_id", "title", "description", "expectation", "test_data"}
    unknown = set(raw) - allowed
    if unknown:
        raise ExplorationError(f"scenario has unknown field(s): {sorted(unknown)}.")

    requirement_id = _clean_text(
        raw.get("requirement_id"),
        field_name="requirement_id",
        max_length=16,
    )
    by_id = {item.id: item for item in spec.requirements}
    requirement = by_id.get(requirement_id)
    if requirement is None:
        raise ExplorationError(
            f"Unknown requirement ID {requirement_id!r}; scenarios cannot add "
            "requirements."
        )

    title = _clean_text(raw.get("title"), field_name="title", max_length=160)
    description = _clean_text(
        raw.get("description"),
        field_name="description",
        max_length=MAX_DESCRIPTION,
        allow_newlines=True,
    )
    try:
        expectation = parse_expectation(raw.get("expectation"))
    except ExpectationError as exc:
        raise ExplorationError(f"expectation: {exc}") from exc

    test_data = _string_list(
        raw.get("test_data", []),
        field_name="test_data",
        max_items=MAX_TEST_DATA_ITEMS,
        max_length=200,
    )
    if spec.allowed_test_data:
        allowed_values = set(spec.allowed_test_data)
        outside = [value for value in test_data if value not in allowed_values]
        if outside:
            raise ExplorationError(
                f"test_data contains value(s) outside the spec allow-list: {outside}."
            )
        allowlist = test_data or spec.allowed_test_data
        if (
            expectation.kind == REGION_KIND
            and expectation.text not in set(allowlist)
        ):
            raise ExplorationError(
                "region_text_exact expectation text must be one of the scenario's "
                "allowed test data values."
            )
        fill_texts: frozenset[str] | None = frozenset(allowlist)
    else:
        if test_data:
            raise ExplorationError(
                "test_data must be empty when the spec has no allowed_test_data."
            )
        fill_texts = None

    return ScenarioProposal(
        requirement=requirement,
        title=title,
        description=description,
        expectation=expectation,
        test_data=test_data,
        fill_texts=fill_texts,
    )


@dataclass
class Scenario:
    scenario_id: str
    proposal: ScenarioProposal
    session_id: str
    investigation: Investigation
    run_dir_rel: str
    started_at: str = field(default_factory=_iso)
    finished_at: str | None = None
    status: str = "running"
    classification: str = NOT_EVALUATED
    classification_reason: str = "Scenario has not finished."
    check: dict[str, Any] | None = None
    export: dict[str, Any] | None = None
    confirmation: dict[str, Any] = field(
        default_factory=lambda: {
            "status": NOT_RUN,
            "reason": "Not replay-confirmed yet.",
            "run_dir": None,
            "run_id": None,
            "check": None,
            "observed": [],
            "action_count": 0,
            "recipe_sha256": None,
            "expectation_fingerprint": None,
            "completed_at": None,
        }
    )


class Exploration:
    """One bounded, requirement-guided exploration on a single loopback target."""

    def __init__(
        self,
        *,
        exploration_id: str,
        spec: ExplorationSpec,
        run_dir: Path,
        driver: str,
        model: dict[str, str],
        target_override: dict[str, Any] | None = None,
    ) -> None:
        self.exploration_id = exploration_id
        self.spec = spec
        self.run_dir = run_dir
        self.driver = driver
        self.model = model
        self.target_override = target_override
        self.status = "active"
        self.started_at = _iso()
        self.finished_at: str | None = None
        self.scenarios: list[Scenario] = []
        self.manifest_path = run_dir / "exploration.json"
        self.summary_path = run_dir / "exploration.md"
        (run_dir / "scenarios").mkdir(parents=True, exist_ok=True)
        (run_dir / "confirmations").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- lifecycle
    @classmethod
    def start(
        cls,
        *,
        exploration_id: str,
        spec: ExplorationSpec,
        run_root: str | Path,
        label: str,
        driver: str,
        model: dict[str, str],
        target_override: dict[str, Any] | None = None,
    ) -> "Exploration":
        from .runner import allocate_run_dir  # local import keeps startup light

        run_dir = allocate_run_dir(
            run_root, label=label or "exploration", invocation="exploration"
        )
        exploration = cls(
            exploration_id=exploration_id,
            spec=spec,
            run_dir=run_dir,
            driver=driver,
            model=model,
            target_override=target_override,
        )
        exploration._write_manifest()
        return exploration

    def budget(self) -> dict[str, Any]:
        exploration_actions = sum(
            scenario.investigation.budget_used for scenario in self.scenarios
        )
        confirmation_actions = sum(
            int(scenario.confirmation.get("action_count") or 0)
            for scenario in self.scenarios
        )
        used = exploration_actions + confirmation_actions
        return {
            "limit": self.spec.max_actions,
            "used": used,
            "exploration_actions": exploration_actions,
            "confirmation_actions": confirmation_actions,
            "remaining": max(0, self.spec.max_actions - used),
            "scenarios_used": len(self.scenarios),
            "max_scenarios": self.spec.max_scenarios,
        }

    def _scenario(self, scenario_id: str) -> Scenario:
        for scenario in self.scenarios:
            if scenario.scenario_id == scenario_id:
                return scenario
        raise ExplorationError(
            f"Unknown scenario: {scenario_id}.",
            code="unknown_scenario",
            http_status=404,
        )

    def _ensure_active(self) -> None:
        if self.status != "active":
            raise ExplorationError(
                f"Exploration is {self.status}.", code="exploration_not_active"
            )

    # --------------------------------------------------------------- scenarios
    def start_scenario(
        self,
        raw_scenario: Any,
        *,
        host: PlaywrightHost,
        session_id: str,
        headless: bool,
        timeout_ms: int,
    ) -> tuple[dict[str, Any], Investigation, list[str]]:
        self._ensure_active()
        if len(self.scenarios) >= self.spec.max_scenarios:
            raise ExplorationError(
                f"This exploration allows at most {self.spec.max_scenarios} scenarios.",
                code="scenario_limit",
            )

        proposal = parse_scenario(raw_scenario, self.spec)

        remaining = self.budget()["remaining"]
        if remaining <= 0:
            raise ExplorationError(
                "The exploration action budget is exhausted.",
                code="budget_exhausted",
                http_status=409,
            )
        scenario_budget = min(SCENARIO_ACTION_CAP, remaining, 50)
        scenario_id = f"S{len(self.scenarios) + 1}"

        closed_session_ids = self._close_existing_investigations(
            "Closed when the next scenario starts."
        )

        label = f"{scenario_id.lower()}-{proposal.requirement.id.lower()}-scenario"
        investigation = Investigation.start(
            session_id=session_id,
            base_url=self.spec.target,
            bug_report=None,
            expectation=proposal.expectation,
            driver=self.driver,
            model=self.model,
            budget_limit=scenario_budget,
            timeout_ms=timeout_ms,
            run_root=self.run_dir / "scenarios",
            label=label,
            headless=headless,
            host=host,
            requirement=proposal.requirement.to_dict(),
            scenario={
                "title": proposal.title,
                "description": proposal.description,
                "test_data": list(proposal.test_data),
            },
            exploration_id=self.exploration_id,
            allowed_actions=frozenset(self.spec.allowed_actions),
            allowed_fill_texts=proposal.fill_texts,
            invocation="exploration",
        )

        scenario = Scenario(
            scenario_id=scenario_id,
            proposal=proposal,
            session_id=session_id,
            investigation=investigation,
            run_dir_rel=str(investigation.run_dir.relative_to(self.run_dir)),
        )
        scenario.classification, scenario.classification_reason = self._classify(
            scenario
        )
        self.scenarios.append(scenario)
        self._write_manifest()

        payload = self._scenario_payload(scenario)
        payload["budget"] = investigation.budget()
        payload["run_dir_absolute"] = str(investigation.run_dir)
        payload["closed_session_ids"] = closed_session_ids
        return payload, investigation, closed_session_ids

    def _close_existing_investigations(self, reason: str) -> list[str]:
        closed: list[str] = []
        for scenario in self.scenarios:
            investigation = scenario.investigation
            if investigation.closed:
                continue
            try:
                investigation.close(reason)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
            if scenario.status == "running":
                scenario.status = "closed"
                scenario.finished_at = _iso()
                scenario.check = (
                    investigation.check.to_dict() if investigation.check else None
                )
                scenario.classification = NOT_EVALUATED
                scenario.classification_reason = (
                    "Scenario closed before it was finished."
                )
            closed.append(scenario.session_id)
        return closed

    def finish_scenario(self, scenario_id: str) -> dict[str, Any]:
        scenario = self._scenario(scenario_id)
        if scenario.status == "closed":
            raise ExplorationError(
                "Scenario is closed; it cannot be finished.",
                code="scenario_closed",
                http_status=409,
            )
        if scenario.investigation.attempt_status == "closed":
            raise ExplorationError(
                "Scenario browser session is closed; it cannot be finished.",
                code="scenario_closed",
                http_status=409,
            )

        try:
            scenario.investigation.finish()
        except InvestigationError:
            # If the session disappeared, keep whatever record exists.
            pass

        if scenario.investigation.attempt_status == "active":
            raise ExplorationError(
                "Scenario did not finish cleanly.", code="scenario_not_finished"
            )

        scenario.status = (
            "finished"
            if scenario.investigation.attempt_status == "finished"
            else scenario.investigation.attempt_status
        )
        scenario.finished_at = scenario.investigation.finished_at or _iso()
        scenario.check = (
            scenario.investigation.check.to_dict()
            if scenario.investigation.check
            else None
        )
        scenario.classification, scenario.classification_reason = self._classify(
            scenario
        )
        self._write_manifest()
        payload = self._scenario_payload(scenario)
        payload["exportable"] = self._exportable(scenario)
        return payload

    def export_scenario(self, scenario_id: str) -> dict[str, Any]:
        scenario = self._scenario(scenario_id)
        if scenario.status != "finished":
            raise ExplorationError(
                "Only a finished scenario can export a recipe.",
                code="scenario_not_finished",
                http_status=409,
            )
        exported = scenario.investigation.export_recipe()
        recipe_path = Path(exported["recipe_path"])
        scenario.export = {
            "recipe_path": str(recipe_path.relative_to(self.run_dir)),
            "sha256": exported["recipe_sha256"],
            "expectation_fingerprint": exported["expectation_fingerprint"],
            "exported_at": _iso(),
        }
        self._write_manifest()
        payload = self._scenario_payload(scenario)
        payload["export"] = scenario.export
        payload["recipe_path_absolute"] = str(recipe_path)
        return payload

    def confirm_scenario(
        self,
        scenario_id: str,
        *,
        host: PlaywrightHost,
        timeout_ms: int,
    ) -> dict[str, Any]:
        scenario = self._scenario(scenario_id)
        if scenario.status != "finished":
            raise ExplorationError(
                "Only a finished scenario can be replay-confirmed.",
                code="scenario_not_finished",
                http_status=409,
            )
        if scenario.check is None or scenario.check.get("status") != "failed":
            raise ExplorationError(
                "Only an observed requirement failure can be replay-confirmed.",
                code="no_violation_to_confirm",
                http_status=409,
            )
        if scenario.export is None:
            raise ExplorationError(
                "Export the scenario recipe before replay confirmation.",
                code="recipe_not_exported",
                http_status=409,
            )

        recipe_path = self.run_dir / scenario.export["recipe_path"]
        try:
            recipe = load_recipe(recipe_path)
        except Exception as exc:  # noqa: BLE001 - explicit confirmation error
            return self._store_confirmation(
                scenario,
                status=CONFIRMATION_ERROR,
                reason=f"Could not load the exported recipe: {exc}",
            )

        if recipe.expectation.fingerprint() != (
            scenario.proposal.expectation.fingerprint()
        ):
            return self._store_confirmation(
                scenario,
                status=CONFIRMATION_ERROR,
                reason=(
                    "The exported recipe expectation does not match the frozen "
                    "scenario expectation."
                ),
            )
        if recipe_fingerprint(recipe) != scenario.export.get("sha256"):
            return self._store_confirmation(
                scenario,
                status=CONFIRMATION_ERROR,
                reason="The exported recipe fingerprint changed.",
            )

        required_actions = len(recipe.actions)
        remaining = self.budget()["remaining"]
        if remaining < required_actions:
            return self._store_confirmation(
                scenario,
                status=CONFIRMATION_ERROR,
                reason=(
                    "Replay confirmation would exceed the exploration action "
                    f"budget ({remaining} remaining, {required_actions} required)."
                ),
                action_count=0,
            )

        try:
            replay = execute_recipe_with_host(
                recipe=recipe,
                base_url=self.spec.target,
                host=host,
                run_root=self.run_dir / "confirmations",
                invocation="exploration-replay",
                driver="recorded-replay",
                label=f"confirm-{scenario_id.lower()}",
                timeout_ms=timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001 - explicit confirmation error
            return self._store_confirmation(
                scenario,
                status=CONFIRMATION_ERROR,
                reason=f"Replay could not run: {type(exc).__name__}: {exc}",
                action_count=0,
            )

        original_observed = list(scenario.check.get("observed") or [])
        replay_observed = list(replay.check.observed)
        action_count = len(replay.actions)

        if replay.action_failures or replay.execution_errors:
            status = CONFIRMATION_ERROR
            reason = (
                "The replay recorded an action failure or execution error; it is "
                "not confirmation."
            )
        elif replay.check.status == "failed" and replay_observed == original_observed:
            status = CONFIRMED
            reason = "The same requirement violation recurred in a fresh context."
        elif replay.check.status == "failed":
            status = NOT_REPRODUCED
            reason = (
                "The replay failed differently: original observed "
                f"{original_observed}, replay observed {replay_observed}."
            )
        else:
            status = NOT_REPRODUCED
            reason = (
                "The replay did not reproduce the violation: replay check "
                f"was {replay.check.status!r}."
            )

        return self._store_confirmation(
            scenario,
            status=status,
            reason=reason,
            run_dir=str(replay.run_dir.relative_to(self.run_dir)),
            run_id=replay.run_id,
            check=replay.check.to_dict(),
            observed=replay_observed,
            action_count=action_count,
            recipe_sha256=replay.recipe_hash,
            expectation_fingerprint=replay.expectation_fingerprint,
        )

    def _store_confirmation(
        self,
        scenario: Scenario,
        *,
        status: str,
        reason: str,
        run_dir: str | None = None,
        run_id: str | None = None,
        check: dict[str, Any] | None = None,
        observed: list[str] | None = None,
        action_count: int = 0,
        recipe_sha256: str | None = None,
        expectation_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        scenario.confirmation = {
            "status": status,
            "reason": reason,
            "run_dir": run_dir,
            "run_id": run_id,
            "check": check,
            "observed": list(observed or []),
            "action_count": action_count,
            "recipe_sha256": recipe_sha256,
            "expectation_fingerprint": expectation_fingerprint,
            "completed_at": _iso(),
        }
        scenario.classification, scenario.classification_reason = self._classify(
            scenario
        )
        self._write_manifest()
        payload = self._scenario_payload(scenario)
        payload["replay_confirmation"] = scenario.confirmation
        return payload

    def close_scenario(self, scenario_id: str, reason: str = "Closed by client.") -> dict[str, Any]:
        scenario = self._scenario(scenario_id)
        investigation = scenario.investigation
        if not investigation.closed:
            investigation.close(reason)
        if scenario.status == "running":
            scenario.status = "closed"
            scenario.finished_at = _iso()
            scenario.check = (
                investigation.check.to_dict() if investigation.check else None
            )
        scenario.classification, scenario.classification_reason = self._classify(
            scenario
        )
        self._write_manifest()
        payload = self._scenario_payload(scenario)
        payload["closed_session_id"] = scenario.session_id
        return payload

    # ------------------------------------------------------------- finalize
    def finish(self) -> dict[str, Any]:
        if self.status == "finished":
            return self.status_payload()
        self._ensure_active()
        self._close_existing_investigations("Exploration finished.")
        self.status = "finished"
        self.finished_at = _iso()
        self._write_manifest()
        return self.status_payload()

    def close(self, reason: str = "Service shutdown.") -> dict[str, Any]:
        if self.status == "active":
            self._close_existing_investigations(reason)
            self.status = "closed"
            self.finished_at = _iso()
            self._write_manifest()
        return self.status_payload()

    # ------------------------------------------------------------- reporting
    def _classify(self, scenario: Scenario) -> tuple[str, str]:
        investigation = scenario.investigation
        if scenario.status == "running":
            return NOT_EVALUATED, "Scenario has not finished."
        if scenario.status == "closed":
            return NOT_EVALUATED, "Scenario was closed before it finished."
        if investigation.attempt_status == "crashed":
            return (
                EXECUTION_ERROR,
                "The browser session crashed; this is not a product finding.",
            )
        if investigation.budget_exhausted:
            return (
                NOT_EVALUATED,
                "The scenario action budget was exhausted; it was not fully evaluated.",
            )
        if investigation.action_failures or investigation.execution_errors:
            return (
                EXECUTION_ERROR,
                "The scenario recorded action failures or execution errors; "
                "this is not a product finding.",
            )
        check = investigation.check
        if check is None or check.status == "error":
            return (
                NOT_EVALUATED,
                "The frozen expectation could not be evaluated.",
            )
        if check.status == "passed":
            return NO_VIOLATION, "The frozen expectation was observed."

        confirmation = scenario.confirmation.get("status", NOT_RUN)
        if confirmation == CONFIRMED:
            return (
                VIOLATION,
                "The frozen expectation failed and the same violation recurred "
                "in a fresh replay context.",
            )
        if confirmation == NOT_REPRODUCED:
            return (
                POSSIBLE_ISSUE,
                "The expectation failed once but did not recur in replay; "
                "human confirmation is required.",
            )
        if confirmation == CONFIRMATION_ERROR:
            return (
                POSSIBLE_ISSUE,
                "The expectation failed once, but replay confirmation could not "
                "be completed; human confirmation is required.",
            )
        return (
            POSSIBLE_ISSUE,
            "The expectation failed once and has not been replay-confirmed; "
            "human confirmation is required.",
        )

    def _reconcile(self) -> None:
        for scenario in self.scenarios:
            investigation = scenario.investigation
            if scenario.status == "running" and investigation.attempt_status != "active":
                scenario.status = (
                    "finished"
                    if investigation.attempt_status == "finished"
                    else investigation.attempt_status
                )
                scenario.finished_at = investigation.finished_at or _iso()
            if scenario.status in {"finished", "crashed"}:
                scenario.check = (
                    investigation.check.to_dict() if investigation.check else None
                )
            scenario.classification, scenario.classification_reason = self._classify(
                scenario
            )

    def _exportable(self, scenario: Scenario) -> bool:
        if scenario.status != "finished" or scenario.investigation.budget_exhausted:
            return False
        if (
            scenario.investigation.action_failures
            or scenario.investigation.execution_errors
        ):
            return False
        check = scenario.investigation.check
        return check is not None and check.status in {"passed", "failed"}

    def _scenario_payload(self, scenario: Scenario) -> dict[str, Any]:
        self._reconcile_scenario_only(scenario)
        investigation = scenario.investigation
        return {
            "scenario_id": scenario.scenario_id,
            "requirement_id": scenario.proposal.requirement.id,
            "requirement_text": scenario.proposal.requirement.text,
            "title": scenario.proposal.title,
            "description": scenario.proposal.description,
            "expectation": {
                **scenario.proposal.expectation.to_dict(),
                "fingerprint": scenario.proposal.expectation.fingerprint(),
                "frozen_at": investigation.started_at,
            },
            "test_data": list(scenario.proposal.test_data),
            "session_id": scenario.session_id,
            "run_dir": scenario.run_dir_rel,
            "status": scenario.status,
            "classification": scenario.classification,
            "classification_reason": scenario.classification_reason,
            "check": (
                investigation.check.to_dict() if investigation.check else None
            ),
            "action_count": len(investigation.actions),
            "action_failure_count": len(investigation.action_failures),
            "execution_error_count": len(investigation.execution_errors),
            "budget": investigation.budget(),
            "evidence": investigation.evidence_list(),
            "export": scenario.export,
            "exportable": self._exportable(scenario),
            "replay_confirmation": scenario.confirmation,
            "started_at": scenario.started_at,
            "finished_at": scenario.finished_at,
        }

    def _reconcile_scenario_only(self, scenario: Scenario) -> None:
        investigation = scenario.investigation
        if scenario.status == "running" and investigation.attempt_status != "active":
            scenario.status = (
                "finished"
                if investigation.attempt_status == "finished"
                else investigation.attempt_status
            )
            scenario.finished_at = investigation.finished_at or _iso()
        if scenario.status in {"finished", "crashed"}:
            scenario.check = (
                investigation.check.to_dict() if investigation.check else None
            )
        scenario.classification, scenario.classification_reason = self._classify(
            scenario
        )

    def requirement_summary(self) -> list[dict[str, Any]]:
        summary: list[dict[str, Any]] = []
        for requirement in self.spec.requirements:
            scenarios = [
                scenario
                for scenario in self.scenarios
                if scenario.proposal.requirement.id == requirement.id
            ]
            classifications = {scenario.classification for scenario in scenarios}
            if VIOLATION in classifications:
                coverage = VIOLATION
            elif POSSIBLE_ISSUE in classifications:
                coverage = POSSIBLE_ISSUE
            elif EXECUTION_ERROR in classifications:
                coverage = EXECUTION_ERROR
            elif NO_VIOLATION in classifications:
                coverage = NO_VIOLATION
            else:
                coverage = NOT_EVALUATED
            summary.append(
                {
                    "id": requirement.id,
                    "text": requirement.text,
                    "scenario_ids": [item.scenario_id for item in scenarios],
                    "classification": coverage,
                    "evaluated": coverage != NOT_EVALUATED,
                }
            )
        return summary

    def status_payload(self) -> dict[str, Any]:
        self._reconcile()
        self._write_manifest()
        summary = self.requirement_summary()
        counts = {
            classification: sum(
                1 for scenario in self.scenarios if scenario.classification == classification
            )
            for classification in CLASSIFICATIONS
        }
        return {
            "status": "ok",
            "exploration_id": self.exploration_id,
            "run_id": self.run_dir.name,
            "run_dir": str(self.run_dir),
            "manifest": str(self.manifest_path),
            "exploration_status": self.status,
            "driver": self.driver,
            "model": self.model,
            "target": self.spec.target,
            "description": self.spec.description,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "requirements": [item.to_dict() for item in self.spec.requirements],
            "spec_fingerprint": self.spec.fingerprint(),
            "budget": self.budget(),
            "scenarios": [self._scenario_payload(item) for item in self.scenarios],
            "requirement_summary": summary,
            "untested_requirements": [
                {"id": item["id"], "text": item["text"]}
                for item in summary
                if not item["scenario_ids"]
            ],
            "classification_counts": counts,
            "limits": {
                "max_scenarios": self.spec.max_scenarios,
                "max_actions": self.spec.max_actions,
                "scenario_action_cap": SCENARIO_ACTION_CAP,
            },
        }

    def _manifest_document(self) -> dict[str, Any]:
        self._reconcile()
        summary = self.requirement_summary()
        return {
            "schema_version": SCHEMA_VERSION,
            "exploration_id": self.exploration_id,
            "exploration_status": self.status,
            "target": self.spec.target,
            "target_override": self.target_override,
            "description": self.spec.description,
            "driver": self.driver,
            "model": self.model,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "requirements": [item.to_dict() for item in self.spec.requirements],
            "allowed_actions": list(self.spec.allowed_actions),
            "allowed_test_data": list(self.spec.allowed_test_data),
            "excluded_interactions": list(self.spec.excluded_interactions),
            "max_scenarios": self.spec.max_scenarios,
            "max_actions": self.spec.max_actions,
            "spec_fingerprint": self.spec.fingerprint(),
            "budget": self.budget(),
            "scenarios": [self._scenario_payload(item) for item in self.scenarios],
            "requirement_summary": summary,
            "untested_requirements": [
                {"id": item["id"], "text": item["text"]}
                for item in summary
                if not item["scenario_ids"]
            ],
        }

    def _write_manifest(self) -> None:
        document = self._manifest_document()
        self.manifest_path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.summary_path.write_text(
            render_exploration_markdown(document), encoding="utf-8"
        )


def render_exploration_markdown(document: dict[str, Any]) -> str:
    """Short human summary; the HTML report is the primary presentation."""
    lines = [
        f"# Exploration: {document['exploration_id']}",
        "",
        f"- Target: `{document['target']}`",
        f"- Status: `{document['exploration_status']}`",
        f"- Driver: `{document['driver']}`",
        f"- Budget: {document['budget']['used']} used / "
        f"{document['budget']['limit']} allowed",
        "",
        "## Requirements",
        "",
        "| ID | Requirement | Coverage |",
        "| --- | --- | --- |",
    ]
    for item in document["requirement_summary"]:
        lines.append(
            f"| {item['id']} | {item['text']} | {item['classification']} |"
        )
    lines.extend(
        [
            "",
            "## Scenarios",
            "",
            "| ID | Requirement | Scenario | Classification | Replay |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if document["scenarios"]:
        for scenario in document["scenarios"]:
            replay = scenario["replay_confirmation"]["status"]
            lines.append(
                f"| {scenario['scenario_id']} | {scenario['requirement_id']} | "
                f"{scenario['title']} | {scenario['classification']} | {replay} |"
            )
    else:
        lines.append("| - | - | No scenarios were run. | not_evaluated | not_run |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "A failed expectation is an observation about this sequence, not proof "
            "of a root cause. Only a replay-confirmed, error-free violation is "
            "labelled `violation`.",
            "",
        ]
    )
    return "\n".join(lines)