"""Playwright browser session used by both investigation and replay.

The session only executes validated :class:`~bugrepro.recipe.Action` objects.
It never evaluates model-generated Python or JavaScript. Every run gets a
fresh browser context, every operation has a finite timeout, and cleanup is
guaranteed by the context manager.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    sync_playwright,
)

from .recipe import Action
from .safety import is_loopback_url, resolve_target, validate_base_url

ARIA_SNAPSHOT_LIMIT = 10_000


class BrowserStartupError(RuntimeError):
    """Raised when Chromium or the browser context cannot be started."""


class BlockedTargetError(RuntimeError):
    """Raised when the page tries to contact a non-loopback origin."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"


def _describe_url(url: str) -> str:
    """Describe a blocked URL without query strings or embedded credentials."""
    parts = urlsplit(url)
    host = parts.hostname or "(unknown host)"
    return f"{parts.scheme}://{host}{parts.path}"[:300]


@dataclass(frozen=True)
class Observation:
    url: str
    title: str
    headings: tuple[str, ...]
    aria_excerpt: str
    captured_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "headings": list(self.headings),
            "aria_excerpt": self.aria_excerpt,
            "captured_at": self.captured_at,
        }


class BrowserSession:
    """A single fresh browser context bound to one validated loopback base URL."""

    def __init__(
        self,
        *,
        base_url: str,
        headless: bool = True,
        timeout_ms: int = 10_000,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.blocked_requests: list[str] = []

    def __enter__(self) -> "BrowserSession":
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            self._context = self._browser.new_context()
            self._context.set_default_timeout(self.timeout_ms)
            self._context.set_default_navigation_timeout(self.timeout_ms * 2)
            # A local app must not be able to bounce the browser to the internet
            # or load remote subresources. Every request is checked.
            self._context.route("**/*", self._guard_route)
            self.page = self._context.new_page()
        except Exception as exc:  # noqa: BLE001 - normalized into a startup error
            self.close()
            raise BrowserStartupError(
                "Could not start Chromium. If this is a fresh environment, run "
                f"'python -m playwright install chromium'. Details: {exc}"
            ) from exc
        return self

    def _guard_route(self, route: Any) -> None:
        request_url = route.request.url
        if is_loopback_url(request_url):
            route.continue_()
            return
        self.blocked_requests.append(_describe_url(request_url))
        route.abort("blockedbyclient")

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close page context, browser and Playwright, ignoring cleanup noise."""
        for resource, method in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if resource is None:
                continue
            try:
                getattr(resource, method)()
            except Exception:  # noqa: BLE001 - cleanup must never mask the run result
                pass
        self.page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def _require_page(self) -> Page:
        if self.page is None:
            raise BrowserStartupError("Browser page is not available.")
        return self.page

    def perform(self, action: Action) -> Observation:
        """Execute one validated action and return the page state afterwards."""
        page = self._require_page()

        if action.type == "goto":
            assert action.path is not None
            page.goto(
                resolve_target(self.base_url, action.path),
                wait_until="domcontentloaded",
            )
        elif action.type == "click":
            assert action.locator is not None
            self._locator(action.locator).click()
        elif action.type == "fill":
            assert action.locator is not None and action.text is not None
            self._locator(action.locator).fill(action.text)
        elif action.type in {"observe", "screenshot"}:
            pass
        else:
            raise ValueError(f"Unsupported action type: {action.type!r}")

        if self.blocked_requests:
            raise BlockedTargetError(
                "Blocked non-loopback request(s) during the action: "
                + ", ".join(self.blocked_requests[:3])
            )
        return self.observe()

    def _locator(self, locator: Any) -> Any:
        page = self._require_page()
        return page.get_by_role(locator.role, name=locator.name, exact=locator.exact)

    def screenshot(self, path: str | Path) -> None:
        """Capture a full-page PNG into a run's evidence directory."""
        page = self._require_page()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target), full_page=True)

    def observe(self) -> Observation:
        """Return URL, title, headings and an accessibility snapshot excerpt."""
        page = self._require_page()

        try:
            url = page.url
        except PlaywrightError:
            url = ""
        try:
            title = page.title()
        except PlaywrightError:
            title = ""

        headings: list[str] = []
        try:
            headings = [
                text.strip()
                for text in page.get_by_role("heading").all_inner_texts()
                if text.strip()
            ][:30]
        except PlaywrightError:
            pass

        aria = ""
        try:
            aria = page.locator("body").aria_snapshot()
        except PlaywrightError:
            try:
                # Older Playwright versions expose the accessibility tree this way.
                snapshot = page.accessibility.snapshot()  # type: ignore[attr-defined]
                aria = json.dumps(snapshot, ensure_ascii=False)
            except Exception:  # noqa: BLE001 - observation is best effort
                aria = ""

        return Observation(
            url=url,
            title=title,
            headings=tuple(headings),
            aria_excerpt=_truncate(aria, ARIA_SNAPSHOT_LIMIT),
            captured_at=_utc_now(),
        )