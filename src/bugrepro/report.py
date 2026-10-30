"""Readable Markdown report for a run or an agent investigation.

The report is generated from the immutable run record. It always shows the
expectation verbatim and keeps action failures, execution errors, budget state
and the expectation result visibly separate.
"""

from __future__ import annotations

import json
from typing import Any

VERDICTS = {
    "passed": "CHECK PASSED",
    "failed": "EXPECTATION FAILED - expected result not observed",
    "error": "EXECUTION ERROR - inconclusive",
}

VERDICT_NOTES = {
    "passed": "The page satisfied the recorded expectation.",
    "failed": (
        "The expectation was evaluated and the page did not meet it. This is an "
        "assertion failure about the observed result, not by itself proof that "
        "the reported bug was reproduced correctly."
    ),
    "error": (
        "The expectation could not be evaluated. This is not proof that the "
        "reported application bug was reproduced."
    ),
}


def _cell(value: Any, limit: int = 180) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = text.replace("|", "\\|")
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return f"`{text}`" if text else ""


def _json_cell(value: Any, limit: int = 180) -> str:
    return _cell(json.dumps(value, ensure_ascii=False, sort_keys=True), limit)


def _recipe_line(run: dict[str, Any]) -> str:
    recipe = run.get("recipe") or {}
    name = recipe.get("name") or "unknown"
    count = recipe.get("action_count", len(run.get("actions") or []))
    if recipe.get("sha256"):
        return (
            f"{_cell(name)} ({count} actions, "
            f"{_cell(recipe['sha256'], 80)})"
        )
    return f"{_cell(name)} ({count} actions, recipe not exported)"


def _provenance(run: dict[str, Any]) -> str:
    driver = run.get("driver")
    if driver == "codex-cli":
        return (
            "Codex CLI decision backend. Actions were submitted to the validated "
            "executor; the expectation was frozen before the first decision. "
            "The requested model, when supplied, is not a verified model identity. "
            "CLI tool-disable controls and a read-only sandbox are requested; "
            "they are not a universal no-tools isolation guarantee."
        )
    if driver in {"alan-agent", "model-agent"}:
        model = (run.get("model") or {}).get("id", "unknown")
        return (
            "Live agent investigation. The model chose each action through the "
            "local service and every action was validated by the executor. The "
            f"expectation was frozen before the first action. Model identity "
            f"({_cell(model, 80)}) is session-reported, not provider-verified."
        )
    if driver == "scripted-test":
        return (
            "Scripted test. The action sequence was written in advance; no model "
            "decisions were made."
        )
    if driver == "recorded-replay":
        return "Scripted replay of a saved recipe. No model calls were made."
    if run.get("invocation") == "replay":
        return "Scripted replay of a saved recipe. No model calls were made."
    return "Scripted recipe execution. No model was called in this run."


