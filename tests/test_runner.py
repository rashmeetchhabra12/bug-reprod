"""Run-directory allocation and file-shape checks (no browser needed)."""

from __future__ import annotations

from datetime import datetime, timezone

from bugrepro.runner import allocate_run_dir


def test_run_directories_are_unique_for_identical_labels(tmp_path) -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    first = allocate_run_dir(
        tmp_path, label="same label", invocation="replay", now=moment
    )
    second = allocate_run_dir(
        tmp_path, label="same label", invocation="replay", now=moment
    )

    assert first != second
    assert first.is_dir() and second.is_dir()
    assert second.name.endswith("-2")


def test_run_directory_name_is_sanitized(tmp_path) -> None:
    run_dir = allocate_run_dir(
        tmp_path, label="Bug: Alice -> Bob?", invocation="replay"
    )
    assert run_dir.name.endswith("-bug-alice-bob-replay")
    assert "?" not in run_dir.name
    assert ":" not in run_dir.name