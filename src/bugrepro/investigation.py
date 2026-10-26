"""State for one persistent browser investigation.

An investigation owns exactly one :class:`BrowserSession` (a fresh context)
for its whole life. It records every action and observation, freezes the
human expectation at start, enforces the action budget, and finalizes into the
same run/report format used by Phase 1.

All methods are called on the service worker thread. This module never calls
a model, reads files from arbitrary paths, or executes code from inputs.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import (
    BrowserSession,
    BrowserStartupError,
    Observation,
    PlaywrightHost,
)
from .check import CheckResult, evaluate_expectation
from .expectations import Expectation
from .recipe import Action, Recipe, recipe_fingerprint, write_recipe
from .report import render_report
from .runner import (
    ActionRecord,
    allocate_run_dir,
    capture_failure_screenshot,
    capture_screenshot,
    safe_observe,
)

SCHEMA_VERSION = 2
INVESTIGATION_DRIVERS = frozenset({"alan-agent", "scripted-test"})
MODEL_SOURCES = frozenset({"session-reported", "unavailable"})
TERMINAL_ATTEMPT_STATUSES = frozenset({"finished", "closed", "crashed"})


class InvestigationError(RuntimeError):
    """An explicit, client-safe investigation error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_request",
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _iso(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class Investigation:
    """One isolated context, one frozen expectation, one bounded attempt."""

    def __init__(
        self,
        *,
        session_id: str,
        run_dir: Path,
        base_url: str,
        bug_report: str | None,
        expectation: Expectation,
        driver: str,
        model: dict[str, str],
        budget_limit: int,
        timeout_ms: int,
        session: BrowserSession,
        requirement: dict[str, Any] | None = None,
        scenario: dict[str, Any] | None = None,
        exploration_id: str | None = None,
        allowed_actions: frozenset[str] | None = None,
        allowed_fill_texts: frozenset[str] | None = None,
        invocation: str = "agent",
    ) -> None:
        if driver not in INVESTIGATION_DRIVERS:
            raise ValueError(f"unsupported investigation driver: {driver}")
        self.session_id = session_id
        self.run_dir = run_dir
        self.evidence_dir = run_dir / "evidence"
        self.base_url = base_url
        self.bug_report = bug_report
        self.expectation = expectation
        self.driver = driver
        self.model = model
        self.budget_limit = budget_limit
        self.timeout_ms = timeout_ms
        self.session = session
        self.requirement = requirement
        self.scenario = scenario
        self.exploration_id = exploration_id
        self.allowed_actions = allowed_actions
        self.allowed_fill_texts = allowed_fill_texts
        self.invocation = invocation

        self.started_at = _iso()
        self.finished_at: str | None = None
        self.attempt_status = "active"
        self.closed = False

        self.actions: list[ActionRecord] = []
        self.action_failures: list[dict[str, Any]] = []
        self.execution_errors: list[dict[str, Any]] = []
        self.executed_actions: list[Action] = []

        self.budget_used = 0
        self.budget_exhausted = False
        self.check: CheckResult | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_observation: Observation | None = None
        self.extra_evidence: list[str] = []

        self.exported = False
        self.recipe_hash: str | None = None
        self.last_used = time.monotonic()

    # ---------------------------------------------------------------- start
    @classmethod
    def start(
        cls,
        *,
        session_id: str,
        base_url: str,
        bug_report: str | None,
        expectation: Expectation,
        driver: str,
        model: dict[str, str],
        budget_limit: int,
        timeout_ms: int,
        run_root: str | Path,
        label: str,
        headless: bool,
        host: PlaywrightHost | None = None,
        requirement: dict[str, Any] | None = None,
        scenario: dict[str, Any] | None = None,
        exploration_id: str | None = None,
        allowed_actions: frozenset[str] | None = None,
        allowed_fill_texts: frozenset[str] | None = None,
        invocation: str = "agent",
    ) -> "Investigation":
        session = BrowserSession(
            base_url=base_url,
            headless=headless,
            timeout_ms=timeout_ms,
            host=host,
        )
        try:
            session.__enter__()
        except BrowserStartupError as exc:
            session.close()
            raise InvestigationError(
                str(exc), code="browser_startup", http_status=503
            ) from exc

        try:
            run_dir = allocate_run_dir(
                run_root, label=label or "investigation", invocation=invocation
            )
            (run_dir / "evidence").mkdir()
        except Exception:
            session.close()
            raise

        return cls(
            session_id=session_id,
            run_dir=run_dir,
            base_url=base_url,
            bug_report=bug_report,
            expectation=expectation,
            driver=driver,
            model=model,
            budget_limit=budget_limit,
            timeout_ms=timeout_ms,
            session=session,
            requirement=requirement,
            scenario=scenario,
            exploration_id=exploration_id,
            allowed_actions=allowed_actions,
            allowed_fill_texts=allowed_fill_texts,
            invocation=invocation,
        )

    # ------------------------------------------------------------- runtime
    def touch(self) -> None:
        self.last_used = time.monotonic()

    def budget(self) -> dict[str, Any]:
        return {
            "limit": self.budget_limit,
            "used": self.budget_used,
            "remaining": max(0, self.budget_limit - self.budget_used),
            "exhausted": self.budget_exhausted,
        }

    def _error_check(self, reason: str) -> CheckResult:
        return CheckResult(
            status="error",
            expected=self.expectation.text,
            observed=[],
            reason=reason,
            duration_ms=0,
        )

    def mark_crashed(self, reason: str) -> None:
        if self.attempt_status in {"crashed", "closed", "finished"}:
            return
        self.execution_errors.append({"stage": "browser", "error": reason})
        self.attempt_status = "crashed"
        self.finished_at = _iso()
        self.check = self._error_check(
            "Browser session crashed or became unusable. "
            "This is not evidence of the reported bug."
        )
        try:
            self.session.close()
        finally:
            self.closed = True
            self._write_record()

    def _ensure_active(self, operation: str, *, allow_finished: bool = False) -> None:
        blocked = self.closed or self.attempt_status in {"closed", "crashed"}
        if blocked or (self.attempt_status != "active" and not allow_finished):
            raise InvestigationError(
                f"Cannot {operation}: investigation is {self.attempt_status}.",
                code="session_not_active",
                http_status=409,
            )

    # ------------------------------------------------------------- actions
    def execute_action(self, action: Action, rationale: str | None = None) -> dict[str, Any]:
        self._ensure_active("execute an action")
        self.touch()

        if self.allowed_actions is not None and action.type not in self.allowed_actions:
            raise InvestigationError(
                f"Action type {action.type!r} is not allowed by the exploration "
                "specification.",
                code="action_not_allowed",
                http_status=400,
            )
        if (
            action.type == "fill"
            and self.allowed_fill_texts is not None
            and action.text not in self.allowed_fill_texts
        ):
            raise InvestigationError(
                "Fill text is not in the exploration's allowed test data.",
                code="test_data_not_allowed",
                http_status=400,
            )

        if self.budget_used >= self.budget_limit:
            self.budget_exhausted = True
            message = (
                f"Action budget exhausted ({self.budget_limit} actions already used). "
                "The attempt is incomplete and cannot become a confirmed reproduction."
            )
            self.last_result = {
                "index": None,
                "status": "error",
                "action": action.to_dict(),
                "rationale": rationale,
                "error": message,
                "at": _iso(),
            }
            return self._action_response(
                status="error",
                error=message,
                observation=self.last_observation,
            )

        self.budget_used += 1
        index = len(self.actions) + 1
        started = time.perf_counter()

        try:
            observation = self.session.perform(action)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            if self.session.blocked_requests:
                message = (
                    "Blocked non-loopback request(s): "
                    + ", ".join(self.session.blocked_requests[:3])
                )
                self.execution_errors.append(
                    {"stage": f"action {index} ({action.type})", "error": message}
                )
            else:
                message = str(exc)
                self.action_failures.append(
                    {
                        "index": index,
                        "type": action.type,
                        "category": "action_failure",
                        "error": message,
                    }
                )
            failure_observation = safe_observe(self.session)
            screenshot = capture_failure_screenshot(
                self.session, self.evidence_dir, index
            )
            self.actions.append(
                ActionRecord(
                    index=index,
                    type=action.type,
                    input=action.to_dict(),
                    status="failed",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    observation=(
                        failure_observation.to_dict() if failure_observation else None
                    ),
                    error=message,
                    screenshot=screenshot,
                    rationale=rationale,
                )
            )
            if failure_observation is not None:
                self.last_observation = failure_observation
            self.last_result = {
                "index": index,
                "status": "failed",
                "action": action.to_dict(),
                "rationale": rationale,
                "error": message,
                "at": _iso(),
            }
            if not self.session.is_healthy():
                self.mark_crashed("Browser session became unusable during an action.")
            return self._action_response(
                status="failed",
                error=message,
                observation=failure_observation or self.last_observation,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal to the service
            if self.session.blocked_requests:
                message = (
                    "Blocked non-loopback request(s): "
                    + ", ".join(self.session.blocked_requests[:3])
                )
            else:
                message = f"{type(exc).__name__}: {exc}"
            self.execution_errors.append(
                {"stage": f"action {index} ({action.type})", "error": message}
            )
            failure_observation = safe_observe(self.session)
            screenshot = capture_failure_screenshot(
                self.session, self.evidence_dir, index
            )
            self.actions.append(
                ActionRecord(
                    index=index,
                    type=action.type,
                    input=action.to_dict(),
                    status="failed",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    observation=(
                        failure_observation.to_dict() if failure_observation else None
                    ),
                    error=message,
                    screenshot=screenshot,
                    rationale=rationale,
                )
            )
            if failure_observation is not None:
                self.last_observation = failure_observation
            self.last_result = {
                "index": index,
                "status": "failed",
                "action": action.to_dict(),
                "rationale": rationale,
                "error": message,
                "at": _iso(),
            }
            if not self.session.is_healthy():
                self.mark_crashed("Browser session became unusable during an action.")
            return self._action_response(
                status="failed",
                error=message,
                observation=failure_observation or self.last_observation,
            )

        self.executed_actions.append(action)
        screenshot = capture_screenshot(
            self.session, self.evidence_dir, index, action, self.execution_errors
        )
        self.actions.append(
            ActionRecord(
                index=index,
                type=action.type,
                input=action.to_dict(),
                status="ok",
                duration_ms=int((time.perf_counter() - started) * 1000),
                observation=observation.to_dict(),
                screenshot=screenshot,
                rationale=rationale,
            )
        )
        self.last_observation = observation
        self.last_result = {
            "index": index,
            "status": "ok",
            "action": action.to_dict(),
            "rationale": rationale,
            "error": None,
            "at": _iso(),
        }
        if not self.session.is_healthy():
            self.mark_crashed("Browser session became unusable after an action.")
        return self._action_response(
            status="ok", error=None, observation=observation
        )

    def _action_response(
        self,
        *,
        status: str,
        error: str | None,
        observation: Observation | None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "session_id": self.session_id,
            "attempt_status": self.attempt_status,
            "action_result": self.last_result,
            "observation": observation.to_dict() if observation else None,
            "error": error,
            "budget": self.budget(),
            "evidence": self.evidence_list(),
        }

    # --------------------------------------------------------------- read
    def observe(self) -> dict[str, Any]:
        self._ensure_active("observe the page", allow_finished=True)
        self.touch()
        try:
            observation = self.session.observe()
        except Exception as exc:  # noqa: BLE001 - explicit session error
            message = f"Could not observe the page: {type(exc).__name__}: {exc}"
            self.execution_errors.append({"stage": "observe", "error": message})
            self.mark_crashed(message)
            raise InvestigationError(
                message, code="session_unavailable", http_status=409
            ) from exc
        self.last_observation = observation
        return {
            "status": "ok",
            "session_id": self.session_id,
            "attempt_status": self.attempt_status,
            "observation": observation.to_dict(),
            "latest_result": self.last_result,
            "budget": self.budget(),
            "evidence": self.evidence_list(),
        }

    def status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "session_id": self.session_id,
            "run_id": self.run_dir.name,
            "run_dir": str(self.run_dir),
            "report": str(self.run_dir / "report.md"),
            "driver": self.driver,
            "model": self.model,
            "attempt_status": self.attempt_status,
            "closed": self.closed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "base_url": self.base_url,
            "expectation": {
                **self.expectation.to_dict(),
                "fingerprint": self.expectation.fingerprint(),
                "frozen_at": self.started_at,
            },
            "check": self.check.to_dict() if self.check else None,
            "budget": self.budget(),
            "action_count": len(self.actions),
            "action_failure_count": len(self.action_failures),
            "execution_error_count": len(self.execution_errors),
            "latest_result": self.last_result,
            "export": self.export_summary(),
            "evidence": self.evidence_list(),
        }

    def evidence_list(self) -> list[str]:
        evidence: list[str] = []
        for record in self.actions:
            if record.screenshot and record.screenshot not in evidence:
                evidence.append(record.screenshot)
        for path in self.extra_evidence:
            if path not in evidence:
                evidence.append(path)
        return evidence

    # -------------------------------------------------------------- finish
    def finish(self) -> dict[str, Any]:
        if self.attempt_status == "finished":
            return self._finish_response()
        if self.attempt_status == "crashed":
            return self._finish_response()
        if self.attempt_status == "closed" or self.closed:
            raise InvestigationError(
                "Investigation is already closed.",
                code="session_not_active",
                http_status=409,
            )

        self.touch()
        if self.session.is_healthy():
            try:
                self.session.screenshot(self.evidence_dir / "final.png")
                self.extra_evidence.append("evidence/final.png")
            except Exception as exc:  # noqa: BLE001 - screenshot is evidence
                self.execution_errors.append(
                    {
                        "stage": "final screenshot",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        else:
            self.mark_crashed("Browser session was not usable at finish.")
            return self._finish_response()

        self.finished_at = _iso()
        self.attempt_status = "finished"

        if self.budget_exhausted:
            self.check = self._error_check(
                "Action budget was exhausted before finish. The attempt is "
                "incomplete and cannot become a confirmed reproduction."
            )
        elif self.action_failures or self.execution_errors:
            self.check = self._error_check(
                "Not evaluated because the attempt recorded "
                f"{len(self.action_failures)} action failure(s) and "
                f"{len(self.execution_errors)} execution error(s). "
                "An incomplete attempt is not a confirmed reproduction."
            )
        elif not self.executed_actions:
            self.check = self._error_check(
                "No browser actions were executed, so the expectation was not "
                "evaluated. This is not a confirmed reproduction."
            )
        else:
            try:
                self.check = evaluate_expectation(
                    self.session.page, self.expectation, self.timeout_ms
                )
            except Exception as exc:  # noqa: BLE001 - must stay explicit
                self.execution_errors.append(
                    {
                        "stage": "expectation check",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                self.check = self._error_check(
                    "Expectation check failed unexpectedly; the attempt is "
                    "inconclusive and not a confirmed reproduction."
                )

        self._write_record()
        return self._finish_response()

    def _finish_response(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "session_id": self.session_id,
            "run_id": self.run_dir.name,
            "run_dir": str(self.run_dir),
            "report": str(self.run_dir / "report.md"),
            "attempt_status": self.attempt_status,
            "driver": self.driver,
            "model": self.model,
            "expectation": {
                **self.expectation.to_dict(),
                "fingerprint": self.expectation.fingerprint(),
                "frozen_at": self.started_at,
            },
            "check": self.check.to_dict() if self.check else None,
            "budget": self.budget(),
            "action_count": len(self.actions),
            "action_failure_count": len(self.action_failures),
            "execution_error_count": len(self.execution_errors),
            "latest_result": self.last_result,
            "export": self.export_summary(),
            "evidence": self.evidence_list(),
        }

    # -------------------------------------------------------------- export
    def _export_availability(self) -> tuple[bool, str]:
        if self.exported:
            return True, ""
        if self.attempt_status != "finished":
            return False, "The attempt has not finished."
        if self.budget_exhausted:
            return False, "The action budget was exhausted."
        if self.action_failures or self.execution_errors:
            return False, "The attempt recorded action failures or execution errors."
        if self.check is None or self.check.status not in {"passed", "failed"}:
            return False, "The expectation was not evaluated successfully."
        if not self.executed_actions:
            return False, "No actions were executed."
        return True, ""

    def export_summary(self) -> dict[str, Any]:
        available, reason = self._export_availability()
        return {
            "available": available,
            "reason": reason,
            "recipe_path": "recipe.json" if self.exported else None,
            "sha256": self.recipe_hash,
        }

    def export_recipe(self) -> dict[str, Any]:
        available, reason = self._export_availability()
        if not available:
            raise InvestigationError(
                f"Recipe cannot be exported: {reason}",
                code="not_exportable",
                http_status=409,
            )

        recipe = Recipe(
            name=self.run_dir.name,
            recorded_base_url=self.base_url,
            expectation=self.expectation,
            actions=tuple(self.executed_actions),
        )
        path = self.run_dir / "recipe.json"
        write_recipe(path, recipe)
        self.exported = True
        self.recipe_hash = recipe_fingerprint(recipe)
        self._write_record()
        return {
            "status": "ok",
            "session_id": self.session_id,
            "run_id": self.run_dir.name,
            "run_dir": str(self.run_dir),
            "recipe_path": str(path),
            "recipe_sha256": self.recipe_hash,
            "expectation_fingerprint": self.expectation.fingerprint(),
            "action_count": len(recipe.actions),
            "export": self.export_summary(),
        }

    # --------------------------------------------------------------- close
    def close(self, reason: str = "Closed by client.") -> dict[str, Any]:
        self.touch()
        if self.attempt_status == "active":
            self.attempt_status = "closed"
            self.finished_at = _iso()
            self.check = self._error_check(
                f"Session closed before finish: {reason} "
                "This is not a confirmed reproduction."
            )
            self._write_record()
        try:
            self.session.close()
        finally:
            self.closed = True
        return {
            "status": "ok",
            "session_id": self.session_id,
            "attempt_status": self.attempt_status,
            "run_id": self.run_dir.name,
            "run_dir": str(self.run_dir),
            "report": str(self.run_dir / "report.md"),
            "check": self.check.to_dict() if self.check else None,
            "export": self.export_summary(),
        }

    # ------------------------------------------------------------ records
    def _run_document(self) -> dict[str, Any]:
        check = self.check or self._error_check("The attempt has not finished.")
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_dir.name,
            "invocation": self.invocation,
            "driver": self.driver,
            "model": self.model,
            "attempt_status": self.attempt_status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "base_url": self.base_url,
            "expectation": {
                **self.expectation.to_dict(),
                "fingerprint": self.expectation.fingerprint(),
                "frozen_at": self.started_at,
            },
            "actions": [record.to_dict() for record in self.actions],
            "action_failures": list(self.action_failures),
            "execution_errors": list(self.execution_errors),
            "budget": self.budget(),
            "check": check.to_dict(),
            "recipe": {
                "name": self.run_dir.name,
                "action_count": len(self.executed_actions),
                "sha256": self.recipe_hash,
                "path": "recipe.json" if self.exported else None,
            },
            "export": self.export_summary(),
            "evidence": self.evidence_list(),
        }
        if self.bug_report is not None:
            document["bug_report"] = self.bug_report
        if self.requirement is not None:
            document["requirement"] = self.requirement
        if self.scenario is not None:
            document["scenario"] = self.scenario
        if self.exploration_id is not None:
            document["exploration_id"] = self.exploration_id
        return document

    def _write_record(self) -> None:
        document = self._run_document()
        (self.run_dir / "report.md").write_text(
            render_report(document), encoding="utf-8"
        )
        (self.run_dir / "run.json").write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )