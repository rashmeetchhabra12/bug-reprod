"""Execute a recipe, record every action and evaluate the fixed expectation.

Run directories are created exclusively and never reused, so output from
separate invocations cannot silently overwrite earlier evidence.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import BrowserSession, BrowserStartupError, Observation
from .check import CheckResult, evaluate_expectation
from .recipe import Recipe, load_recipe, recipe_fingerprint, write_recipe
from .report import render_report
from .safety import validate_base_url

SCHEMA_VERSION = 1


@dataclass
class ActionRecord:
    index: int
    type: str
    input: dict[str, Any]
    status: str
    duration_ms: int
    observation: dict[str, Any] | None = None
    error: str | None = None
    screenshot: str | None = None
    rationale: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "type": self.type,
            "input": self.input,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "observation": self.observation,
            "error": self.error,
            "screenshot": self.screenshot,
            "rationale": self.rationale,
        }


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    invocation: str
    driver: str
    base_url: str
    recipe: Recipe
    recipe_hash: str
    expectation_fingerprint: str
    started_at: str
    finished_at: str
    actions: list[ActionRecord]
    action_failures: list[dict[str, Any]]
    execution_errors: list[dict[str, Any]]
    check: CheckResult
    report_path: Path
    run_json_path: Path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _slug(text: str, fallback: str = "run") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or fallback


def allocate_run_dir(
    run_root: str | Path,
    *,
    label: str,
    invocation: str,
    now: datetime | None = None,
) -> Path:
    """Create a unique run directory, never reusing an existing one."""
    root = Path(run_root)
    root.mkdir(parents=True, exist_ok=True)
    moment = now or _utc_now()
    stem = f"{moment.strftime('%Y%m%d-%H%M%S')}-{_slug(label)}-{_slug(invocation, 'run')}"
    for suffix in range(1, 10_000):
        candidate = root / (stem if suffix == 1 else f"{stem}-{suffix}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"Could not allocate a run directory under {root}.")


def resolve_recipe_source(target: str | Path) -> tuple[Recipe, Path]:
    """Accept either a recipe JSON file or a previous run directory."""
    path = Path(target)
    if path.is_dir():
        path = path / "recipe.json"
    return load_recipe(path), path


def execute_recipe(
    *,
    recipe: Recipe,
    base_url: str,
    run_root: str | Path = "runs",
    invocation: str = "run",
    driver: str | None = None,
    label: str | None = None,
    headless: bool = True,
    timeout_ms: int = 10_000,
) -> RunResult:
    """Run all actions, then evaluate the recipe's expectation once."""
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive.")
    if driver is None:
        driver = "recorded-replay" if invocation == "replay" else "scripted-test"
    if driver not in {"recorded-replay", "scripted-test"}:
        raise ValueError("execute_recipe only supports scripted drivers.")
    base = validate_base_url(base_url)
    started_at = _iso(_utc_now())
    run_dir = allocate_run_dir(
        run_root,
        label=label or recipe.name,
        invocation=invocation,
    )
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir()

    recipe_path = run_dir / "recipe.json"
    write_recipe(recipe_path, recipe)
    recipe_hash = recipe_fingerprint(recipe)
    expectation_fingerprint = recipe.expectation.fingerprint()

    records: list[ActionRecord] = []
    action_failures: list[dict[str, Any]] = []
    execution_errors: list[dict[str, Any]] = []
    check = CheckResult(
        status="error",
        expected=recipe.expectation.text,
        observed=[],
        reason="The check was not evaluated.",
        duration_ms=0,
    )

    try:
        with BrowserSession(
            base_url=base,
            headless=headless,
            timeout_ms=timeout_ms,
        ) as session:
            for index, action in enumerate(recipe.actions, start=1):
                action_started = time.perf_counter()
                try:
                    observation: Observation = session.perform(action)
                except (PlaywrightTimeoutError, PlaywrightError) as exc:
                    if session.blocked_requests:
                        execution_errors.append(
                            {
                                "stage": f"action {index} ({action.type})",
                                "error": (
                                    "Blocked non-loopback request(s) during the "
                                    "action: "
                                    + ", ".join(session.blocked_requests[:3])
                                ),
                            }
                        )
                    else:
                        action_failures.append(
                            {
                                "index": index,
                                "type": action.type,
                                "category": "action_failure",
                                "error": str(exc),
                            }
                        )
                    failure_shot = capture_failure_screenshot(
                        session, evidence_dir, index
                    )
                    failure_observation = safe_observe(session)
                    records.append(
                        ActionRecord(
                            index=index,
                            type=action.type,
                            input=action.to_dict(),
                            status="failed",
                            duration_ms=int(
                                (time.perf_counter() - action_started) * 1000
                            ),
                            observation=(
                                failure_observation.to_dict()
                                if failure_observation
                                else None
                            ),
                            error=str(exc),
                            screenshot=failure_shot,
                        )
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - unexpected execution fault
                    execution_errors.append(
                        {
                            "stage": f"action {index} ({action.type})",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    records.append(
                        ActionRecord(
                            index=index,
                            type=action.type,
                            input=action.to_dict(),
                            status="failed",
                            duration_ms=int(
                                (time.perf_counter() - action_started) * 1000
                            ),
                            error=str(exc),
                        )
                    )
                    break

                screenshot_rel = capture_screenshot(
                    session,
                    evidence_dir,
                    index,
                    action,
                    execution_errors,
                )
                records.append(
                    ActionRecord(
                        index=index,
                        type=action.type,
                        input=action.to_dict(),
                        status="ok",
                        duration_ms=int(
                            (time.perf_counter() - action_started) * 1000
                        ),
                        observation=observation.to_dict(),
                        screenshot=screenshot_rel,
                    )
                )

            if not action_failures and not execution_errors:
                check = evaluate_expectation(
                    session.page, recipe.expectation, timeout_ms
                )
            else:
                check = CheckResult(
                    status="error",
                    expected=recipe.expectation.text,
                    observed=[],
                    reason=(
                        "Not evaluated because the run recorded "
                        f"{len(action_failures)} action failure(s) and "
                        f"{len(execution_errors)} execution error(s). "
                        "An execution error is not proof that the reported "
                        "application bug was reproduced."
                    ),
                    duration_ms=0,
                )
    except BrowserStartupError as exc:
        execution_errors.append({"stage": "browser_startup", "error": str(exc)})
        check = CheckResult(
            status="error",
            expected=recipe.expectation.text,
            observed=[],
            reason=(
                "Not evaluated because the browser could not start. "
                "This is not evidence of the reported bug."
            ),
            duration_ms=0,
        )
    except Exception as exc:  # noqa: BLE001 - always produce a report
        execution_errors.append(
            {
                "stage": "run",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
        check = CheckResult(
            status="error",
            expected=recipe.expectation.text,
            observed=[],
            reason=(
                "Not evaluated because the run failed unexpectedly. "
                "This is not evidence of the reported bug."
            ),
            duration_ms=0,
        )

    finished_at = _iso(_utc_now())
    run_document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "invocation": invocation,
        "driver": driver,
        "started_at": started_at,
        "finished_at": finished_at,
        "base_url": base,
        "recipe": {
            "name": recipe.name,
            "action_count": len(recipe.actions),
            "sha256": recipe_hash,
            "path": "recipe.json",
            "source": str(recipe_path),
        },
        "expectation": {
            **recipe.expectation.to_dict(),
            "fingerprint": expectation_fingerprint,
        },
        "actions": [record.to_dict() for record in records],
        "action_failures": action_failures,
        "execution_errors": execution_errors,
        "check": check.to_dict(),
    }

    report_path = run_dir / "report.md"
    report_path.write_text(render_report(run_document), encoding="utf-8")
    run_json_path = run_dir / "run.json"
    run_json_path.write_text(
        json.dumps(run_document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return RunResult(
        run_id=run_dir.name,
        run_dir=run_dir,
        invocation=invocation,
        driver=driver,
        base_url=base,
        recipe=recipe,
        recipe_hash=recipe_hash,
        expectation_fingerprint=expectation_fingerprint,
        started_at=started_at,
        finished_at=finished_at,
        actions=records,
        action_failures=action_failures,
        execution_errors=execution_errors,
        check=check,
        report_path=report_path,
        run_json_path=run_json_path,
    )


def screenshot_name(index: int, action: Any) -> str:
    suffix = action.label if action.type == "screenshot" else action.type
    return f"{index:02d}-{suffix}.png"


def capture_screenshot(
    session: BrowserSession,
    evidence_dir: Path,
    index: int,
    action: Any,
    execution_errors: list[dict[str, Any]],
) -> str | None:
    relative = f"evidence/{screenshot_name(index, action)}"
    try:
        session.screenshot(evidence_dir / screenshot_name(index, action))
        return relative
    except Exception as exc:  # noqa: BLE001 - screenshot is evidence, not control flow
        execution_errors.append(
            {
                "stage": f"screenshot after action {index} ({action.type})",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None


def capture_failure_screenshot(
    session: BrowserSession, evidence_dir: Path, index: int
) -> str | None:
    name = f"{index:02d}-failure.png"
    try:
        session.screenshot(evidence_dir / name)
        return f"evidence/{name}"
    except Exception:  # noqa: BLE001 - a failed action may already have closed the page
        return None


def safe_observe(session: BrowserSession) -> Observation | None:
    try:
        return session.observe()
    except Exception:  # noqa: BLE001 - diagnostics are best effort
        return None
