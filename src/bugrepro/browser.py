"""Playwright browser sessions used by one-shot runs and the agent service.

There is exactly one execution engine: :class:`BrowserSession`. A normal run
owns its browser; the persistent service owns a :class:`PlaywrightHost` and
gives each investigation a fresh context from it. All Playwright calls for a
host must happen on the thread that started it.

The session only executes validated :class:`~bugrepro.recipe.Action` objects.
It never evaluates model-generated Python or JavaScript.
"""

from __future__ import annotations

import json
import threading
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
RESOURCE_POLICIES = frozenset({"strict", "block-external-images"})


def validate_resource_policy(value: Any) -> str:
    if not isinstance(value, str) or value not in RESOURCE_POLICIES:
        raise ValueError("resource_policy must be strict or block-external-images.")
    return value


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


class PlaywrightHost:
    """Owns one Chromium instance for a small group of isolated contexts.

    The service creates and uses this object on a single worker thread. The
    thread guard makes accidental cross-thread Playwright use fail loudly.
    """

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._thread_id: int | None = None

    def start(self) -> "PlaywrightHost":
        if self._browser is not None:
            return self
        try:
            self._thread_id = threading.get_ident()
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
        except Exception as exc:  # noqa: BLE001 - normalized into a startup error
            self.close()
            raise BrowserStartupError(
                "Could not start Chromium. If this is a fresh environment, run "
                f"'python -m playwright install chromium'. Details: {exc}"
            ) from exc
        return self

    def _assert_owning_thread(self) -> None:
        if self._thread_id is not None and threading.get_ident() != self._thread_id:
            raise RuntimeError(
                "PlaywrightHost must be used from the thread that started it."
            )

    def new_context(self, *, timeout_ms: int) -> BrowserContext:
        self._assert_owning_thread()
        if self._browser is None:
            raise BrowserStartupError("Browser host has not been started.")
        # Service workers can bypass request interception in Chromium.
        context = self._browser.new_context(service_workers="block")
        context.set_default_timeout(timeout_ms)
        context.set_default_navigation_timeout(timeout_ms * 2)
        return context

    def is_connected(self) -> bool:
        try:
            return self._browser is not None and self._browser.is_connected()
        except Exception:  # noqa: BLE001 - a dead browser is simply not connected
            return False

    def close(self) -> None:
        for resource, method in (
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            if resource is None:
                continue
            try:
                getattr(resource, method)()
            except Exception:  # noqa: BLE001 - cleanup must not mask results
                pass
        self._browser = None
        self._playwright = None


class BrowserSession:
    """A single fresh browser context bound to one validated loopback base URL.

    Pass ``host`` to reuse a service-owned browser; the session still creates a
    brand-new context. Without a host the session owns a short-lived browser.
    """

    def __init__(
        self,
        *,
        base_url: str,
        headless: bool = True,
        timeout_ms: int = 10_000,
        host: PlaywrightHost | None = None,
        resource_policy: str = "strict",
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self.resource_policy = validate_resource_policy(resource_policy)
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._external_host = host
        self._owned_host: PlaywrightHost | None = None
        self._host = host
        self._context: BrowserContext | None = None
        self._cdp: Any | None = None
        self.page: Page | None = None
        self.blocked_requests: list[str] = []
        self.blocked_resources: list[dict[str, Any]] = []
        self.guard_errors: list[str] = []
        self._main_frame_id: str | None = None

    def __enter__(self) -> "BrowserSession":
        try:
            if self._host is None:
                self._owned_host = PlaywrightHost(headless=self.headless).start()
                self._host = self._owned_host
            self._context = self._host.new_context(timeout_ms=self.timeout_ms)
            self.page = self._context.new_page()
            # CDP Fetch validates every request, including each redirect hop,
            # before it reaches the network. Playwright's route handler does not
            # intercept redirect destinations reliably.
            self._cdp = self._context.new_cdp_session(self.page)
            self._main_frame_id = self._cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
            self._cdp.on("Fetch.requestPaused", self._guard_request)
            self._cdp.send(
                "Fetch.enable",
                {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
            )
        except BrowserStartupError:
            self.close()
            raise
        except Exception as exc:  # noqa: BLE001 - normalized into a startup error
            self.close()
            raise BrowserStartupError(
                "Could not start a browser context. "
                f"Details: {exc}"
            ) from exc
        return self

    def _guard_request(self, event: Any) -> None:
        """Continue loopback requests and abort everything else before network."""
        request_id = event.get("requestId")
        request_url = (event.get("request") or {}).get("url", "")
        if not request_id:
            return
        record = None
        try:
            if is_loopback_url(request_url):
                self._cdp.send(
                    "Fetch.continueRequest", {"requestId": request_id}
                )
            else:
                description = _describe_url(request_url)
                resource_type = event.get("resourceType", "Unknown")
                image_only = (
                    self.resource_policy == "block-external-images"
                    and resource_type == "Image"
                )
                record = {
                    "url": description,
                    "resource_type": resource_type,
                    "is_main_document": (
                        resource_type == "Document" and event.get("frameId") == self._main_frame_id
                    ),
                    "disposition": "blocked",
                    "fatal": not image_only,
                    "at": _utc_now(),
                }
                self.blocked_requests.append(description)
                self.blocked_resources.append(record)
                self._cdp.send(
                    "Fetch.failRequest",
                    {
                        "requestId": request_id,
                        "errorReason": "BlockedByClient",
                    },
                )
        except Exception as exc:  # interception failure is never an allowed-image warning
            if record is not None:
                record["fatal"] = True
            self.guard_errors.append(f"Request interception failed: {type(exc).__name__}.")

    @property
    def fatal_blocked_requests(self) -> list[str]:
        return [item["url"] for item in self.blocked_resources if item["fatal"]]

    def network_record(self) -> dict[str, Any]:
        return {
            "resource_policy": self.resource_policy,
            "blocked_resources": [dict(item) for item in self.blocked_resources],
            "request_guard_errors": list(self.guard_errors),
        }

    def check_network_policy(self) -> None:
        if self.guard_errors:
            raise BlockedTargetError(" ".join(self.guard_errors[:3]))
        fatal = self.fatal_blocked_requests
        if fatal:
            raise BlockedTargetError(
                "Blocked non-loopback request(s) during the action: " + ", ".join(fatal[:3])
            )

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close this context; close the browser only if this session owns it."""
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001 - cleanup must never mask the result
                pass
        self.page = None
        self._cdp = None
        self._context = None
        if self._owned_host is not None:
            self._owned_host.close()
            self._owned_host = None
            self._host = None

    def is_healthy(self) -> bool:
        """True when the page and its browser are still usable."""
        if self.page is None or self._host is None:
            return False
        try:
            return not self.page.is_closed() and self._host.is_connected()
        except Exception:  # noqa: BLE001 - treat any check failure as unhealthy
            return False

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
        elif action.type == "reload":
            # Same-origin reload only: the page keeps whatever origin it has.
            page.reload(wait_until="domcontentloaded")
        elif action.type in {"observe", "screenshot"}:
            pass
        else:
            raise ValueError(f"Unsupported action type: {action.type!r}")

        observation = self.observe()
        self.check_network_policy()
        return observation

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
