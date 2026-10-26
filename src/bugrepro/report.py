"""Readable Markdown report for a run.

The report is generated from the immutable run record. It always shows the
expectation verbatim and keeps action failures, execution errors and the
expectation result visibly separate.
"""

from __future__ import annotations

import json
from typing import Any

VERDICTS = {
    "passed": "CHECK PASSED",
    "failed": "EXPECTATION FAILED - bug reproduced",
    "error": "EXECUTION ERROR - inconclusive",
}

VERDICT_NOTES = {
    "passed": "The page satisfied the recorded expectation.",
    "failed": (
        "The expectation was evaluated and the page did not meet it. "
        "In the sample scenario this is the reproduced bug."
    ),
    "error": (
        "The expectation could not be evaluated. This is not proof that the "
        "reported application bug was reproduced."
    ),
}


def _cell(value: Any, limit: int = 180) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = text.replace("|", "\\|")
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return f"`{text}`" if text else ""


def _json_cell(value: Any, limit: int = 180) -> str:
    return _cell(json.dumps(value, ensure_ascii=False, sort_keys=True), limit)


def render_report(run: dict[str, Any]) -> str:
    check = run["check"]
    status = check["status"]
    lines: list[str] = [
        f"# Bug Repro run: {run['run_id']}",
        "",
        f"**Verdict:** **{VERDICTS[status]}**",
        "",
        f"> {VERDICT_NOTES[status]}",
        "",
        "## Expectation (fixed)",
        "",
        "This expectation comes from the recipe and is never inferred from the "
        "actions or from the page.",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Kind | {_cell(run['expectation']['kind'])} |",
        f"| Heading text | {_cell(run['expectation']['text'])} |",
        f"| Heading level | {run['expectation']['level']} |",
        f"| Fingerprint | {_cell(run['expectation']['fingerprint'], 80)} |",
        "",
        "## Target",
        "",
        f"- Base URL: {_cell(run['base_url'])}",
        f"- Invocation: {_cell(run['invocation'])}",
        f"- Started: {_cell(run['started_at'])}",
        f"- Finished: {_cell(run['finished_at'])}",
        f"- Recipe: {_cell(run['recipe']['name'])} "
        f"({run['recipe']['action_count']} actions, {_cell(run['recipe']['sha256'], 80)})",
        "",
        "## Actions",
        "",
    ]

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

    evidence = [
        record["screenshot"]
        for record in run["actions"]
        if record.get("screenshot")
    ]
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
            (
                "Scripted recipe execution. No model was called in this run."
                if run["invocation"] != "replay"
                else "Scripted replay. No model calls were made."
            ),
            "",
            "A timeout or missing element is reported as an execution error; it does "
            "not prove the reported application bug.",
            "",
        ]
    )
    return "\n".join(lines)