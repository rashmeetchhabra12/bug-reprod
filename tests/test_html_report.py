"""Tests for the self-contained HTML evidence report.

Synthetic run directories are used so the tests stay fast and deterministic.
They cover escaping of untrusted text, embedded screenshots, fingerprint
mismatch detection and explicit-run selection.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from bugrepro.cli import main
from bugrepro.html_report import (
    ReportError,
    compare_fingerprints,
    load_report_run,
    render_report_html,
    write_report_html,
)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)

EXPECT_FP = "sha256:" + "a" * 64
RECIPE_FP = "sha256:" + "b" * 64


def _make_run(
    root: Path,
    name: str,
    *,
    driver: str = "alan-agent",
    status: str = "failed",
    recipe_fp: str | None = RECIPE_FP,
    expectation_fp: str | None = EXPECT_FP,
    bug_report: str = "A normal report.",
    expectation_text: str = "Prepare hackathon demo",
    rationale: str = "Open the page to see the controls.",
    action_text: str = "Prepare hackathon demo",
    screenshot: str | None = "evidence/01-goto.png",
) -> Path:
    run_dir = root / name
    (run_dir / "evidence").mkdir(parents=True)
    if screenshot:
        (run_dir / screenshot).write_bytes(PNG_BYTES)

    observation = {
        "url": "http://127.0.0.1:8103/",
        "title": "Task list",
        "headings": ["Task list", "Tasks"],
        "aria_excerpt": "- heading \"Task list\"\n- region \"Tasks\"",
        "captured_at": "2026-01-01T00:00:00Z",
    }
    document = {
        "schema_version": 2,
        "run_id": name,
        "invocation": "agent" if driver == "alan-agent" else "replay",
        "driver": driver,
        "model": {"id": "deepseek/deepseek-v4.1-flash", "source": "session-reported"},
        "attempt_status": "finished",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:01:00Z",
        "base_url": "http://127.0.0.1:8103",
        "bug_report": bug_report,
        "expectation": {
            "kind": "region_text_exact",
            "text": expectation_text,
            "level": 1,
            "region": "Tasks",
            "fingerprint": expectation_fp,
            "frozen_at": "2026-01-01T00:00:00Z",
        },
        "actions": [
            {
                "index": 1,
                "type": "fill",
                "input": {
                    "type": "fill",
                    "locator": {"role": "textbox", "name": "Task title"},
                    "text": action_text,
                },
                "status": "ok",
                "duration_ms": 12,
                "observation": observation,
                "error": None,
                "screenshot": screenshot,
                "rationale": rationale,
            }
        ],
        "action_failures": [],
        "execution_errors": [],
        "budget": {"limit": 20, "used": 4, "remaining": 16, "exhausted": False},
        "check": {
            "status": status,
            "expected": expectation_text,
            "observed": [] if status == "failed" else [expectation_text],
            "reason": "Synthetic run.",
            "duration_ms": 5,
        },
        "recipe": {
            "name": name,
            "action_count": 1,
            "sha256": recipe_fp,
            "path": "recipe.json" if recipe_fp else None,
        },
        "export": {"available": True, "reason": "", "recipe_path": None, "sha256": recipe_fp},
        "evidence": [screenshot] if screenshot else [],
    }
    (run_dir / "run.json").write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )
    return run_dir


def _load_three(
    tmp_path: Path,
    *,
    attempt: dict | None = None,
    buggy: dict | None = None,
    fixed: dict | None = None,
):
    attempt_kwargs = {"driver": "alan-agent", **(attempt or {})}
    buggy_kwargs = {"driver": "recorded-replay", **(buggy or {})}
    fixed_kwargs = {
        "driver": "recorded-replay",
        "status": "passed",
        **(fixed or {}),
    }
    return {
        "attempt": load_report_run(
            _make_run(tmp_path, "attempt", **attempt_kwargs), "attempt"
        ),
        "buggy": load_report_run(
            _make_run(tmp_path, "buggy", **buggy_kwargs), "buggy"
        ),
        "fixed": load_report_run(
            _make_run(tmp_path, "fixed", **fixed_kwargs), "fixed"
        ),
    }


def test_report_is_self_contained_and_has_no_scripts(tmp_path: Path) -> None:
    runs = _load_three(tmp_path)
    html = render_report_html(runs)

    assert html.startswith("<!doctype html>")
    assert "data:image/png;base64," in html
    assert "<script" not in html.lower()
    assert "http://127.0.0.1:8103" in html
    # No external resource references.
    assert "<link" not in html.lower()
    assert "src=\"http" not in html.lower()


def test_untrusted_text_is_escaped(tmp_path: Path) -> None:
    payload = '<script>alert("x")</script>'
    runs = _load_three(
        tmp_path,
        attempt={
            "bug_report": f"Report: {payload}",
            "expectation_text": f"Title {payload}",
            "rationale": f"Because {payload}",
            "action_text": f"Input {payload}",
        },
    )
    html = render_report_html(runs)

    assert "<script" not in html.lower()
    assert "&lt;script&gt;" in html
    assert "alert(&quot;x&quot;)" in html or "alert(\"x\")" not in html
    # The raw payload must never appear unescaped.
    assert payload not in html


def test_unchanged_replay_is_flagged_as_verified(tmp_path: Path) -> None:
    runs = _load_three(tmp_path)
    html = render_report_html(runs)

    assert "Unchanged replay verified" in html
    assert "does not represent an unchanged replay" not in html


def test_different_recipe_fingerprint_is_flagged(tmp_path: Path) -> None:
    runs = _load_three(
        tmp_path,
        fixed={"recipe_fp": "sha256:" + "c" * 64},
    )
    unchanged, reasons = compare_fingerprints(runs.values())
    html = render_report_html(runs)

    assert unchanged is False
    assert any("Recipe fingerprints differ" in reason for reason in reasons)
    assert "does not represent an unchanged replay" in html
    assert "Unchanged replay verified" not in html


def test_different_expectation_fingerprint_is_flagged(tmp_path: Path) -> None:
    runs = _load_three(
        tmp_path,
        buggy={"expectation_fp": "sha256:" + "d" * 64},
    )
    unchanged, reasons = compare_fingerprints(runs.values())

    assert unchanged is False
    assert any("Expectation fingerprints differ" in reason for reason in reasons)


def test_non_deterministic_replay_is_flagged(tmp_path: Path) -> None:
    runs = _load_three(tmp_path, fixed={"driver": "scripted-test"})
    unchanged, reasons = compare_fingerprints(runs.values())

    assert unchanged is False
    assert any("not a deterministic replay" in reason for reason in reasons)


def test_missing_recipe_fingerprint_is_flagged(tmp_path: Path) -> None:
    runs = _load_three(tmp_path, buggy={"recipe_fp": None})
    unchanged, reasons = compare_fingerprints(runs.values())

    assert unchanged is False
    assert any("recipe fingerprint is missing" in reason for reason in reasons)


def test_screenshot_outside_the_run_is_not_embedded(tmp_path: Path) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG_BYTES)

    runs = _load_three(tmp_path, attempt={"screenshot": "../secret.png"})
    html = render_report_html(runs)

    assert "Screenshot unavailable" in html
    # Only the two legitimate replays contribute images.
    assert html.count("data:image/png;base64,") == 2


def test_missing_run_json_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ReportError):
        load_report_run(empty, "attempt")


def test_report_requires_an_attempt(tmp_path: Path) -> None:
    buggy = _make_run(tmp_path, "only-buggy", driver="recorded-replay")
    runs = {"buggy": load_report_run(buggy, "buggy")}
    with pytest.raises(ReportError):
        render_report_html(runs)


def test_cli_writes_only_the_selected_runs(tmp_path: Path) -> None:
    attempt = _make_run(tmp_path, "attempt")
    buggy = _make_run(tmp_path, "buggy", driver="recorded-replay")
    unrelated = _make_run(tmp_path, "unrelated", driver="recorded-replay")
    output = tmp_path / "report.html"

    exit_code = main(
        [
            "report",
            "--attempt",
            str(attempt),
            "--buggy",
            str(buggy),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert output.is_file()
    html = output.read_text(encoding="utf-8")
    assert "attempt" in html
    assert "unrelated" not in html
    assert str(unrelated) not in html


def test_write_report_creates_parent_directory(tmp_path: Path) -> None:
    runs = _load_three(tmp_path)
    target = write_report_html(tmp_path / "nested" / "dir" / "report.html", runs)
    assert target.is_file()
    assert "<!doctype html>" in target.read_text(encoding="utf-8")

def test_no_replay_does_not_claim_unchanged_replay(tmp_path: Path) -> None:
    attempt = _make_run(tmp_path, "attempt-only")
    run = load_report_run(attempt, "attempt")

    unchanged, reasons = compare_fingerprints([run])
    html = render_report_html({"attempt": run})

    assert unchanged is False
    assert any("No buggy or fixed replay run was selected." in reason for reason in reasons)
    assert "does not represent an unchanged replay" in html
    assert "Unchanged replay verified" not in html


def test_missing_expectation_fingerprint_is_flagged(tmp_path: Path) -> None:
    runs = _load_three(tmp_path, buggy={"expectation_fp": None})
    unchanged, reasons = compare_fingerprints(runs.values())

    assert unchanged is False
    assert any("expectation fingerprint is missing" in reason for reason in reasons)


def test_driver_specific_labels(tmp_path: Path) -> None:
    scripted_attempt = load_report_run(
        _make_run(tmp_path, "scripted-attempt", driver="scripted-test"),
        "attempt",
    )
    model_attempt = load_report_run(
        _make_run(tmp_path, "model-attempt", driver="alan-agent"),
        "attempt",
    )
    buggy_replay = load_report_run(
        _make_run(tmp_path, "buggy-replay", driver="recorded-replay"),
        "buggy",
    )
    scripted_fixed = load_report_run(
        _make_run(tmp_path, "scripted-fixed", driver="scripted-test"),
        "fixed",
    )

    assert scripted_attempt.label == "Scripted attempt"
    assert model_attempt.label == "Original model-driven attempt"
    assert buggy_replay.label == "Replay against buggy version"
    assert scripted_fixed.label == "Scripted run against fixed version"
