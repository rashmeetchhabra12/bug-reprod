"""Fixed expected-result check for a scenario.

The checker only looks at the page and the expectation. It never edits the
expectation and never treats a browser problem as a product bug.

Result semantics:
- ``passed``: the page satisfies the expectation.
- ``failed``: the expectation was evaluated and the page showed a different
  level-N heading. In the demo scenario this is the reproduced bug.
- ``error`` : the expectation could not be evaluated (no heading, timeout,
  browser failure). This is not proof that the reported bug exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .expectations import Expectation, normalize_heading

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
    expected = expectation.text

    def finish(status: str, observed: list[str], reason: str) -> CheckResult:
        return CheckResult(
            status=status,
            expected=expected,
            observed=observed,
            reason=reason,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    if expectation.kind != "heading_text":
        return finish(
            "error",
            [],
            f"Unsupported expectation kind: {expectation.kind!r}.",
        )

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