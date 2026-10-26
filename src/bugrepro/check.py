"""Fixed expected-result check for a scenario.

The checker only looks at the page and the expectation. It never edits the
expectation and never treats a browser problem as a product bug.

Result semantics:
- ``passed``: the page satisfies the expectation.
- ``failed``: the expectation was evaluated and the page did not satisfy it.
  In the demo scenarios this is the reproduced bug.
- ``error`` : the expectation could not be evaluated (missing region or
  heading, timeout, browser failure). This is not proof that the reported bug
  exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .expectations import HEADING_KIND, REGION_KIND, Expectation, normalize_heading

CHECK_STATUSES = frozenset({"passed", "failed", "error"})


@dataclass
class CheckResult:
    status: str
    expected: str
    observed: list[str] = field(default_factory=list)
    reason: str = ""
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.status not in CHECK_STATUSES:
            raise ValueError(f"invalid check status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "expected": self.expected,
            "observed": list(self.observed),
            "reason": self.reason,
            "duration_ms": self.duration_ms,
        }


def evaluate_expectation(page: Any, expectation: Expectation, timeout_ms: int) -> CheckResult:
    """Evaluate the expectation against the current page without changing it."""
    started = time.perf_counter()

    def finish(status: str, observed: list[str], reason: str) -> CheckResult:
        return CheckResult(
            status=status,
            expected=expectation.text,
            observed=observed,
            reason=reason,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    if expectation.kind == HEADING_KIND:
        return _check_heading(page, expectation, timeout_ms, finish)
    if expectation.kind == REGION_KIND:
        return _check_region_text(page, expectation, timeout_ms, finish)

    return finish(
        "error",
        [],
        f"Unsupported expectation kind: {expectation.kind!r}.",
    )


def _check_heading(
    page: Any,
    expectation: Expectation,
    timeout_ms: int,
    finish: Any,
) -> CheckResult:
    expected = expectation.text
    headings = page.get_by_role("heading", level=expectation.level)
    try:
        # Attached, not visible: a heading hidden by CSS is still a concrete
        # value to report; a page that never renders one is an error.
        headings.first.wait_for(state="attached", timeout=timeout_ms)
        raw_texts = headings.all_inner_texts()
    except PlaywrightTimeoutError:
        return finish(
            "error",
            [],
            (
                f"No level-{expectation.level} heading appeared within {timeout_ms} ms. "
                "The expectation could not be evaluated; this is an execution error, "
                "not evidence of the reported bug."
            ),
        )
    except PlaywrightError as exc:
        return finish(
            "error",
            [],
            f"Browser error while looking for the expected heading: {exc}",
        )

    observed = [normalize_heading(text) for text in raw_texts if text.strip()]
    if expected in observed:
        return finish("passed", observed, f'Found heading "{expected}".')

    return finish(
        "failed",
        observed,
        f'Expected heading "{expected}", but the page showed {observed}.',
    )


def _check_region_text(
    page: Any,
    expectation: Expectation,
    timeout_ms: int,
    finish: Any,
) -> CheckResult:
    """Check that the named region contains a list item with exactly this text."""
    expected = expectation.text
    region_name = expectation.region or ""
    region = page.get_by_role("region", name=region_name, exact=True)

    try:
        region.first.wait_for(state="attached", timeout=timeout_ms)
        raw_items = region.first.get_by_role("listitem").all_inner_texts()
    except PlaywrightTimeoutError:
        return finish(
            "error",
            [],
            (
                f'No region named "{region_name}" appeared within {timeout_ms} ms. '
                "The expectation could not be evaluated; this is an execution error, "
                "not evidence of the reported bug."
            ),
        )
    except PlaywrightError as exc:
        return finish(
            "error",
            [],
            f'Browser error while looking inside region "{region_name}": {exc}',
        )

    observed = [normalize_heading(text) for text in raw_items if text.strip()]
    if expected in observed:
        return finish(
            "passed",
            observed,
            f'Region "{region_name}" contains the exact task title "{expected}".',
        )

    return finish(
        "failed",
        observed,
        (
            f'Region "{region_name}" does not contain the exact task title '
            f'"{expected}". Observed items: {observed}.'
        ),
    )