def render_report(run: dict[str, Any]) -> str:
    check = run["check"]
    status = check["status"]
    lines: list[str] = [
        f"# ReproWitness run: {run['run_id']}",
        "",
        f"**Verdict:** **{VERDICTS[status]}**",
        "",
        f"> {VERDICT_NOTES[status]}",
        "",
        "## Expectation (fixed)",
        "",
        "This expectation comes from human input and is never inferred from the "
        "actions or from the page.",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Kind | {_cell(run['expectation']['kind'])} |",
        f"| Expected text | {_cell(run['expectation']['text'])} |",
        *(
            [f"| Region | {_cell(run['expectation']['region'])} |"]
            if run["expectation"].get("region") is not None
            else []
        ),
        *(
            [f"| Heading level | {run['expectation']['level']} |"]
            if run["expectation"]["kind"] == "heading_text"
            else []
        ),
        f"| Fingerprint | {_cell(run['expectation']['fingerprint'], 80)} |",
    ]

    if run.get("expectation", {}).get("frozen_at"):
        lines.append(
            f"| Frozen at | {_cell(run['expectation']['frozen_at'])} |"
        )

    lines.extend(["", "## Target", ""])
    lines.append(f"- Base URL: {_cell(run['base_url'])}")
    lines.append(f"- Invocation: {_cell(run['invocation'])}")
    if run.get("driver"):
        lines.append(f"- Driver: {_cell(run['driver'])}")
    model = run.get("model") or {}
    if model.get("id"):
        lines.append(
            f"- Model: {_cell(model['id'])} "
            f"(source: {_cell(model.get('source', 'unknown'), 40)})"
        )
    if run.get("attempt_status"):
        lines.append(f"- Attempt status: {_cell(run['attempt_status'])}")
    lines.append(f"- Started: {_cell(run['started_at'])}")
    lines.append(f"- Finished: {_cell(run.get('finished_at') or 'not finished')}")
    lines.append(f"- Recipe: {_recipe_line(run)}")
    if run.get("recovery"):
        recovery = run["recovery"]
        lines.append(f"- Recovery: {recovery['used']}/{recovery['limit']}; recovered {recovery['recovered_count']}; unresolved {recovery['pending_attempt_indices']}.")
        lines.append("- Replay repeats only the successful path, not the failed attempts.")
        lines.append(f"- Recipe action order → source attempt indices: {(run.get('export') or {}).get('source_attempt_indices')}.")
    lines.append(f"- Resource policy: {_cell(run.get('resource_policy', 'strict'))}")
    lines.append(
        "- Restricted environment: all external requests are blocked. Results "
        "describe this restricted environment; blocked images may be missing."
    )
    resources = run.get("blocked_resources") or []
    if resources:
        lines.extend(["", "## Blocked resources", "",
                      "| Type | URL (credentials/query omitted) | Fatal |",
                      "| --- | --- | --- |"])
        for resource in resources:
            lines.append(
                f"| {_cell(resource.get('resource_type'))} | {_cell(resource.get('url'), 320)} "
                f"| {_cell(resource.get('fatal'))} |"
            )
    else:
        lines.append("- Blocked resources recorded: none.")
    for guard_error in run.get("request_guard_errors") or []:
        lines.append(f"- Request guard error: {_cell(guard_error)}")

    budget = run.get("budget")
    if budget:
        lines.append(
            f"- Action budget: {budget.get('used', 0)} used / "
            f"{budget.get('limit', 0)} allowed "
            f"({budget.get('remaining', 0)} remaining)"
        )

    model_calls = run.get("model_calls")
    if model_calls:
        lines.append(
            f"- Model calls: {_cell(model_calls.get('used', 0))} used / "
            f"{_cell(model_calls.get('limit', 0))} allowed"
        )

    if run.get("bug_report"):
        lines.extend(["", "## Bug report (supplied)", ""])
        for line in str(run["bug_report"]).splitlines() or [""]:
            lines.append(f"> {line}")

    requirement = run.get("requirement")
    scenario = run.get("scenario")
    if requirement or scenario:
        lines.extend(["", "## Exploration scenario", ""])
        if requirement:
            lines.append(
                f"- Requirement: {_cell(requirement.get('id'), 20)} — "
                f"{requirement.get('text', '')}"
            )
        if scenario:
            lines.append(f"- Scenario: {scenario.get('title', '')}")
            if scenario.get("description"):
                lines.append(f"- Intent: {scenario['description']}")
            test_data = scenario.get("test_data") or []
            if test_data:
                lines.append(
                    "- Test data: " + ", ".join(f"`{item}`" for item in test_data)
                )
        if run.get("exploration_id"):
            lines.append(f"- Exploration: {_cell(run['exploration_id'], 80)}")

    lines.extend(["", "## Actions", ""])
    if run["actions"]:
        lines.extend(
            [
                "| # | Action | Input | Result | Duration | Page after action |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for record in run["actions"]:
            observation = record.get("observation") or {}
            page_state = observation.get("url", "")
            headings = observation.get("headings") or []
            if headings:
                page_state = f"{page_state} | headings: {', '.join(headings)}"
            lines.append(
                "| {index} | {action} | {input} | {result} | {duration} ms | {page} |".format(
                    index=record["index"],
                    action=_cell(record["type"], 40),
                    input=_json_cell(record["input"]),
                    result=_cell(record["status"], 20),
                    duration=record["duration_ms"],
                    page=_cell(page_state, 220),
                )
            )
    else:
        lines.append("No actions were executed.")

    lines.extend(
        [
            "",
            "## Check",
            "",
            f"- Status: {_cell(check['status'], 20)}",
            f"- Expected heading: {_cell(check['expected'])}",
            f"- Observed heading(s): {_cell(', '.join(check['observed']) or '(none)')}",
            f"- Duration: {check['duration_ms']} ms",
            f"- Reason: {check['reason']}",
            "",
            "## Action failures",
            "",
        ]
    )
    if run["action_failures"]:
        for failure in run["action_failures"]:
            lines.append(
                f"- Action {failure['index']} ({_cell(failure['type'], 20)}): "
                f"{failure['error']}"
                + (f" (Recovered by attempt {failure['resolved_by']}; excluded from recipe.)"
                   if failure.get("resolved_by") else "")
            )
    else:
        lines.append("None.")

    lines.extend(["", "## Execution errors", ""])
    if run["execution_errors"]:
        for error in run["execution_errors"]:
            stage = error.get("stage", "run")
            lines.append(f"- {_cell(stage, 30)}: {error['error']}")
    else:
        lines.append("None.")

    export = run.get("export")
    if export is not None:
        lines.extend(["", "## Export", ""])
        lines.append(f"- Available: {'yes' if export.get('available') else 'no'}")
        if export.get("recipe_path"):
            lines.append(
                f"- Recipe: {_cell(export['recipe_path'])} "
                f"({_cell(export.get('sha256'), 80)})"
            )
        if export.get("reason"):
            lines.append(f"- Reason: {export['reason']}")

    evidence: list[str] = []
    for record in run["actions"]:
        if record.get("screenshot"):
            evidence.append(record["screenshot"])
    for path in run.get("evidence") or []:
        if path not in evidence:
            evidence.append(path)

    lines.extend(["", "## Evidence", ""])
    if evidence:
        for path in evidence:
            lines.append(f"- `{path}`")
    else:
        lines.append("No screenshots were captured.")

    lines.extend(
        [
            "",
            "## Provenance",
            "",
            _provenance(run),
            "",
            "A timeout, missing element, budget exhaustion or browser crash is "
            "reported as an execution error; it does not prove the reported "
            "application bug.",
            "",
        ]
    )
    return "\n".join(lines)
