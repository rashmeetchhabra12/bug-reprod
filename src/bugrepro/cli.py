"""Command line entry points.

Scripted recipes:

    bugrepro serve  --mode buggy|fixed --port 8101
    bugrepro run    recipes/sample-alice-bob.json --base-url http://127.0.0.1:8101
    bugrepro replay runs/<run-dir> --base-url http://127.0.0.1:8102

Persistent local service for an external agent:

    bugrepro agent serve
    bugrepro agent start  --target ... --report-file ... --expectation-file ...
    bugrepro agent observe --session <id>
    bugrepro agent act     --session <id> --action-file action.json
    bugrepro agent finish  --session <id>
    bugrepro agent export  --session <id>
    bugrepro agent status  --session <id>
    bugrepro agent close   --session <id>
    bugrepro agent stop

Portable evidence reports:

    bugrepro serve --app tasks --mode buggy --port 8103
    bugrepro report --attempt runs/<live> --buggy runs/<replay> \
        --fixed runs/<replay> --output runs/evidence-report.html

Requirement-guided exploration:

    bugrepro explore start --spec-file examples/task-exploration.json
    bugrepro explore scenario start --exploration <id> --scenario-file <json>
    bugrepro agent observe --session <scenario-session>
    bugrepro agent act --session <scenario-session> --action-file <json>
    bugrepro explore scenario finish --exploration <id> --scenario <scenario-id>
    bugrepro explore scenario export --exploration <id> --scenario <scenario-id>
    bugrepro explore scenario confirm --exploration <id> --scenario <scenario-id>
    bugrepro explore finish --exploration <id>
    bugrepro report --exploration runs/<exploration-dir> --output runs/exploration.html

Exit codes for run/replay/finish: 0 = expectation passed, 1 = expectation
failed, 2 = execution error / incomplete attempt. Agent and exploration
control commands return 0 on a successful operation and 2 on a service error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .agent_client import AgentClient, AgentClientError, default_state_path
from .runner import execute_recipe, resolve_recipe_source
from .sample_app.server import serve
from .service import AgentServiceError, ServiceConfig, run_service

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ERROR = 2


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    _add_resource_policy_argument(parser)
    parser.add_argument(
        "recipe",
        help="recipe JSON file, or a previous run directory containing recipe.json",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="loopback base URL to test; defaults to the recipe's recorded URL",
    )
    parser.add_argument(
        "--run-root",
        default="runs",
        help="directory that receives a new run directory (default: runs)",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="short label used in the run directory name (default: recipe name)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=_positive_int,
        default=10_000,
        help="finite timeout for each browser operation (default: 10000)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window instead of running headless",
    )


def _add_resource_policy_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--resource-policy", choices=("strict", "block-external-images"), default="strict",
        help="all external requests remain blocked; optionally treat only blocked images as nonfatal",
    )


def _add_state_file_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-file",
        default=None,
        help=(
            "local service state file "
            "(default: per-user BugRepro state path, outside the repo)"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bugrepro",
        description=(
            "Browser bug reproduction: scripted recipes plus a persistent "
            "local service for the Alan agent."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="run a sample app")
    serve_parser.add_argument(
        "--app",
        choices=("accounts", "tasks"),
        default="accounts",
        help="which fixture to serve (default: accounts)",
    )
    serve_parser.add_argument("--mode", choices=("buggy", "fixed"), required=True)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8101)

    run_parser = sub.add_parser(
        "run",
        help="execute a recipe, record a run directory, evaluate the expectation",
    )
    _add_run_arguments(run_parser)

    replay_parser = sub.add_parser(
        "replay",
        help="replay a saved recipe or run directory with no model calls",
    )
    _add_run_arguments(replay_parser)

    act_parser = sub.add_parser(
        "act",
        help="execute one JSON action in a one-shot browser and print the observation",
    )
    act_parser.add_argument(
        "--base-url",
        required=True,
        help="loopback base URL of the application under test",
    )
    act_parser.add_argument(
        "--timeout-ms",
        type=_positive_int,
        default=10_000,
        help="finite timeout for the browser operation (default: 10000)",
    )
    act_parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window instead of running headless",
    )
    act_action = act_parser.add_mutually_exclusive_group(required=True)
    act_action.add_argument(
        "--action",
        default=None,
        help="one action as JSON, e.g. "
        '\'{"type":"click","locator":{"role":"button","name":"Log in"}}\'',
    )
    act_action.add_argument(
        "--action-file",
        default=None,
        help="path to a JSON file containing one action (reliable in PowerShell)",
    )

    observe_parser = sub.add_parser(
        "observe",
        help="print the accessibility snapshot and headings of a loopback page",
    )
    observe_parser.add_argument("--base-url", required=True)
    observe_parser.add_argument(
        "--path", default="/", help="page path to observe (default: /)"
    )
    observe_parser.add_argument(
        "--timeout-ms", type=_positive_int, default=10_000
    )
    observe_parser.add_argument("--headed", action="store_true")

    agent_parser = sub.add_parser(
        "agent",
        help="persistent local browser service controlled by the Alan agent",
    )
    agent_sub = agent_parser.add_subparsers(dest="agent_command", required=True)

    agent_serve = agent_sub.add_parser(
        "serve", help="run the persistent local service in the foreground"
    )
    _add_state_file_argument(agent_serve)
    agent_serve.add_argument("--port", type=int, default=0)
    agent_serve.add_argument("--run-root", default="runs")
    agent_serve.add_argument(
        "--idle-timeout",
        type=_positive_float,
        default=900.0,
        help="stop the service after this many idle seconds (default: 900)",
    )
    agent_serve.add_argument(
        "--session-idle-timeout",
        type=_positive_float,
        default=300.0,
        help="close an idle investigation after this many seconds (default: 300)",
    )
    agent_serve.add_argument(
        "--request-timeout",
        type=_positive_float,
        default=180.0,
        help="maximum seconds a service request may wait (default: 180)",
    )
    agent_serve.add_argument("--headed", action="store_true")

    agent_stop = agent_sub.add_parser("stop", help="stop the local service")
    _add_state_file_argument(agent_stop)

    agent_start = agent_sub.add_parser(
        "start", help="start a fresh investigation with frozen inputs"
    )
    _add_state_file_argument(agent_start)
    agent_start.add_argument("--target", required=True, help="loopback app URL")
    agent_start.add_argument(
        "--report-file", required=True, help="plain-text bug report"
    )
    agent_start.add_argument(
        "--expectation-file",
        required=True,
        help='JSON expectation, e.g. {"kind":"heading_text","text":"Profile for Bob"}',
    )
    agent_start.add_argument(
        "--driver",
        choices=("alan-agent", "scripted-test"),
        default="scripted-test",
        help="label for who chose the actions (default: scripted-test)",
    )
    agent_start.add_argument(
        "--model", default="unknown", help="session-reported model identifier"
    )
    agent_start.add_argument(
        "--model-source",
        choices=("session-reported", "unavailable"),
        default="session-reported",
    )
    agent_start.add_argument("--label", default="investigation")
    agent_start.add_argument("--action-budget", type=_positive_int, default=20)
    agent_start.add_argument("--timeout-ms", type=_positive_int, default=10_000)

    agent_observe = agent_sub.add_parser("observe", help="observe the session")
    _add_state_file_argument(agent_observe)
    agent_observe.add_argument("--session", required=True)

    agent_act = agent_sub.add_parser(
        "act", help="execute one action in the persistent session"
    )
    _add_state_file_argument(agent_act)
    agent_act.add_argument("--session", required=True)
    agent_act.add_argument("--action-file", required=True)
    agent_act.add_argument(
        "--rationale-file",
        default=None,
        help="optional text file with the agent's public reason for this action",
    )

    agent_finish = agent_sub.add_parser(
        "finish", help="finish and evaluate the frozen expectation"
    )
    _add_state_file_argument(agent_finish)
    agent_finish.add_argument("--session", required=True)

    agent_export = agent_sub.add_parser(
        "export", help="export the executed sequence as a replayable recipe"
    )
    _add_state_file_argument(agent_export)
    agent_export.add_argument("--session", required=True)

    agent_status = agent_sub.add_parser("status", help="inspect session status")
    _add_state_file_argument(agent_status)
    agent_status.add_argument("--session", required=True)

    agent_close = agent_sub.add_parser("close", help="close one session")
    _add_state_file_argument(agent_close)
    agent_close.add_argument("--session", required=True)

    report_parser = sub.add_parser(
        "report",
        help="render a self-contained HTML evidence report from selected runs",
    )
    report_parser.add_argument(
        "--attempt", default=None, help="run directory of the original attempt"
    )
    report_parser.add_argument(
        "--buggy", default=None, help="run directory of the buggy replay"
    )
    report_parser.add_argument(
        "--fixed", default=None, help="run directory of the fixed replay"
    )
    report_parser.add_argument(
        "--exploration",
        default=None,
        help="exploration directory containing exploration.json",
    )
    report_parser.add_argument(
        "--output", required=True, help="path of the HTML file to write"
    )

    explore_parser = sub.add_parser(
        "explore",
        help="bounded, requirement-guided exploration through the agent service",
    )
    explore_sub = explore_parser.add_subparsers(
        dest="explore_command", required=True
    )

    explore_start = explore_sub.add_parser(
        "start", help="start a bounded exploration from a spec file"
    )
    _add_state_file_argument(explore_start)
    explore_start.add_argument("--spec-file", required=True)
    explore_start.add_argument(
        "--target",
        default=None,
        help="optional loopback target override, recorded as an override",
    )
    explore_start.add_argument(
        "--driver",
        choices=("alan-agent", "scripted-test"),
        default="scripted-test",
    )
    explore_start.add_argument("--model", default="unknown")
    explore_start.add_argument(
        "--model-source",
        choices=("session-reported", "unavailable"),
        default="session-reported",
    )
    explore_start.add_argument("--label", default="exploration")

    explore_status = explore_sub.add_parser("status", help="inspect an exploration")
    _add_state_file_argument(explore_status)
    explore_status.add_argument("--exploration", required=True)

    explore_finish = explore_sub.add_parser(
        "finish", help="finish an exploration and write its manifest"
    )
    _add_state_file_argument(explore_finish)
    explore_finish.add_argument("--exploration", required=True)

    scenario_parser = explore_sub.add_parser(
        "scenario", help="propose, run, finish, export or confirm a scenario"
    )
    scenario_sub = scenario_parser.add_subparsers(
        dest="scenario_command", required=True
    )

    scenario_start = scenario_sub.add_parser(
        "start", help="freeze a scenario expectation and start its investigation"
    )
    _add_state_file_argument(scenario_start)
    scenario_start.add_argument("--exploration", required=True)
    scenario_start.add_argument("--scenario-file", required=True)

    for name, help_text in (
        ("finish", "finish a scenario and classify it"),
        ("export", "export the scenario's executed recipe"),
        ("confirm", "replay the exported recipe to confirm a violation"),
        ("close", "close a scenario without finishing it"),
    ):
        scenario_cmd = scenario_sub.add_parser(name, help=help_text)
        _add_state_file_argument(scenario_cmd)
        scenario_cmd.add_argument("--exploration", required=True)
        scenario_cmd.add_argument("--scenario", required=True)

    investigate_parser = sub.add_parser(
        "investigate", help="investigate with a configured model endpoint"
    )
    investigate_parser.add_argument("--target", required=True)
    investigate_parser.add_argument("--report-file", required=True)
    investigate_parser.add_argument("--expectation-file", required=True)
    investigate_parser.add_argument(
        "--decision-backend", choices=("model-endpoint", "codex-cli"),
        default="model-endpoint", help="decision provider (default: model-endpoint)"
    )
    investigate_parser.add_argument(
        "--model-endpoint", help="full Chat Completions URL; required for model-endpoint"
    )
    investigate_parser.add_argument(
        "--model", help="required for model-endpoint; optional requested Codex model"
    )
    investigate_parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    investigate_parser.add_argument("--model-timeout", type=_positive_float, default=60)
    investigate_parser.add_argument("--model-call-limit", type=_positive_int, default=25)
    investigate_parser.add_argument("--action-budget", type=_positive_int, default=20)
    investigate_parser.add_argument("--timeout-ms", type=_positive_int, default=10000)
    investigate_parser.add_argument("--run-root", default="runs")
    investigate_parser.add_argument("--label", default="investigation")
    investigate_parser.add_argument("--headed", action="store_true")
    _add_resource_policy_argument(investigate_parser)
    return parser


def _print_result(result) -> None:
    print(f"Invocation: {result.invocation}")
    print(f"Driver: {result.driver}")
    print(f"Run directory: {result.run_dir}")
    print(f"Report: {result.report_path}")
    print(f"Recipe: {result.recipe.name} ({result.recipe_hash})")
    print(f"Expectation: {result.recipe.expectation.text} "
          f"({result.expectation_fingerprint})")
    print(f"Check: {result.check.status.upper()} - {result.check.reason}")
    if result.check.observed:
        print("Observed: " + " | ".join(result.check.observed))
    if result.action_failures:
        print(f"Action failures: {len(result.action_failures)}")
    if result.execution_errors:
        print(f"Execution errors: {len(result.execution_errors)}")


def _exit_code(status: str) -> int:
    if status == "passed":
        return EXIT_PASSED
    if status == "failed":
        return EXIT_FAILED
    return EXIT_ERROR


def _run_single_action(args) -> int:
    """Execute or observe in a one-shot browser, printing JSON to stdout."""
    from .browser import BrowserSession
    from .recipe import parse_action

    try:
        with BrowserSession(
            base_url=args.base_url,
            headless=not args.headed,
            timeout_ms=args.timeout_ms,
        ) as session:
            if args.command == "observe":
                goto = parse_action({"type": "goto", "path": args.path})
                observation = session.perform(goto)
                print(json.dumps(observation.to_dict(), indent=2, ensure_ascii=False))
                return EXIT_PASSED

            # Validate the agent's action with the same rules as a recipe.
            if args.action_file:
                action_raw = Path(args.action_file).read_text(encoding="utf-8-sig")
            else:
                action_raw = args.action
            action = parse_action(json.loads(action_raw))
            observation = session.perform(action)
            print(json.dumps(observation.to_dict(), indent=2, ensure_ascii=False))
            return EXIT_PASSED
    except Exception as exc:  # noqa: BLE001 - the caller gets JSON, not a traceback
        print(
            json.dumps(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                indent=2,
                ensure_ascii=False,
            )
        )
        return EXIT_ERROR


def _read_json_object(path: str | Path, what: str) -> dict[str, Any]:
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {what}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{what} must contain a JSON object.")
    return parsed


def _agent_error(exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "error": str(exc),
    }
    if isinstance(exc, AgentClientError):
        if exc.status:
            payload["http_status"] = exc.status
        if exc.payload.get("code"):
            payload["code"] = exc.payload["code"]
    return payload


def _cmd_agent_serve(args) -> int:
    config = ServiceConfig(
        state_file=Path(args.state_file) if args.state_file else default_state_path(),
        run_root=Path(args.run_root),
        port=args.port,
        idle_timeout=args.idle_timeout,
        session_idle_timeout=args.session_idle_timeout,
        headless=not args.headed,
        request_timeout=args.request_timeout,
    )
    try:
        run_service(config)
    except (AgentServiceError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    return EXIT_PASSED


def _cmd_agent_client(args) -> int:
    try:
        client = AgentClient(args.state_file)
        command = args.agent_command

        if command == "stop":
            result = client.shutdown()
            deadline = time.time() + 10
            state_path = Path(client.state_file)
            while state_path.exists() and time.time() < deadline:
                time.sleep(0.1)
            result["state_file_removed"] = not state_path.exists()
        elif command == "start":
            report_text = Path(args.report_file).read_text(encoding="utf-8-sig")
            if not report_text.strip():
                raise ValueError("The bug report file is empty.")
            if len(report_text) > 20_000:
                raise ValueError("The bug report file exceeds 20000 characters.")
            expectation = _read_json_object(args.expectation_file, "expectation file")
            payload = {
                "target": args.target,
                "bug_report": report_text,
                "expectation": expectation,
                "driver": args.driver,
                "model": {"id": args.model, "source": args.model_source},
                "label": args.label,
                "action_budget": args.action_budget,
                "timeout_ms": args.timeout_ms,
            }
            result = client.start(payload)
        elif command == "observe":
            result = client.observe(args.session)
        elif command == "act":
            action = _read_json_object(args.action_file, "action file")
            rationale = None
            if args.rationale_file:
                rationale = Path(args.rationale_file).read_text(
                    encoding="utf-8-sig"
                ).strip()
                if not rationale:
                    rationale = None
            result = client.act(args.session, action, rationale)
        elif command == "finish":
            result = client.finish(args.session)
        elif command == "export":
            result = client.export(args.session)
        elif command == "status":
            result = client.status(args.session)
        elif command == "close":
            result = client.close(args.session)
        else:  # pragma: no cover - argparse restricts the choices
            raise ValueError(f"Unknown agent command: {command}")

    except (AgentClientError, OSError, ValueError) as exc:
        print(json.dumps(_agent_error(exc), indent=2, ensure_ascii=False))
        return EXIT_ERROR

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if command == "finish":
        check = result.get("check") or {}
        return _exit_code(check.get("status", "error"))
    if command == "act" and result.get("status") != "ok":
        return EXIT_ERROR
    return EXIT_PASSED


def _cmd_explore(args) -> int:
    """Drive a bounded exploration through the local service."""
    try:
        client = AgentClient(args.state_file)
        command = args.explore_command

        if command == "start":
            spec = _read_json_object(args.spec_file, "exploration spec file")
            payload: dict[str, Any] = {
                "spec": spec,
                "driver": args.driver,
                "model": {"id": args.model, "source": args.model_source},
                "label": args.label,
            }
            if args.target:
                payload["target_override"] = args.target
            result = client.start_exploration(payload)
        elif command == "status":
            result = client.exploration_status(args.exploration)
        elif command == "finish":
            result = client.finish_exploration(args.exploration)
        elif command == "scenario":
            scenario_command = args.scenario_command
            if scenario_command == "start":
                scenario = _read_json_object(
                    args.scenario_file, "scenario file"
                )
                result = client.start_scenario(args.exploration, scenario)
            elif scenario_command == "finish":
                result = client.finish_scenario(
                    args.exploration, args.scenario
                )
            elif scenario_command == "export":
                result = client.export_scenario(
                    args.exploration, args.scenario
                )
            elif scenario_command == "confirm":
                result = client.confirm_scenario(
                    args.exploration, args.scenario
                )
            else:
                result = client.close_scenario(
                    args.exploration, args.scenario
                )
        else:  # pragma: no cover - argparse restricts choices
            raise ValueError(f"Unknown explore command: {command}")
    except (AgentClientError, OSError, ValueError) as exc:
        print(json.dumps(_agent_error(exc), indent=2, ensure_ascii=False))
        return EXIT_ERROR

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return EXIT_PASSED


def _cmd_report(args) -> int:
    """Render a self-contained HTML report from explicitly selected data."""
    if args.exploration and (args.attempt or args.buggy or args.fixed):
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "--exploration cannot be combined with --attempt/--buggy/--fixed.",
                }
            )
        )
        return EXIT_ERROR
    if not args.exploration and not args.attempt:
        print(
            json.dumps(
                {"status": "error", "error": "Provide --attempt or --exploration."}
            )
        )
        return EXIT_ERROR

    if args.exploration:
        from .html_report import (
            load_exploration_report,
            write_exploration_html,
        )

        try:
            report = load_exploration_report(args.exploration)
            output = write_exploration_html(report, args.output)
        except Exception as exc:  # noqa: BLE001 - the caller gets JSON
            print(
                json.dumps(
                    {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return EXIT_ERROR
        print(
            json.dumps(
                {
                    "status": "ok",
                    "output": str(output),
                    "exploration": str(report.run_dir),
                    "classification_counts": report.counts,
                    "scenarios": len(report.manifest.get("scenarios", [])),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return EXIT_PASSED

    from .html_report import (
        compare_fingerprints,
        load_report_run,
        write_report_html,
    )

    try:
        runs = {"attempt": load_report_run(args.attempt, "attempt")}
        if args.buggy:
            runs["buggy"] = load_report_run(args.buggy, "buggy")
        if args.fixed:
            runs["fixed"] = load_report_run(args.fixed, "fixed")
        unchanged, reasons = compare_fingerprints(runs.values())
        output = write_report_html(args.output, runs)
    except Exception as exc:  # noqa: BLE001 - the caller gets JSON
        print(
            json.dumps(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                indent=2,
                ensure_ascii=False,
            )
        )
        return EXIT_ERROR

    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(output),
                "runs": {role: str(run.run_dir) for role, run in runs.items()},
                "unchanged_replay": unchanged,
                "mismatch_reasons": reasons,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return EXIT_PASSED


def _cmd_investigate(args) -> int:
    from .model_driver import ModelClient, ModelError, investigate

    try:
        driver = "model-agent"
        if args.decision_backend == "codex-cli":
            from .codex_driver import CodexClient

            if args.model_endpoint:
                raise ModelError("--model-endpoint cannot be used with --decision-backend codex-cli.")
            model_client = CodexClient(model=args.model, timeout=args.model_timeout)
            driver = "codex-cli"
        else:
            if not args.model_endpoint or not args.model:
                raise ModelError("The model-endpoint backend requires --model-endpoint and --model.")
            model_client = ModelClient(
                args.model_endpoint, args.model, args.api_key_env, args.model_timeout
            )
        result = investigate(
            target=args.target,
            bug_report=Path(args.report_file).read_text(encoding="utf-8-sig"),
            expectation=_read_json_object(args.expectation_file, "expectation file"),
            model_client=model_client,
            run_root=args.run_root,
            label=args.label,
            action_budget=args.action_budget,
            model_call_limit=args.model_call_limit,
            timeout_ms=args.timeout_ms,
            headed=args.headed,
            driver=driver,
            resource_policy=args.resource_policy,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except Exception as exc:  # provider and input diagnostics may contain secrets
        message = (
            str(exc) if isinstance(exc, ModelError)
            else "Could not complete investigation; check input files, target and browser setup."
        )
        print(json.dumps({"status": "error", "error": message}))
        return EXIT_ERROR
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return _exit_code((result.get("check") or {}).get("status", "error"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "investigate":
        return _cmd_investigate(args)

    if args.command == "serve":
        try:
            if args.app == "tasks":
                from .task_app.server import serve as serve_tasks

                serve_tasks(mode=args.mode, host=args.host, port=args.port)
            else:
                serve(mode=args.mode, host=args.host, port=args.port)
        except (ValueError, OSError) as exc:
            print(f"Could not start the sample app: {exc}", file=sys.stderr)
            return EXIT_ERROR
        return EXIT_PASSED

    if args.command in {"act", "observe"}:
        return _run_single_action(args)

    if args.command == "agent":
        if args.agent_command == "serve":
            return _cmd_agent_serve(args)
        return _cmd_agent_client(args)

    if args.command == "explore":
        return _cmd_explore(args)

    if args.command == "report":
        return _cmd_report(args)

    invocation = "run" if args.command == "run" else "replay"
    try:
        recipe, _source = resolve_recipe_source(Path(args.recipe))
        result = execute_recipe(
            recipe=recipe,
            base_url=args.base_url or recipe.recorded_base_url,
            run_root=args.run_root,
            invocation=invocation,
            label=args.label,
            headless=not args.headed,
            timeout_ms=args.timeout_ms,
            resource_policy=args.resource_policy,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should fail cleanly
        print(f"{invocation} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    _print_result(result)
    return _exit_code(result.check.status)
