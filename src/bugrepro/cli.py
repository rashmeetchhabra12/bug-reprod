"""Command line entry points.

    bugrepro serve  --mode buggy|fixed --port 8101
    bugrepro run    recipes/sample-alice-bob.json --base-url http://127.0.0.1:8101
    bugrepro replay runs/<run-dir> --base-url http://127.0.0.1:8102

Exit codes for run/replay: 0 = expectation passed, 1 = expectation failed
(the reported bug was reproduced), 2 = execution error / inconclusive.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runner import execute_recipe, resolve_recipe_source
from .sample_app.server import serve

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


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bugrepro",
        description="Scripted browser bug reproduction and replay (Phase 1).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="run the sample app")
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
        help="execute one JSON action and print the resulting page observation",
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

    return parser


def _print_result(result) -> None:
    print(f"Invocation: {result.invocation}")
    print(f"Run directory: {result.run_dir}")
    print(f"Report: {result.report_path}")
    print(f"Recipe: {result.recipe.name} ({result.recipe_hash})")
    print(f"Expectation: {result.recipe.expectation.text} "
          f"({result.expectation_fingerprint})")
    print(f"Check: {result.check.status.upper()} - {result.check.reason}")
    if result.check.observed:
        print("Observed headings: " + " | ".join(result.check.observed))
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
    """Execute or observe on behalf of a future agent, printing JSON to stdout."""
    import json

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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "serve":
        try:
            serve(mode=args.mode, host=args.host, port=args.port)
        except (ValueError, OSError) as exc:
            print(f"Could not start the sample app: {exc}", file=sys.stderr)
            return EXIT_ERROR
        return EXIT_PASSED

    if args.command in {"act", "observe"}:
        return _run_single_action(args)

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
        )
    except Exception as exc:  # noqa: BLE001 - CLI should fail cleanly
        print(f"{invocation} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    _print_result(result)
    return _exit_code(result.check.status)